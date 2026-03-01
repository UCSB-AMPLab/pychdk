"""USB transport layer for PTP devices.

Handles device discovery, endpoint management, and raw bulk
read/write operations over USB using pyusb.
"""
import os
import struct
import subprocess
import sys
import usb.core
import usb.util

CANON_VENDOR_ID = 0x04A9
PTP_INTERFACE_CLASS = 6       # Image
PTP_INTERFACE_SUBCLASS = 1    # Still Image Capture
PTP_INTERFACE_PROTOCOL = 1    # PTP

# USB endpoint directions
EP_DIR_IN = 0x80
EP_DIR_OUT = 0x00

DEFAULT_TIMEOUT = 5000  # ms


def find_ptp_devices(vendor_id=None):
    """Find all connected PTP devices.

    Args:
        vendor_id: Optional USB vendor ID to filter by.
                   Defaults to None (all vendors). Use
                   CANON_VENDOR_ID to find only Canon cameras.

    Returns:
        List of usb.core.Device objects with PTP interfaces.
    """
    kwargs = {}
    if vendor_id is not None:
        kwargs["idVendor"] = vendor_id

    devices = usb.core.find(find_all=True, **kwargs)
    ptp_devices = []
    for dev in devices:
        if _has_ptp_interface(dev):
            ptp_devices.append(dev)
    return ptp_devices


def _has_ptp_interface(dev):
    """Check if a USB device has a PTP interface."""
    try:
        for cfg in dev:
            for intf in cfg:
                if (intf.bInterfaceClass == PTP_INTERFACE_CLASS
                        and intf.bInterfaceSubClass == PTP_INTERFACE_SUBCLASS
                        and intf.bInterfaceProtocol == PTP_INTERFACE_PROTOCOL):
                    return True
    except usb.core.USBError:
        return False
    return False


def _kill_macos_ptp_daemon():
    """Kill macOS PTP daemons that claim the USB interface.

    macOS automatically launches ptpcamerad (or PTPCamera on older
    versions) when a PTP device is connected, which grabs the USB
    interface. We kill it and give it a moment to release.
    """
    import time
    killed = False
    for name in ("ptpcamerad", "PTPCamera"):
        result = subprocess.run(["killall", name],
                                capture_output=True, check=False)
        if result.returncode == 0:
            killed = True
    if killed:
        time.sleep(0.5)


class PTPDevice:
    """Low-level USB connection to a PTP device.

    Manages endpoint discovery, interface claiming, and raw bulk
    read/write operations.
    """

    def __init__(self, usb_device):
        self._dev = usb_device
        self._ep_in = None
        self._ep_out = None
        self._ep_int = None
        self._intf_num = None
        self._is_open = False

    @property
    def vendor_id(self):
        return self._dev.idVendor

    @property
    def product_id(self):
        return self._dev.idProduct

    @property
    def bus(self):
        return self._dev.bus

    @property
    def address(self):
        return self._dev.address

    @property
    def serial_number(self):
        try:
            return self._dev.serial_number
        except (usb.core.USBError, ValueError):
            return None

    def open(self):
        """Open the PTP device — claim interface and find endpoints."""
        if self._is_open:
            return

        try:
            self._dev.set_configuration()
        except usb.core.USBError:
            pass  # may already be configured

        # Find PTP interface and its endpoints
        cfg = self._dev[0]
        for intf in cfg:
            if (intf.bInterfaceClass == PTP_INTERFACE_CLASS
                    and intf.bInterfaceSubClass == PTP_INTERFACE_SUBCLASS
                    and intf.bInterfaceProtocol == PTP_INTERFACE_PROTOCOL):
                self._intf_num = intf.bInterfaceNumber
                break

        if self._intf_num is None:
            raise RuntimeError("No PTP interface found on device")

        # Detach kernel driver if necessary
        try:
            if self._dev.is_kernel_driver_active(self._intf_num):
                self._dev.detach_kernel_driver(self._intf_num)
        except (usb.core.USBError, NotImplementedError):
            pass

        # On macOS, kill ptpcamerad which grabs PTP devices
        if sys.platform == "darwin":
            _kill_macos_ptp_daemon()
            # Reset the device to clear stale state from the daemon
            try:
                self._dev.reset()
            except usb.core.USBError:
                pass

        usb.util.claim_interface(self._dev, self._intf_num)

        # Find endpoints
        intf = cfg[(self._intf_num, 0)]
        for ep in intf:
            attr = ep.bmAttributes & 0x03  # transfer type mask
            direction = ep.bEndpointAddress & 0x80  # direction mask
            if attr == usb.util.ENDPOINT_TYPE_BULK:
                if direction == EP_DIR_IN:
                    self._ep_in = ep
                else:
                    self._ep_out = ep
            elif attr == usb.util.ENDPOINT_TYPE_INTR:
                if direction == EP_DIR_IN:
                    self._ep_int = ep

        if self._ep_in is None or self._ep_out is None:
            raise RuntimeError("Could not find bulk endpoints on PTP device")

        self._is_open = True

    def close(self):
        """Release the USB interface."""
        if not self._is_open:
            return
        try:
            usb.util.release_interface(self._dev, self._intf_num)
        except usb.core.USBError:
            pass
        try:
            self._dev.reset()
        except usb.core.USBError:
            pass
        self._is_open = False

    def bulk_write(self, data, timeout=DEFAULT_TIMEOUT):
        """Write data to the bulk-out endpoint."""
        self._ep_out.write(data, timeout=timeout)

    def bulk_read(self, size=None, timeout=DEFAULT_TIMEOUT):
        """Read data from the bulk-in endpoint."""
        if size is None:
            size = self._ep_in.wMaxPacketSize
        return bytes(self._ep_in.read(size, timeout=timeout))

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
