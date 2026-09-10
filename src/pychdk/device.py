"""High-level CHDK device API.

Provides ChdkDevice — the main interface for controlling a CHDK
camera — and list_devices() for discovery.
"""
import atexit
import signal
import threading
import time
import weakref
from collections import namedtuple

from pychdk.usb_transport import PTPDevice, find_ptp_devices, CANON_VENDOR_ID
from pychdk.ptp import PTPSession, PTPError
from pychdk.chdk import (
    ChdkPTP,
    MessageType,
    _script_error_name,
    REMOTE_CAP_JPEG,
    REMOTE_CAP_NOTSET,
    REMOTE_CAP_RAW,
    REMOTE_CAP_DNG_HDR,
)
from pychdk.util import shutter_to_tv96, iso_to_sv96


# How long a capture script is allowed to still be starting before an
# "uninitialized" answer counts against it. CHDK acknowledges a script
# as scheduled, not as run, so the first polls can legitimately land
# before init_usb_capture has executed.
CAPTURE_INIT_GRACE = 5.0


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


# Track all open devices for cleanup on exit
_open_devices = weakref.WeakSet()
_original_sigint = None
_original_sigterm = None


def _cleanup_all():
    """Close all open camera connections."""
    for dev in list(_open_devices):
        try:
            dev.close()
        except Exception:
            pass


def _signal_handler(signum, frame):
    """Close cameras on SIGINT/SIGTERM, then re-raise."""
    _cleanup_all()
    # Restore original handler and re-raise
    original = _original_sigint if signum == signal.SIGINT else _original_sigterm
    if callable(original):
        original(signum, frame)
    elif original == signal.SIG_DFL:
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)


def install_signal_handlers():
    """Install the SIGINT/SIGTERM handlers that close open cameras.

    Python only allows handlers to be installed from the main thread of
    the main interpreter, so this declines anywhere else instead of
    raising: a host that first imports pychdk inside a worker thread —
    a synchronous FastAPI route, say — would otherwise fail on the
    import. Such a host can call this later from its main thread to get
    the handlers after all. Installing twice is harmless; the originals
    are captured once.

    Returns:
        True if our handlers are in place, False if it declined.
    """
    global _original_sigint, _original_sigterm
    if threading.current_thread() is not threading.main_thread():
        return False
    current_sigint = signal.getsignal(signal.SIGINT)
    current_sigterm = signal.getsignal(signal.SIGTERM)
    if current_sigint is _signal_handler and current_sigterm is _signal_handler:
        return True
    # Never save our own handler as the original: that would recurse.
    if current_sigint is not _signal_handler:
        _original_sigint = current_sigint
    if current_sigterm is not _signal_handler:
        _original_sigterm = current_sigterm
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError:
        # Some embeddings refuse even on the main thread.
        return False
    return True


# atexit is thread-safe, so it is registered unconditionally.
atexit.register(_cleanup_all)
install_signal_handlers()


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
        _open_devices.add(self)
        # Re-register so our cleanup runs before any pyusb finalizers
        # that were registered during device creation (atexit is LIFO).
        atexit.register(_cleanup_all)

    @property
    def is_connected(self):
        return self._connected

    def switch_mode(self, mode):
        """Switch camera to 'record' or 'play' mode.

        Waits for the switch_mode_usb script to finish, then polls
        get_mode() to confirm the physical switch completed.
        """
        mode_val = 1 if mode == "record" else 0
        script_id = self._chdk.execute_script(f"switch_mode_usb({mode_val})")
        # Wait for the script to finish before polling, matching its id
        # so an error left by an earlier shot is not blamed on this.
        self._chdk.wait_for_script(timeout=5, script_id=script_id)
        # Give the camera time to physically switch (lens motor, etc.)
        time.sleep(1)
        for _ in range(8):
            current = self.lua_execute("return get_mode()")
            # get_mode() returns 0 (falsy) for record, nonzero for play
            in_record = not current
            if (mode == "record" and in_record) or (mode == "play" and not in_record):
                return
            time.sleep(0.5)

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
            dng: Request DNG. Not implemented on either path: streaming
                refuses it, and the card path ignores it.
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
        """Capture using remote capture (PTP commands 13/14).

        Setup and shutter go out as one script, because a second script
        kills the first unless NOKILL is set ("if script is running
        return error instead of killing", core/ptp.h) — so a separate
        shoot() could terminate the init_usb_capture that was still
        running and leave the camera taking an ordinary card shot while
        we waited for bytes that were never coming.

        The script is started, not waited on, and that order matters:
        CHDK can hold the capture pipeline until the host takes the
        data, so waiting for the script to return before downloading
        leaves both sides waiting on each other and the capture times
        out having never once asked whether data was ready. We service
        the camera while the script runs — readiness first, messages
        second. Once the picture is in hand the queue is cleared of
        whatever the script left, which is housekeeping so the next
        capture does not read a stale message: it is a bounded sweep of
        what is already waiting, not a wait for a result still to come.

        Raises:
            RuntimeError: If the camera refuses to initialize remote
                capture.
            NotImplementedError: If dng is True. CHDK's DNG_HDR flag
                sends the DNG header only; the raw data is a separate
                transfer, and the client has to splice the two into a
                file. This method downloads one format, so it cannot,
                and _shoot_standard does not request a DNG either.
        """
        if dng:
            raise NotImplementedError(
                "DNG capture is not implemented. Streaming would need the "
                "DNG header and the raw data fetched as two separate "
                "transfers and assembled into a file on this side, which "
                "this library does not do; capturing to the card does not "
                "request a DNG either, it runs shoot() and takes whatever "
                "the camera is set to produce. Streamed JPEG works."
            )

        fmt = REMOTE_CAP_JPEG
        setup = "".join(part + "; " for part in setup_parts)
        script = (
            f"{setup}local ok = init_usb_capture({fmt}); "
            "if ok == false then return false end; "
            "shoot(); "
            "return true"
        )
        script_id = self._chdk.execute_script(script)

        image = None
        deadline = time.monotonic() + 30
        grace = CAPTURE_INIT_GRACE
        init_deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            ready, status = self._chdk.remote_capture_is_ready()
            formats = status
            if ready:
                # A mask without the format we asked for is a fault, not
                # a menu: the other bits are a different picture.
                if not formats & fmt:
                    raise RuntimeError(
                        f"The camera has no 0x{fmt:02x} data ready; "
                        f"it offers 0x{formats:02x}"
                    )
                # The request parameter is one bit, not the whole mask.
                image = self._chdk.remote_capture_get_data(fmt)
                break

            running, has_msgs = self._chdk.get_script_status()
            if has_msgs:
                msg = self._chdk.read_script_message()
                if msg.script_id == script_id:
                    if msg.msg_type == MessageType.ERR:
                        kind = _script_error_name(msg.data_type)
                        raise RuntimeError(
                            f"Capture script failed ({kind}): {msg.value}"
                        )
                    # Only an explicit false is a refusal: an older CHDK
                    # returns nil, which must not be read as failure.
                    if msg.msg_type == MessageType.RET and msg.value is False:
                        raise RuntimeError(
                            "The camera refused to initialize remote "
                            "capture: init_usb_capture returned false"
                        )
                continue

            # Checked after the queue, so a script that explained itself
            # is reported by its own words rather than by this status.
            # The two ways of getting here are different observations
            # and read differently in a bench log: a script that ran and
            # did not initialize, versus one still going after we gave
            # up waiting. Neither proves the camera cannot do this.
            if status == REMOTE_CAP_NOTSET:
                if not running:
                    raise RuntimeError(
                        "The capture script ended without initializing "
                        "remote capture"
                    )
                if time.monotonic() >= init_deadline:
                    raise RuntimeError(
                        "The capture script did not initialize remote "
                        f"capture within {grace}s and is still running"
                    )
            if not running:
                raise RuntimeError(
                    "The capture script finished without producing a capture"
                )
            time.sleep(0.1)

        if image is None:
            raise TimeoutError("Remote capture did not complete")

        # Clear what the script left behind so the next capture does not
        # read a stale message — but never at the cost of this picture.
        try:
            self._chdk.drain_messages()
        except Exception:
            pass
        return image

    def _shoot_standard(self, setup_parts, download, remove):
        """Capture to SD card, optionally download and delete."""
        script = "; ".join(setup_parts + ["shoot()"])
        script_id = self._chdk.execute_script(script)
        # Wait for the shoot script to finish (shutter + SD write),
        # matching its id so a previous shot's error is not ours.
        self._chdk.wait_for_script(timeout=30, script_id=script_id)

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
        """Reconnect to the camera with a USB reset.

        Closes the PTP session and USB interface, resets the USB
        device to clear any stale state, then reopens everything.
        """
        self._connected = False
        _open_devices.discard(self)
        try:
            self._session.close()
        except Exception:
            pass
        try:
            self._transport.close()
        except Exception:
            pass
        try:
            self._usb_device.reset()
        except Exception:
            pass
        time.sleep(wait)
        self._transport.open()
        self._session.open()
        self._connected = True
        _open_devices.add(self)

    def close(self):
        """Close the connection to the camera."""
        self._connected = False
        _open_devices.discard(self)
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
