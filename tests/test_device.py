"""Tests for high-level ChdkDevice API."""
import importlib
import signal
import sys
import threading
from unittest.mock import MagicMock, patch
import pytest
import pychdk
from pychdk import device
from pychdk.chdk import (
    MessageType,
    REMOTE_CAP_NOTSET,
    ScriptDataType,
    ScriptErrorType,
    ScriptMessage,
)
from pychdk.device import ChdkDevice, list_devices, DeviceInfo


class TestListDevices:
    @patch("pychdk.device.find_ptp_devices")
    def test_returns_device_infos(self, mock_find):
        mock_usb = MagicMock()
        mock_usb.idVendor = 0x04A9
        mock_usb.idProduct = 0x1234
        mock_usb.bus = 1
        mock_usb.address = 5
        mock_usb.serial_number = "ABC"
        mock_find.return_value = [mock_usb]

        devices = list_devices()
        assert len(devices) == 1
        assert devices[0].vendor_id == 0x04A9
        assert devices[0].serial_num == "ABC"

    @patch("pychdk.device.find_ptp_devices")
    def test_empty_when_no_cameras(self, mock_find):
        mock_find.return_value = []
        assert list_devices() == []


class TestChdkDevice:
    def _make_device(self):
        info = DeviceInfo(
            vendor_id=0x04A9, product_id=0x1234,
            bus_num=1, device_num=5, serial_num="ABC",
        )
        mock_usb = MagicMock()
        with patch("pychdk.device.PTPDevice") as MockTransport, \
             patch("pychdk.device.PTPSession") as MockSession, \
             patch("pychdk.device.ChdkPTP") as MockChdk:
            dev = ChdkDevice(info, _usb_device=mock_usb)
            return dev, MockChdk.return_value

    def test_lua_execute_with_return(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_lua_wait.return_value = 42
        result = dev.lua_execute("return 42")
        assert result == 42

    def test_lua_execute_no_return(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 1
        dev.lua_execute("do_something()", do_return=False)
        mock_chdk.execute_script.assert_called_once()

    def test_switch_mode(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 0
        dev.switch_mode("record")

    def test_upload_file(self, tmp_path):
        dev, mock_chdk = self._make_device()
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello")
        dev.upload_file(str(f), "A/OWN.TXT")
        mock_chdk.upload_file.assert_called_once_with(b"hello", "A/OWN.TXT")

    def test_download_file(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.download_file.return_value = b"EVEN\n"
        result = dev.download_file("A/OWN.TXT")
        assert result == b"EVEN\n"

    def test_switch_mode_waits_on_its_own_script_id(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 21
        dev.switch_mode("record")
        mock_chdk.wait_for_script.assert_called_once_with(
            timeout=5, script_id=21,
        )

    def test_switch_mode_survives_a_previous_captures_error(self):
        info = DeviceInfo(
            vendor_id=0x04A9, product_id=0x1234,
            bus_num=1, device_num=5, serial_num="ABC",
        )
        mock_usb = MagicMock()
        mock_session = MagicMock()
        with patch("pychdk.device.PTPDevice"), \
             patch("pychdk.device.PTPSession", return_value=mock_session):
            dev = ChdkDevice(info, _usb_device=mock_usb)
        # The real waiter, against a queue holding an older shot's error.
        mock_session.transaction.side_effect = [
            ([21, 0], b""),          # switch_mode_usb starts, id 21
            ([0b11], b""),           # running, message waiting
            ([MessageType.ERR, ScriptErrorType.RUN, 9, 12],
             b"stale error\x00"),    # from script 9, not ours
            ([0], b""),              # ours finished cleanly
            ([0, 0], b""),           # get_mode() script starts
            ([0], b""),              # drain: nothing pending
            ([0], b""),              # not running, no messages
        ]
        dev.switch_mode("record")

    def test_streamed_dng_is_refused(self):
        dev, mock_chdk = self._make_device()
        with pytest.raises(NotImplementedError, match="DNG"):
            dev.shoot(dng=True, stream=True)

    def _ready_camera(self, mock_chdk, formats=0x01, data=b"jpeg"):
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (True, formats)
        mock_chdk.remote_capture_get_data.return_value = data

    def test_streaming_initializes_and_shoots_in_one_script(self):
        dev, mock_chdk = self._make_device()
        self._ready_camera(mock_chdk)
        assert dev.shoot(stream=True) == b"jpeg"
        # One script: a second one would kill the first, setup included.
        assert mock_chdk.execute_script.call_count == 1
        script = mock_chdk.execute_script.call_args[0][0]
        assert "init_usb_capture" in script
        assert "shoot()" in script

    def test_streaming_downloads_without_waiting_for_the_script(self):
        dev, mock_chdk = self._make_device()
        self._ready_camera(mock_chdk)
        assert dev.shoot(stream=True) == b"jpeg"
        # Waiting for the script's return before downloading deadlocks:
        # CHDK holds the pipeline until the host takes the data.
        mock_chdk.execute_lua_wait.assert_not_called()
        assert mock_chdk.remote_capture_is_ready.call_count >= 1

    def test_streaming_raises_when_the_camera_refuses_to_initialize(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (False, 0)
        mock_chdk.get_script_status.return_value = (True, True)
        # init_usb_capture returned false, so the script returns false.
        mock_chdk.read_script_message.return_value = ScriptMessage(
            MessageType.RET, ScriptDataType.BOOLEAN, 7, False,
        )
        with pytest.raises(RuntimeError, match="initialize remote capture"):
            dev.shoot(stream=True)

    def test_streaming_raises_a_script_error_with_its_text(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (False, 0)
        mock_chdk.get_script_status.return_value = (True, True)
        mock_chdk.read_script_message.return_value = ScriptMessage(
            MessageType.ERR, ScriptErrorType.RUN, 7, "no such function",
        )
        with pytest.raises(RuntimeError, match="no such function"):
            dev.shoot(stream=True)

    def test_streaming_ignores_a_message_from_another_script(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.side_effect = [
            (False, 0), (True, 0x01),
        ]
        mock_chdk.get_script_status.return_value = (True, True)
        mock_chdk.read_script_message.return_value = ScriptMessage(
            MessageType.ERR, ScriptErrorType.RUN, 3, "stale error",
        )
        mock_chdk.remote_capture_get_data.return_value = b"jpeg"
        assert dev.shoot(stream=True) == b"jpeg"

    def test_streaming_tolerates_a_camera_that_returns_nothing(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.side_effect = [
            (False, 0), (True, 0x01),
        ]
        mock_chdk.get_script_status.return_value = (True, True)
        # An older CHDK returns nil, which is not a refusal.
        mock_chdk.read_script_message.return_value = ScriptMessage(
            MessageType.RET, ScriptDataType.NIL, 7, None,
        )
        mock_chdk.remote_capture_get_data.return_value = b"jpeg"
        assert dev.shoot(stream=True) == b"jpeg"

    def test_an_early_not_initialized_poll_is_not_a_failure(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        # CHDK acknowledges a script as scheduled, not as run, so the
        # first poll can land before init_usb_capture has executed.
        mock_chdk.remote_capture_is_ready.side_effect = [
            (False, REMOTE_CAP_NOTSET),
            (True, 0x01),
        ]
        mock_chdk.get_script_status.return_value = (True, False)
        mock_chdk.remote_capture_get_data.return_value = b"jpeg"
        assert dev.shoot(stream=True) == b"jpeg"

    def test_not_initialized_after_the_script_ended_is_a_failure(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (
            False, REMOTE_CAP_NOTSET,
        )
        mock_chdk.get_script_status.return_value = (False, False)
        with pytest.raises(RuntimeError, match="never initialized"):
            dev.shoot(stream=True)
        assert mock_chdk.remote_capture_is_ready.call_count == 1

    def test_an_initialization_failure_beats_the_early_status(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        # The status arrives first, but the script's own false is the
        # better answer and must be the one reported.
        mock_chdk.remote_capture_is_ready.return_value = (
            False, REMOTE_CAP_NOTSET,
        )
        mock_chdk.get_script_status.return_value = (True, True)
        mock_chdk.read_script_message.return_value = ScriptMessage(
            MessageType.RET, ScriptDataType.BOOLEAN, 7, False,
        )
        with pytest.raises(RuntimeError, match="refused to initialize"):
            dev.shoot(stream=True)

    def test_a_script_error_beats_the_early_status(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (
            False, REMOTE_CAP_NOTSET,
        )
        mock_chdk.get_script_status.return_value = (True, True)
        mock_chdk.read_script_message.return_value = ScriptMessage(
            MessageType.ERR, ScriptErrorType.RUN, 7, "no such function",
        )
        with pytest.raises(RuntimeError, match="no such function"):
            dev.shoot(stream=True)

    def test_streaming_stops_when_the_script_ends_without_a_capture(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (False, 0)
        mock_chdk.get_script_status.return_value = (False, False)
        with pytest.raises(RuntimeError, match="without producing a capture"):
            dev.shoot(stream=True)
        # Promptly, rather than spinning out the thirty-second deadline.
        assert mock_chdk.remote_capture_is_ready.call_count == 1

    def test_streaming_drains_the_script_after_downloading(self):
        dev, mock_chdk = self._make_device()
        self._ready_camera(mock_chdk)
        assert dev.shoot(stream=True) == b"jpeg"
        mock_chdk.drain_messages.assert_called_once()

    def test_a_failure_draining_does_not_lose_the_picture(self):
        dev, mock_chdk = self._make_device()
        self._ready_camera(mock_chdk)
        mock_chdk.drain_messages.side_effect = RuntimeError("late boom")
        assert dev.shoot(stream=True) == b"jpeg"

    def test_streaming_asks_for_a_single_data_type(self):
        dev, mock_chdk = self._make_device()
        # JPEG and RAW both ready; the request parameter takes one bit.
        mock_chdk.remote_capture_is_ready.return_value = (True, 0x03)
        mock_chdk.remote_capture_get_data.return_value = b"jpeg"
        assert dev.shoot(stream=True) == b"jpeg"
        mock_chdk.remote_capture_get_data.assert_called_once_with(1)

    def test_streaming_refuses_a_mask_without_the_requested_format(self):
        dev, mock_chdk = self._make_device()
        # No JPEG on offer: RAW (0x2) and DNG header (0x4). Returning
        # raw framebuffer bytes as though they were the JPEG asked for
        # would be a wrong picture, not a substitute.
        mock_chdk.remote_capture_is_ready.return_value = (True, 0x06)
        with pytest.raises(RuntimeError, match="0x01.*0x06"):
            dev.shoot(stream=True)
        mock_chdk.remote_capture_get_data.assert_not_called()

    def test_streaming_jpeg_is_unchanged(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.remote_capture_is_ready.return_value = (True, 0x01)
        mock_chdk.remote_capture_get_data.return_value = b"jpeg"
        assert dev.shoot(stream=True) == b"jpeg"
        mock_chdk.remote_capture_get_data.assert_called_once_with(1)

    def test_standard_shot_waits_on_its_own_script_id(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 11
        dev.shoot()
        mock_chdk.wait_for_script.assert_called_once_with(
            timeout=30, script_id=11,
        )

    def test_close(self):
        dev, mock_chdk = self._make_device()
        dev.close()
        assert not dev.is_connected


class TestSignalHandlers:
    """Signal handlers may only be installed from the main thread."""

    def test_importing_from_a_worker_thread_does_not_raise(self):
        saved_module = sys.modules.pop("pychdk.device", None)
        failures = []

        def reimport():
            try:
                importlib.import_module("pychdk.device")
            except BaseException as exc:
                failures.append(exc)

        try:
            thread = threading.Thread(target=reimport)
            thread.start()
            thread.join()
        finally:
            if saved_module is not None:
                sys.modules["pychdk.device"] = saved_module
                pychdk.device = saved_module
        assert failures == []

    def test_declines_on_a_worker_thread(self):
        before = (
            signal.getsignal(signal.SIGINT),
            signal.getsignal(signal.SIGTERM),
        )
        results = []

        def install():
            results.append(device.install_signal_handlers())

        thread = threading.Thread(target=install)
        thread.start()
        thread.join()
        assert results == [False]
        assert signal.getsignal(signal.SIGINT) is before[0]
        assert signal.getsignal(signal.SIGTERM) is before[1]

    def test_installing_twice_keeps_the_first_saved_originals(self):
        saved_handlers = (
            signal.getsignal(signal.SIGINT),
            signal.getsignal(signal.SIGTERM),
        )
        saved_originals = (device._original_sigint, device._original_sigterm)

        def sentinel_sigint(signum, frame):
            pass

        def sentinel_sigterm(signum, frame):
            pass

        try:
            signal.signal(signal.SIGINT, sentinel_sigint)
            signal.signal(signal.SIGTERM, sentinel_sigterm)
            assert device.install_signal_handlers() is True
            assert device.install_signal_handlers() is True
            assert signal.getsignal(signal.SIGINT) is device._signal_handler
            assert signal.getsignal(signal.SIGTERM) is device._signal_handler
            assert device._original_sigint is sentinel_sigint
            assert device._original_sigterm is sentinel_sigterm
        finally:
            signal.signal(signal.SIGINT, saved_handlers[0])
            signal.signal(signal.SIGTERM, saved_handlers[1])
            device._original_sigint = saved_originals[0]
            device._original_sigterm = saved_originals[1]
