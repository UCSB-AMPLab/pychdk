"""Tests for multi-camera coordination."""
from unittest.mock import MagicMock, patch
import pytest
from pychdk.multicam import MultiCam
from pychdk.device import DeviceInfo


class TestMultiCam:
    @patch("pychdk.multicam.list_devices")
    @patch("pychdk.multicam.ChdkDevice")
    def test_discovers_cameras(self, MockDevice, mock_list):
        mock_list.return_value = [
            DeviceInfo(0x04A9, 0x1234, 1, 5, "AAA"),
            DeviceInfo(0x04A9, 0x1234, 1, 6, "BBB"),
        ]
        mc = MultiCam()
        assert len(mc.cameras) == 2

    @patch("pychdk.multicam.list_devices")
    def test_no_cameras_raises(self, mock_list):
        mock_list.return_value = []
        with pytest.raises(RuntimeError, match="No CHDK cameras found"):
            MultiCam()

    @patch("pychdk.multicam.list_devices")
    @patch("pychdk.multicam.ChdkDevice")
    def test_execute_all(self, MockDevice, mock_list):
        mock_list.return_value = [
            DeviceInfo(0x04A9, 0x1234, 1, 5, "AAA"),
            DeviceInfo(0x04A9, 0x1234, 1, 6, "BBB"),
        ]
        mc = MultiCam()
        for cam in mc.cameras:
            cam.lua_execute.return_value = 4
        results = mc.execute_all("return 2 + 2")
        assert results == [4, 4]

    @patch("pychdk.multicam.list_devices")
    @patch("pychdk.multicam.ChdkDevice")
    def test_close(self, MockDevice, mock_list):
        mock_list.return_value = [
            DeviceInfo(0x04A9, 0x1234, 1, 5, "AAA"),
        ]
        mc = MultiCam()
        mc.close()
        mc.cameras[0].close.assert_called_once()
