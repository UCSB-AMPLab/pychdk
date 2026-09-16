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
    LV_TFR_VIEWPORT,
    MessageType,
    _script_error_name,
    REMOTE_CAP_JPEG,
    REMOTE_CAP_NOTSET,
    REMOTE_CAP_RAW,
    REMOTE_CAP_DNG_HDR,
)
from pychdk.util import shutter_to_tv96


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
        """Claim the interface and open a session, or claim nothing.

        Until the device is tracked there is nothing for the caller to
        close: a constructor that raised here left the interface
        claimed with no object to release it, so a host retrying
        enumeration piled up claims on a port until the camera was
        unplugged. Anything that fails past the claim gives it back —
        including a failure inside the transport's own open, which can
        hold a claim and still raise.
        """
        try:
            self._transport.open()
            self._session.open()
            self._connected = True
            _open_devices.add(self)
            # Re-register so our cleanup runs before any pyusb finalizers
            # that were registered during device creation (atexit is LIFO).
            atexit.register(_cleanup_all)
        except BaseException:
            self._connected = False
            _open_devices.discard(self)
            try:
                self._transport.close()
            except Exception:
                pass
            raise

    @property
    def is_connected(self):
        return self._connected

    @property
    def last_capture_chunks(self):
        """How many chunks the last streamed capture arrived in.

        Read after shoot(stream=True) rather than returned by it: the
        return value is the picture, and MultiCam.shoot promises a list
        of those, one per camera. The count lives per device, so after
        a MultiCam shot each camera's own figure is on its entry in
        MultiCam.cameras.

        Zero means no chunk arrived, not that no capture was tried.
        Read it from the thread that took the shot, or once that thread
        has finished: MultiCam shoots on a pool, and a reader looking
        at another worker's device mid-capture sees a partial count.
        """
        return self._chdk.last_capture_chunks

    def switch_mode(self, mode):
        """Switch camera to 'record' or 'play' mode.

        Waits for the switch_mode_usb script to finish, then polls
        get_mode() to confirm the physical switch completed.

        The poll is Lua, and the two CHDK script languages disagree
        about this call. CHDK's Lua get_mode() pushes three values —
        is_record, is_video, mode — where is_record is
        `!camera_info.state.mode_play`, so it is TRUE in record mode
        (luaCB_get_mode, modules/luascript.c). CHDK's own wait idiom
        relies on that polarity: the set_record documentation beside it
        says to spin on `while not get_mode() do sleep(10) end` until
        record mode arrives. uBASIC's get_mode is the other way round,
        returning 0 for record, 1 for play and 2 for video record
        (lib/ubasic/ubasic.c), and it is not what this call speaks.
        ChdkPTP.execute_lua_wait returns the first RET value only, so
        what lands in `current` is is_record.

        A switch that is never confirmed raises rather than returning
        quietly, so a wrong answer here cannot look like a right one. A
        poll that comes back with no answer at all is not an answer
        either: it is retried, not read as false.

        This polarity is read from the CHDK sources named above. It has
        not been checked against a camera.

        Raises:
            RuntimeError: If the camera never reports the requested
                mode within the retries.
        """
        want_record = mode == "record"
        mode_val = 1 if want_record else 0
        script_id = self._chdk.execute_script(f"switch_mode_usb({mode_val})")
        # Wait for the script to finish before polling, matching its id
        # so an error left by an earlier shot is not blamed on this.
        self._chdk.wait_for_script(timeout=5, script_id=script_id)
        # Give the camera time to physically switch (lens motor, etc.)
        time.sleep(1)
        current = None
        for _ in range(8):
            current = self.lua_execute("return get_mode()")
            if current is None:
                # execute_lua_wait returns None when the script ends with
                # no RET message, so this is the absence of an answer, not
                # an answer of false. Reading it as false would confirm
                # play mode on a camera that said nothing at all — and
                # since bool(None) is False, a plain bool() here did.
                time.sleep(0.5)
                continue
            in_record = bool(current)
            if in_record == want_record:
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"The camera did not switch to {mode} mode: get_mode() last "
            f"reported is_record={current!r}"
        )

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
              stream=False):
        """Capture a photo.

        Args:
            shutter_speed: Shutter speed in seconds (e.g., 1/100).
                Converted to APEX96 and sent with set_tv96_direct,
                which applies the value as given rather than snapping
                it to one of the camera's own shutter speeds.
            market_iso: ISO as it appears in the camera's own menu —
                100, 200, 400 and so on. It is converted to a real
                sensitivity ON THE CAMERA and applied as a script
                exposure override:

                    set_sv96(sv96_market_to_real(iso_to_sv96(N)))

                Each step is CHDK's own. iso_to_sv96 is the APEX96
                conversion, whose source comment reads "equivalent to
                (short)(log2(iso/3.125)*96+0.5) [APEX equation]";
                sv96_market_to_real subtracts SV96_MARKET_OFFSET, which
                is per-camera and overridable in platform_camera.h; and
                set_sv96 takes the real value. Nothing is converted
                here, and the offset never touches this library.

                Two things make this the right call rather than
                set_iso_mode, which also takes a menu number:

                First, priority. At capture CHDK applies a script's
                deferred sv96 first and falls back to its own
                configured ISO override only if none was set
                (shooting_expo_param_override_thumb, core/shooting.c).
                set_sv96 outside a shot populates that deferred value
                (photo_param_put_off.sv96); set_iso_mode does not. On a
                card with CHDK's ISO override enabled, set_iso_mode
                would be silently overridden and the caller's ISO lost.

                Second, exactness. set_iso_mode snaps to the nearest
                entry in the camera's iso_table; this path applies the
                value asked for.

                Passing a menu number straight to set_sv96 as though it
                were already real — which this argument used to do —
                makes the camera about 0.7 of a stop more sensitive
                than asked, silently.
            dng: Request DNG. Not implemented on either path: streaming
                refuses it, and the card path ignores it.
            stream: If True, use remote capture (direct USB transfer).

        Returns:
            The JPEG as bytes when stream=True. Otherwise None: the
            camera shoots to its own SD card and nothing is fetched
            back: shoot() does not discover or return the saved
            filename. download_file fetches a card file by path, and
            CHDK's own Lua (get_image_dir, the exposure counter, a
            directory listing) can be used to find one — none of that
            happens here.
        """
        parts = []
        if shutter_speed is not None:
            tv96 = shutter_to_tv96(shutter_speed)
            parts.append(f"set_tv96_direct({tv96})")
        if market_iso is not None:
            # Converted on the camera, so the per-camera market-to-real
            # offset is the camera's own. See shoot() for why this is
            # set_sv96 rather than set_iso_mode.
            parts.append(
                f"set_sv96(sv96_market_to_real(iso_to_sv96({market_iso})))"
            )

        if stream:
            return self._shoot_streaming(parts, dng)
        else:
            return self._shoot_standard(parts)

    def _shoot_streaming(self, setup_parts, dng):
        """Capture using remote capture (PTP commands 13/14).

        The chunk count is zeroed here, at the attempt, rather than
        where the download begins: a capture refused, or one that never
        becomes ready, would otherwise keep reporting the chunks of the
        capture before it.

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
        self._chdk.reset_capture_chunks()

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
            # and read differently in a bench log: a script that ended
            # with remote capture not initialized, versus one still
            # going after we gave up waiting. Neither proves the camera
            # cannot do this, and the first does not even prove
            # init_usb_capture never ran — see
            # ChdkPTP.remote_capture_is_ready for why.
            if status == REMOTE_CAP_NOTSET:
                if not running:
                    raise RuntimeError(
                        "The capture script ended and remote capture is "
                        "not initialized: either init_usb_capture never "
                        "ran, or it ran and the capture was cancelled "
                        "afterwards — CHDK cancels on its own download "
                        "timeout and on a transfer error, and reports "
                        "both the same way as never having initialized"
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

    def _shoot_standard(self, setup_parts):
        """Capture to the camera's SD card and leave it there.

        Nothing comes back. Earlier versions took download_after and
        remove_after options; neither was implemented — the download
        listed A/DCIM, threw the listing away and returned None, and
        the delete did nothing at all — so they were removed rather
        than left as a promise. What is missing is narrow: this method
        does not discover or return the saved filename. download_file
        fetches a card file by path, and CHDK's Lua can be asked where
        images go. Whether shoot() should do that for the caller is a
        bench question, not one this library has answered.

        Returns:
            None.
        """
        script = "; ".join(setup_parts + ["shoot()"])
        script_id = self._chdk.execute_script(script)
        # Wait for the shoot script to finish (shutter + SD write),
        # matching its id so a previous shot's error is not ours.
        self._chdk.wait_for_script(timeout=30, script_id=script_id)
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

    def get_frames(self, flags=LV_TFR_VIEWPORT):
        """Generator yielding live preview frames.

        The transfer flags are not optional. CHDK's live_view_get_data
        adds each data block only if the matching LV_TFR_* bit was
        asked for, so a request with no flag set returns the header and
        the framebuffer descriptions and no pixels (core/live_view.c);
        earlier versions of this generator sent none and could yield
        frames with nothing in them. The default asks for the viewport,
        which is the live image.

        Args:
            flags: Bitmask of LV_TFR_* values from pychdk.chdk.
                Defaults to LV_TFR_VIEWPORT.

        Yields:
            Raw frame data bytes — CHDK's live view payload, which is
            the header, the framebuffer descriptions and then whichever
            blocks were requested. This library does not parse it.
        """
        while True:
            try:
                data = self._chdk.get_display_data(flags)
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
        # Same claim, same rollback: a reopen that fails mid-session
        # leaks exactly as a failed construction did.
        self._open()

    def close(self):
        """Close the connection to the camera.

        Safe to call more than once in sequence.

        Concurrently it is safe in one half and not the other, and the
        halves are worth keeping apart. Releasing the USB interface is
        serialised by pyusb itself, so two closers cannot double-release
        it — PTPDevice.close carries the citation. Closing the PTP
        session is not serialised: this sends a close over the wire, and
        two threads can both find the session open and both send one,
        because nothing here guards that. So a host that shares one
        device across threads has to serialise its own teardown.

        This library reaches that state on its own. MultiCam does give
        each worker its own device, but the teardown path is shared:
        _cleanup_all closes every open device from the main thread, and
        it runs from the SIGINT/SIGTERM handler. That handler can close
        a device while a MultiCam worker is inside shoot() on it, and
        nothing here serialises the two.

        The atexit hook is a different case, and the difference is
        worth keeping straight: concurrent.futures registers its pool
        shutdown through threading._register_atexit, which joins the
        worker threads during threading._shutdown, and that runs before
        atexit handlers do. So a MultiCam worker has already finished
        by the time _cleanup_all runs at exit. A daemon thread of the
        host's own is not joined that way and could still be inside
        shoot() — but that is the host's thread, not one this library
        started.

        A plain lock is still not an obvious fix for the signal case,
        because close() also runs at interpreter shutdown, where a lock
        held by a thread being torn down would turn a clean exit into a
        hang. It is an open design question, not a solved one.
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

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
