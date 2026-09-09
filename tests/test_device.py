"""Tests for high-level ChdkDevice API."""
import importlib
import signal
import sys
import threading
from unittest.mock import MagicMock, patch
import pytest
import pychdk
from pychdk import device
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

    def test_streamed_dng_is_refused(self):
        dev, mock_chdk = self._make_device()
        with pytest.raises(NotImplementedError, match="DNG"):
            dev.shoot(dng=True, stream=True)

    def test_streaming_asks_for_a_single_data_type(self):
        dev, mock_chdk = self._make_device()
        # JPEG and RAW both ready; the request parameter takes one bit.
        mock_chdk.remote_capture_is_ready.return_value = (True, 0x03)
        mock_chdk.remote_capture_get_data.return_value = b"jpeg"
        assert dev.shoot(stream=True) == b"jpeg"
        mock_chdk.remote_capture_get_data.assert_called_once_with(1)

    def test_streaming_falls_back_to_the_lowest_ready_bit(self):
        dev, mock_chdk = self._make_device()
        # No JPEG on offer: RAW (0x2) and DNG header (0x4).
        mock_chdk.remote_capture_is_ready.return_value = (True, 0x06)
        mock_chdk.remote_capture_get_data.return_value = b"raw"
        assert dev.shoot(stream=True) == b"raw"
        mock_chdk.remote_capture_get_data.assert_called_once_with(2)

    def test_streaming_jpeg_is_unchanged(self):
        dev, mock_chdk = self._make_device()
        mock_chdk.remote_capture_is_ready.return_value = (True, 0x01)
        mock_chdk.remote_capture_get_data.return_value = b"jpeg"
        assert dev.shoot(stream=True) == b"jpeg"
        mock_chdk.remote_capture_get_data.assert_called_once_with(1)

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
