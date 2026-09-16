"""Tests for high-level ChdkDevice API."""
import importlib
import signal
import struct
import sys
import threading
from unittest.mock import MagicMock, patch
import pytest
import pychdk
from pychdk import device
from pychdk.chdk import (
    ChdkCommand,
    LV_TFR_BITMAP,
    LV_TFR_PALETTE,
    LV_TFR_VIEWPORT,
    MessageType,
    REMOTE_CAP_NOTSET,
    ScriptDataType,
    ScriptErrorType,
    ScriptMessage,
)
from pychdk.device import (
    ChdkDevice,
    list_devices,
    DeviceInfo,
    _open_devices,
)
from pychdk.ptp import PTPError
from pychdk.util import iso_to_sv96


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


class _FakeClock:
    """Stands in for the time module inside pychdk.device.

    monotonic() walks a scripted sequence and then holds its last
    value, so a thirty-second deadline can be reached in no time at
    all; sleep() records what was asked for without waiting.
    """

    def __init__(self, readings):
        self._readings = list(readings)
        self.slept = 0.0

    def monotonic(self):
        if len(self._readings) > 1:
            return self._readings.pop(0)
        return self._readings[0]

    def sleep(self, seconds):
        self.slept += seconds


class TestCaptureChunkCountThroughShoot:
    """Callers use shoot(), so the count has to be reachable from there."""

    def _device_with_a_real_protocol(self):
        info = DeviceInfo(
            vendor_id=0x04A9, product_id=0x1234,
            bus_num=1, device_num=5, serial_num="ABC",
        )
        mock_session = MagicMock()
        with patch("pychdk.device.PTPDevice"), \
             patch("pychdk.device.PTPSession", return_value=mock_session):
            dev = ChdkDevice(info, _usb_device=MagicMock())
        return dev, mock_session

    def test_shoot_reports_how_many_chunks_arrived(self):
        dev, session = self._device_with_a_real_protocol()
        session.transaction.side_effect = [
            ([7, 0], b""),                  # execute_script, id 7
            ([0x01], b""),                  # ready, JPEG
            ([4, 1, 0xFFFFFFFF], b"AAAA"),  # chunk 1
            ([4, 0, 0xFFFFFFFF], b"BBBB"),  # chunk 2, the last
            ([0], b""),                     # drain: nothing waiting
        ]
        assert dev.shoot(stream=True) == b"AAAABBBB"
        assert dev.last_capture_chunks == 2

    def test_a_capture_that_never_downloads_reports_nothing(self):
        dev, session = self._device_with_a_real_protocol()
        session.transaction.side_effect = [
            ([7, 0], b""),                  # first capture: two chunks
            ([0x01], b""),
            ([4, 1, 0xFFFFFFFF], b"AAAA"),
            ([4, 0, 0xFFFFFFFF], b"BBBB"),
            ([0], b""),
        ]
        assert dev.shoot(stream=True) == b"AAAABBBB"
        assert dev.last_capture_chunks == 2

        session.transaction.side_effect = [
            ([8, 0], b""),                  # second: script starts
            ([0], b""),                     # nothing ready
            ([0], b""),                     # and the script has ended
        ]
        with pytest.raises(RuntimeError, match="without producing a capture"):
            dev.shoot(stream=True)
        # No chunk arrived, so reporting two would be a lie a bench
        # reader would believe.
        assert dev.last_capture_chunks == 0

    def test_a_refused_capture_reports_nothing(self):
        dev, session = self._device_with_a_real_protocol()
        session.transaction.side_effect = [
            ([7, 0], b""),
            ([0x01], b""),
            ([4, 0, 0xFFFFFFFF], b"JPEG"),
            ([0], b""),
        ]
        assert dev.shoot(stream=True) == b"JPEG"
        assert dev.last_capture_chunks == 1

        with pytest.raises(NotImplementedError):
            dev.shoot(dng=True, stream=True)
        assert dev.last_capture_chunks == 0

    def test_a_second_capture_that_ends_without_data_reports_nothing(
        self, monkeypatch,
    ):
        dev, session = self._device_with_a_real_protocol()
        session.transaction.side_effect = [
            ([7, 0], b""),
            ([0x01], b""),
            ([4, 0, 0xFFFFFFFF], b"JPEG"),
            ([0], b""),
        ]
        assert dev.shoot(stream=True) == b"JPEG"
        assert dev.last_capture_chunks == 1

        # Nothing ready and the script already finished: this ends on
        # the script-ended path, not at the deadline. See
        # test_a_capture_that_runs_out_its_deadline_reports_nothing.
        session.transaction.side_effect = None
        session.transaction.return_value = ([0], b"")
        monkeypatch.setattr("pychdk.device.CAPTURE_INIT_GRACE", 0.0)
        with pytest.raises(RuntimeError, match="without producing a capture"):
            dev.shoot(stream=True)
        assert dev.last_capture_chunks == 0

    def test_a_capture_that_runs_out_its_deadline_reports_nothing(
        self, monkeypatch,
    ):
        dev, session = self._device_with_a_real_protocol()
        session.transaction.side_effect = [
            ([7, 0], b""),
            ([0x01], b""),
            ([4, 0, 0xFFFFFFFF], b"JPEG"),
            ([0], b""),
        ]
        assert dev.shoot(stream=True) == b"JPEG"
        assert dev.last_capture_chunks == 1

        # A camera that stays busy and never becomes ready. The clock is
        # driven rather than waited on: the deadline is thirty seconds
        # and the suite must not spend them.
        def respond(operation, params=None, **kwargs):
            command = params[0]
            if command == ChdkCommand.EXECUTE_SCRIPT:
                return ([8, 0], b"")
            if command == ChdkCommand.REMOTE_CAPTURE_IS_READY:
                return ([0], b"")       # never ready
            if command == ChdkCommand.SCRIPT_STATUS:
                return ([0b01], b"")    # still running, nothing to say
            return ([0], b"")

        session.transaction.side_effect = respond
        clock = _FakeClock([0.0, 0.0, 0.0, 0.0, 999.0])
        monkeypatch.setattr("pychdk.device.time", clock)

        # TimeoutError, not RuntimeError: only the deadline raises this.
        with pytest.raises(TimeoutError, match="did not complete"):
            dev.shoot(stream=True)
        assert dev.last_capture_chunks == 0
        # It really went round the loop rather than falling straight out.
        assert clock.slept > 0

    def test_a_one_chunk_still_reports_one(self):
        dev, session = self._device_with_a_real_protocol()
        session.transaction.side_effect = [
            ([7, 0], b""),
            ([0x01], b""),
            ([4, 0, 0xFFFFFFFF], b"JPEG"),
            ([0], b""),
        ]
        assert dev.shoot(stream=True) == b"JPEG"
        assert dev.last_capture_chunks == 1


class TestConstructionIsExceptionSafe:
    """A claim taken during construction must not outlive the failure."""

    def _info(self):
        return DeviceInfo(
            vendor_id=0x04A9, product_id=0x1234,
            bus_num=1, device_num=5, serial_num="ABC",
        )

    def test_a_failed_session_releases_the_transport(self):
        tracked_before = len(_open_devices)
        with patch("pychdk.device.PTPDevice") as MockTransport, \
             patch("pychdk.device.PTPSession") as MockSession, \
             patch("pychdk.device.ChdkPTP"):
            MockSession.return_value.open.side_effect = RuntimeError(
                "session refused",
            )
            with pytest.raises(RuntimeError, match="session refused"):
                ChdkDevice(self._info(), _usb_device=MagicMock())
            transport = MockTransport.return_value
            transport.open.assert_called_once()
            # One open, one close: the claim does not survive the raise.
            transport.close.assert_called_once()
        assert len(_open_devices) == tracked_before

    def test_a_failed_transport_open_is_also_released(self):
        tracked_before = len(_open_devices)
        with patch("pychdk.device.PTPDevice") as MockTransport, \
             patch("pychdk.device.PTPSession"), \
             patch("pychdk.device.ChdkPTP"):
            # A transport that claims the interface and then fails
            # finding endpoints raises out of open() itself.
            MockTransport.return_value.open.side_effect = RuntimeError(
                "Could not find bulk endpoints on PTP device",
            )
            with pytest.raises(RuntimeError, match="bulk endpoints"):
                ChdkDevice(self._info(), _usb_device=MagicMock())
            MockTransport.return_value.close.assert_called_once()
        assert len(_open_devices) == tracked_before

    def test_a_failed_construction_tracks_nothing(self):
        tracked_before = len(_open_devices)
        with patch("pychdk.device.PTPDevice"), \
             patch("pychdk.device.PTPSession") as MockSession, \
             patch("pychdk.device.ChdkPTP"):
            MockSession.return_value.open.side_effect = RuntimeError("nope")
            with pytest.raises(RuntimeError):
                ChdkDevice(self._info(), _usb_device=MagicMock())
        # Nothing for _cleanup_all to find, and no half-built device.
        assert len(_open_devices) == tracked_before

    def test_a_failed_reconnect_also_releases_the_transport(self):
        with patch("pychdk.device.PTPDevice") as MockTransport, \
             patch("pychdk.device.PTPSession") as MockSession, \
             patch("pychdk.device.ChdkPTP"):
            dev = ChdkDevice(self._info(), _usb_device=MagicMock())
            transport = MockTransport.return_value
            transport.close.reset_mock()
            MockSession.return_value.open.side_effect = RuntimeError("gone")
            with pytest.raises(RuntimeError, match="gone"):
                dev.reconnect(wait=0)
            # Closed once on the way down, once releasing the failed open.
            assert transport.close.call_count == 2
            assert dev not in _open_devices
            assert not dev.is_connected


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
            ([0], b""),              # drain before get_mode: nothing pending
            ([31], b""),             # get_mode() script starts, id 31
            ([0b11], b""),           # running, its answer waiting
            ([MessageType.RET, ScriptDataType.BOOLEAN, 31, 4],
             struct.pack("<I", 1)),  # is_record true: CHDK Lua for record
        ]
        dev.switch_mode("record")

    def test_streamed_dng_is_refused(self):
        dev, mock_chdk = self._make_device()
        with pytest.raises(NotImplementedError, match="DNG"):
            dev.shoot(dng=True, stream=True)

    def test_the_dng_refusal_does_not_advise_the_card_path(self):
        dev, mock_chdk = self._make_device()
        with pytest.raises(NotImplementedError) as caught:
            dev.shoot(dng=True, stream=True)
        message = str(caught.value)
        # _shoot_standard runs shoot() and never asks for a DNG, so
        # sending the caller there would be advice that does nothing.
        assert "stream=False" not in message
        assert "card" in message.lower()

    def test_the_card_path_does_not_request_a_dng(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 5
        dev.shoot(dng=True)
        script = mock_chdk.execute_script.call_args[0][0]
        assert "dng" not in script.lower()
        assert "raw" not in script.lower()

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

    def test_streaming_names_the_error_kind(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (False, 0)
        mock_chdk.get_script_status.return_value = (True, True)
        seen = {}
        for kind in (ScriptErrorType.COMPILE, ScriptErrorType.RUN):
            mock_chdk.read_script_message.return_value = ScriptMessage(
                MessageType.ERR, kind, 7, "boom",
            )
            with pytest.raises(RuntimeError) as caught:
                dev.shoot(stream=True)
            seen[kind] = str(caught.value)
        # Telling the kinds apart in read_script_message is worth
        # nothing if the capture path flattens them again.
        assert "compile" in seen[ScriptErrorType.COMPILE]
        assert "run" in seen[ScriptErrorType.RUN]
        assert seen[ScriptErrorType.COMPILE] != seen[ScriptErrorType.RUN]

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

    def test_a_script_that_ended_uninitialized_names_both_causes(self):
        """The status cannot tell "never ran" from "ran and was cancelled".

        CHDK's set_remotecap_timeout documentation says that after a
        timeout RemoteCaptureIsReady behaves as if remote capture were
        never initialized (modules/luascript.c), so the message must
        not claim the stronger of the two.
        """
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (
            False, REMOTE_CAP_NOTSET,
        )
        mock_chdk.get_script_status.return_value = (False, False)
        with pytest.raises(RuntimeError) as caught:
            dev.shoot(stream=True)
        message = str(caught.value)
        assert "not initialized" in message
        assert "never ran" in message
        assert "cancelled" in message
        assert mock_chdk.remote_capture_is_ready.call_count == 1

    def test_an_expired_grace_says_how_long_it_waited(self, monkeypatch):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (
            False, REMOTE_CAP_NOTSET,
        )
        # Still running: the camera may merely be slow, which is a
        # different observation from a script that finished.
        mock_chdk.get_script_status.return_value = (True, False)
        monkeypatch.setattr("pychdk.device.CAPTURE_INIT_GRACE", 0.0)
        with pytest.raises(RuntimeError, match=r"within 0.0s"):
            dev.shoot(stream=True)

    def test_neither_initialization_message_claims_incompatibility(
        self, monkeypatch,
    ):
        dev, mock_chdk = self._make_device()
        mock_chdk.execute_script.return_value = 7
        mock_chdk.remote_capture_is_ready.return_value = (
            False, REMOTE_CAP_NOTSET,
        )
        monkeypatch.setattr("pychdk.device.CAPTURE_INIT_GRACE", 0.0)
        seen = []
        for running in (True, False):
            mock_chdk.get_script_status.return_value = (running, False)
            with pytest.raises(RuntimeError) as caught:
                dev.shoot(stream=True)
            seen.append(str(caught.value))
        assert seen[0] != seen[1]
        for message in seen:
            assert "support" not in message.lower()
            assert "incompatible" not in message.lower()

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


_UNSET = object()


class _RecordPlayCamera:
    """A fake ChdkPTP that answers get_mode() the way CHDK's Lua does.

    CHDK's luaCB_get_mode pushes three values — is_record, is_video,
    mode — and the first one is `!camera_info.state.mode_play`
    (modules/luascript.c). So the value a Lua caller reads first is
    is_record, and it is TRUE when the camera is in record mode.
    uBASIC's get_mode is the other polarity entirely: 0 for record, 1
    for play, 2 for video record (lib/ubasic/ubasic.c). ChdkDevice
    polls in Lua, so this fake speaks Lua.

    Written from the CHDK sources named above, not from a camera on a
    bench. The point of writing it this way round is that a fake built
    to agree with the implementation would pass under either polarity
    and prove nothing about which one CHDK uses.
    """

    def __init__(self, in_record=False, obeys=True):
        self.in_record = in_record
        self.polls = 0
        self._obeys = obeys
        self.last_capture_chunks = 0
        # Override what the poll answers, for the cases where the camera
        # says something other than its state: `answer` pins every poll,
        # `answers` is consumed one per poll and then falls back.
        self.answer = _UNSET
        self.answers = None

    def execute_script(self, script, *args, **kwargs):
        if self._obeys and script.startswith("switch_mode_usb("):
            self.in_record = script == "switch_mode_usb(1)"
        return 7

    def wait_for_script(self, *args, **kwargs):
        return None

    def execute_lua_wait(self, script, timeout=10.0):
        assert script == "return get_mode()", script
        self.polls += 1
        if self.answers:
            return self.answers.pop(0)
        if self.answer is not _UNSET:
            return self.answer
        return self.in_record


class TestSwitchModeConfirmsAgainstChdkLua:
    """The confirmation poll has to read CHDK's polarity, not uBASIC's."""

    def _device(self, fake, monkeypatch):
        monkeypatch.setattr(device, "time", _FakeClock([0.0]))
        info = DeviceInfo(
            vendor_id=0x04A9, product_id=0x1234,
            bus_num=1, device_num=5, serial_num="ABC",
        )
        with patch("pychdk.device.PTPDevice"), \
             patch("pychdk.device.PTPSession"), \
             patch("pychdk.device.ChdkPTP", return_value=fake):
            return ChdkDevice(info, _usb_device=MagicMock())

    def test_record_is_confirmed_on_the_first_poll(self, monkeypatch):
        """is_record TRUE means record, so one poll settles it."""
        fake = _RecordPlayCamera(in_record=False)
        dev = self._device(fake, monkeypatch)
        dev.switch_mode("record")
        assert fake.in_record is True
        assert fake.polls == 1

    def test_play_is_confirmed_on_the_first_poll(self, monkeypatch):
        """is_record FALSE means play, so one poll settles that too."""
        fake = _RecordPlayCamera(in_record=True)
        dev = self._device(fake, monkeypatch)
        dev.switch_mode("play")
        assert fake.in_record is False
        assert fake.polls == 1

    def test_a_switch_the_camera_never_makes_raises(self, monkeypatch):
        fake = _RecordPlayCamera(in_record=False, obeys=False)
        dev = self._device(fake, monkeypatch)
        with pytest.raises(RuntimeError) as excinfo:
            dev.switch_mode("record")
        message = str(excinfo.value)
        assert "record" in message
        assert "get_mode" in message
        assert fake.polls > 1

    def test_a_play_switch_the_camera_never_makes_raises(self, monkeypatch):
        fake = _RecordPlayCamera(in_record=True, obeys=False)
        dev = self._device(fake, monkeypatch)
        with pytest.raises(RuntimeError, match="play"):
            dev.switch_mode("play")

    def test_a_poll_with_no_answer_does_not_confirm_play(self, monkeypatch):
        """Silence is not an answer of false.

        execute_lua_wait returns None when a script ends without a RET
        message, and bool(None) is False - so a poll that came back with
        nothing at all used to satisfy a switch to play, because play is
        the mode that expects a falsy is_record. A camera that never
        answered would have been recorded as confirmed in playback.
        """
        fake = _RecordPlayCamera(in_record=True, obeys=False)
        fake.answer = None
        dev = self._device(fake, monkeypatch)
        with pytest.raises(RuntimeError) as excinfo:
            dev.switch_mode("play")
        assert "None" in str(excinfo.value)
        assert fake.polls > 1, "an unanswered poll was taken as an answer"

    def test_a_poll_that_answers_after_silence_is_still_read(self, monkeypatch):
        """Retrying a non-answer must not lose a real answer that follows."""
        fake = _RecordPlayCamera(in_record=False)
        fake.answers = [None, None, True]
        dev = self._device(fake, monkeypatch)
        dev.switch_mode("record")
        assert fake.polls == 3


class TestLivePreviewAsksForPixels:
    """GetDisplayData with no transfer flag sends no pixels at all."""

    def _make_device(self):
        info = DeviceInfo(
            vendor_id=0x04A9, product_id=0x1234,
            bus_num=1, device_num=5, serial_num="ABC",
        )
        with patch("pychdk.device.PTPDevice"), \
             patch("pychdk.device.PTPSession"), \
             patch("pychdk.device.ChdkPTP") as MockChdk:
            dev = ChdkDevice(info, _usb_device=MagicMock())
            return dev, MockChdk.return_value

    def test_the_viewport_flag_is_the_value_chdk_defines(self):
        """core/live_view.h: #define LV_TFR_VIEWPORT 0x01."""
        assert LV_TFR_VIEWPORT == 0x01

    def test_get_frames_asks_for_the_viewport(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.get_display_data.side_effect = [b"pixels", PTPError(0x2002)]
        assert list(dev.get_frames()) == [b"pixels"]
        assert mock_chdk.get_display_data.call_args_list[0].args == (
            LV_TFR_VIEWPORT,
        )

    def test_a_caller_can_ask_for_something_else(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.get_display_data.side_effect = [b"bm", PTPError(0x2002)]
        list(dev.get_frames(flags=LV_TFR_BITMAP | LV_TFR_PALETTE))
        assert mock_chdk.get_display_data.call_args_list[0].args == (
            LV_TFR_BITMAP | LV_TFR_PALETTE,
        )


class TestShootTakesTheMenuIsoAndNothingItCannotDo:
    """The signature has to name the quantity, and drop the dead options."""

    def _make_device(self):
        info = DeviceInfo(
            vendor_id=0x04A9, product_id=0x1234,
            bus_num=1, device_num=5, serial_num="ABC",
        )
        with patch("pychdk.device.PTPDevice"), \
             patch("pychdk.device.PTPSession"), \
             patch("pychdk.device.ChdkPTP") as MockChdk:
            dev = ChdkDevice(info, _usb_device=MagicMock())
            return dev, MockChdk.return_value

    def test_the_menu_number_is_converted_on_the_camera(self):
        """The whole market-to-real conversion happens in CHDK's own Lua.

        iso_to_sv96 then sv96_market_to_real then set_sv96, each of them
        CHDK's, so the per-camera SV96_MARKET_OFFSET is the camera's own
        and nothing is computed on this side.
        """
        dev, mock_chdk = self._make_device()
        dev.shoot(market_iso=400)
        script = mock_chdk.execute_script.call_args.args[0]
        assert "set_sv96(sv96_market_to_real(iso_to_sv96(400)))" in script

    def test_the_iso_is_set_as_a_script_override_not_a_menu_write(self):
        """set_sv96 outside a shot is what beats CHDK's own ISO override.

        shooting_expo_param_override_thumb applies a script's deferred
        photo_param_put_off.sv96 first and falls back to the camera's
        configured ISO override only when none was set (core/shooting.c).
        set_sv96 populates that deferred value; set_iso_mode does not, so
        a card with ISO override enabled would silently win over the
        caller. Emitting set_iso_mode here would be that regression.
        """
        dev, mock_chdk = self._make_device()
        dev.shoot(market_iso=400)
        script = mock_chdk.execute_script.call_args.args[0]
        # Both halves are needed. Without the first this passes when the
        # ISO is not set at all, which is the other way to get it wrong.
        assert "set_sv96(" in script
        assert "set_iso_mode" not in script

    def test_the_menu_number_is_not_sent_as_real_sensitivity(self):
        """The market-to-real step is not optional.

        This is the fault the argument used to have: the menu number was
        run through iso_to_sv96 and handed straight to set_sv96, which
        takes real sensitivity — about 0.7 of a stop more sensitive than
        asked, silently. The conversion has to be in the script.
        """
        dev, mock_chdk = self._make_device()
        dev.shoot(market_iso=400)
        script = mock_chdk.execute_script.call_args.args[0]
        assert "sv96_market_to_real" in script
        assert f"set_sv96({iso_to_sv96(400)})" not in script

    def test_real_iso_is_gone(self):
        dev, _ = self._make_device()
        with pytest.raises(TypeError):
            dev.shoot(real_iso=100)

    def test_download_after_is_gone(self):
        dev, _ = self._make_device()
        with pytest.raises(TypeError):
            dev.shoot(download_after=True)

    def test_remove_after_is_gone(self):
        dev, _ = self._make_device()
        with pytest.raises(TypeError):
            dev.shoot(remove_after=True)

    def test_the_card_path_returns_nothing_and_lists_nothing(self):
        dev, mock_chdk = self._make_device()
        assert dev.shoot() is None
        scripts = [
            call.args[0] for call in mock_chdk.execute_script.call_args_list
        ]
        assert not any("os.listdir" in s for s in scripts)
