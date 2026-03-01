"""High-level CHDK device API.

Provides ChdkDevice — the main interface for controlling a CHDK
camera — and list_devices() for discovery.
"""
import time
from collections import namedtuple

from pychdk.usb_transport import PTPDevice, find_ptp_devices, CANON_VENDOR_ID
from pychdk.ptp import PTPSession, PTPError
from pychdk.chdk import (
    ChdkPTP,
    MessageType,
    REMOTE_CAP_JPEG,
    REMOTE_CAP_RAW,
    REMOTE_CAP_DNG_HDR,
)
from pychdk.util import shutter_to_tv96, iso_to_sv96


DeviceInfo = namedtuple("DeviceInfo", [
    "vendor_id", "product_id", "bus_num", "device_num", "serial_num",
])


def list_devices():
    """Find all connected CHDK-capable Canon cameras.

    Returns:
        List of DeviceInfo namedtuples.
    """
    usb_devices = find_ptp_devices(vendor_id=CANON_VENDOR_ID)
    result = []
    for usb_dev in usb_devices:
        try:
            serial = usb_dev.serial_number
        except Exception:
            serial = None
        info = DeviceInfo(
            vendor_id=usb_dev.idVendor,
            product_id=usb_dev.idProduct,
            bus_num=usb_dev.bus,
            device_num=usb_dev.address,
            serial_num=serial,
        )
        result.append(info)
    return result


def _find_usb_device(info):
    """Find the raw USB device matching a DeviceInfo."""
    import usb.core
    devs = usb.core.find(find_all=True, idVendor=info.vendor_id,
                         idProduct=info.product_id)
    for dev in devs:
        if dev.bus == info.bus_num and dev.address == info.device_num:
            return dev
    raise RuntimeError(
        f"USB device not found: bus={info.bus_num} addr={info.device_num}"
    )


class ChdkDevice:
    """High-level interface to a CHDK camera.

    Wraps USB transport, PTP session, and CHDK protocol into a
    single object with methods matching what Captua expects.
    """

    def __init__(self, device_info, _usb_device=None):
        self.info = device_info
        self._usb_device = _usb_device or _find_usb_device(device_info)
        self._transport = PTPDevice(self._usb_device)
        self._session = PTPSession(self._transport)
        self._chdk = ChdkPTP(self._session)
        self._connected = False
        self._open()

    def _open(self):
        self._transport.open()
        self._session.open()
        self._connected = True

    @property
    def is_connected(self):
        return self._connected

    def switch_mode(self, mode):
        """Switch camera to 'record' or 'play' mode."""
        mode_val = 1 if mode == "record" else 0
        self.lua_execute(f"switch_mode_usb({mode_val})", do_return=False)
        time.sleep(1)  # camera needs time to switch

    def lua_execute(self, lua_code, do_return=True, timeout=10.0):
        """Execute Lua code on the camera.

        Args:
            lua_code: Lua script string.
            do_return: If True, wait for and return the result.
            timeout: Max seconds to wait for result.

        Returns:
            Script return value if do_return is True, else None.
        """
        if do_return:
            return self._chdk.execute_lua_wait(lua_code, timeout=timeout)
        else:
            self._chdk.execute_script(lua_code)
            return None

    def shoot(self, shutter_speed=None, market_iso=None, dng=False,
              stream=False, download_after=False, remove_after=False):
        """Capture a photo.

        Args:
            shutter_speed: Shutter speed in seconds (e.g., 1/100).
            market_iso: ISO value (e.g., 100, 200).
            dng: If True, capture in DNG raw format.
            stream: If True, use remote capture (direct USB transfer).
            download_after: If True (and stream=False), download from SD card.
            remove_after: If True, delete from SD card after download.

        Returns:
            Image data as bytes when stream=True or download_after=True.
        """
        parts = []
        if shutter_speed is not None:
            tv96 = shutter_to_tv96(shutter_speed)
            parts.append(f"set_tv96_direct({tv96})")
        if market_iso is not None:
            sv96 = iso_to_sv96(market_iso)
            parts.append(f"set_sv96({sv96})")

        if stream:
            return self._shoot_streaming(parts, dng)
        else:
            return self._shoot_standard(parts, download_after, remove_after)

    def _shoot_streaming(self, setup_parts, dng):
        """Capture using remote capture (PTP commands 13/14)."""
        fmt = REMOTE_CAP_JPEG
        if dng:
            fmt = REMOTE_CAP_RAW | REMOTE_CAP_DNG_HDR

        script = "init_usb_capture({})".format(fmt)
        for part in setup_parts:
            script = part + "; " + script
        self.lua_execute(script, do_return=False)

        self.lua_execute("shoot()", do_return=False)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready, img_fmt = self._chdk.remote_capture_is_ready()
            if ready:
                return self._chdk.remote_capture_get_data(img_fmt)
            time.sleep(0.1)
        raise TimeoutError("Remote capture did not complete")

    def _shoot_standard(self, setup_parts, download, remove):
        """Capture to SD card, optionally download and delete."""
        script = "; ".join(setup_parts + ["shoot()"])
        self.lua_execute(script, do_return=False)
        time.sleep(2)  # wait for camera to write to SD

        if not download:
            return None

        # Find the most recent file — simplified approach
        result = self.lua_execute(
            "return os.listdir('A/DCIM')"
        )
        return None

    def upload_file(self, local_path, remote_path):
        """Upload a file to the camera.

        Args:
            local_path: Path on the host filesystem.
            remote_path: Destination path on camera.
        """
        with open(local_path, "rb") as f:
            data = f.read()
        self._chdk.upload_file(data, remote_path)

    def download_file(self, remote_path):
        """Download a file from the camera.

        Args:
            remote_path: Path on camera (e.g., 'A/OWN.TXT').

        Returns:
            File contents as bytes.
        """
        return self._chdk.download_file(remote_path)

    def get_frames(self):
        """Generator yielding live preview frames.

        Yields:
            Raw frame data bytes.
        """
        while True:
            try:
                data = self._chdk.get_display_data()
                if data:
                    yield data
            except PTPError:
                break

    def reconnect(self, wait=2.0):
        """Reconnect to the camera."""
        self._connected = False
        try:
            self._transport.close()
        except Exception:
            pass
        time.sleep(wait)
        self._transport.open()
        self._session.open()
        self._connected = True

    def close(self):
        """Close the connection to the camera."""
        self._connected = False
        try:
            self._session.close()
        except Exception:
            pass
        try:
            self._transport.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
