"""Tests for USB transport layer.

These tests use mocked USB devices since real cameras aren't
available in CI. Integration tests with real hardware are separate.
"""
import struct
from unittest.mock import MagicMock, patch, PropertyMock
import pytest
from pychdk.usb_transport import (
    PTPDevice,
    find_ptp_devices,
    CANON_VENDOR_ID,
    PTP_INTERFACE_CLASS,
)


def _make_mock_endpoint(direction, max_packet_size=512):
    ep = MagicMock()
    ep.bEndpointAddress = direction
    ep.wMaxPacketSize = max_packet_size
    ep.bmAttributes = 0x02 if (direction & 0x03) != 0x03 else 0x03  # bulk or interrupt
    return ep


def _make_mock_usb_device(vendor_id=0x04A9, product_id=0xABCD,
                           bus=1, address=5, serial="ABC123"):
    """Create a mock USB device that looks like a Canon PTP camera."""
    ep_out = _make_mock_endpoint(0x02)  # bulk out
    ep_out.bmAttributes = 0x02  # bulk
    ep_in = _make_mock_endpoint(0x81)   # bulk in
    ep_in.bmAttributes = 0x02  # bulk
    ep_int = _make_mock_endpoint(0x83)  # interrupt in
    ep_int.bmAttributes = 0x03  # interrupt

    interface = MagicMock()
    interface.bInterfaceClass = 6      # Image
    interface.bInterfaceSubClass = 1   # Still Image Capture
    interface.bInterfaceProtocol = 1   # PTP
    interface.bInterfaceNumber = 0
    interface.__iter__ = lambda self: iter([ep_out, ep_in, ep_int])

    config = MagicMock()
    config.__iter__ = lambda self: iter([interface])
    config.__getitem__ = lambda self, key: interface

    dev = MagicMock()
    dev.idVendor = vendor_id
    dev.idProduct = product_id
    dev.bus = bus
    dev.address = address
    dev.serial_number = serial
    dev.__iter__ = lambda self: iter([config])
    dev.__getitem__ = lambda self, i: config

    return dev


class TestFindPTPDevices:
    @patch("pychdk.usb_transport.usb.core.find")
    def test_finds_canon_ptp_devices(self, mock_find):
        mock_dev = _make_mock_usb_device()
        mock_find.return_value = [mock_dev]
        devices = find_ptp_devices()
        assert len(devices) == 1

    @patch("pychdk.usb_transport.usb.core.find")
    def test_returns_empty_when_no_devices(self, mock_find):
        mock_find.return_value = []
        devices = find_ptp_devices()
        assert len(devices) == 0


class TestPTPDevice:
    def test_open_claims_interface(self):
        mock_dev = _make_mock_usb_device()
        ptp = PTPDevice(mock_dev)
        ptp.open()
        mock_dev.set_configuration.assert_called_once()

    def test_close_releases_interface(self):
        mock_dev = _make_mock_usb_device()
        ptp = PTPDevice(mock_dev)
        ptp.open()
        ptp.close()
        assert not ptp._is_open

    def test_bulk_write(self):
        mock_dev = _make_mock_usb_device()
        ptp = PTPDevice(mock_dev)
        ptp.open()
        data = b"\x01\x02\x03\x04"
        ptp.bulk_write(data)

    def test_bulk_read(self):
        mock_dev = _make_mock_usb_device()
        ptp = PTPDevice(mock_dev)
        ptp.open()
        ptp._ep_in.read = MagicMock(return_value=b"\x0c\x00\x00\x00\x02\x00\x01\x10\x01\x00\x00\x00")
        result = ptp.bulk_read()
        assert len(result) > 0

    def test_context_manager(self):
        mock_dev = _make_mock_usb_device()
        with PTPDevice(mock_dev) as ptp:
            assert ptp._is_open
        assert not ptp._is_open
