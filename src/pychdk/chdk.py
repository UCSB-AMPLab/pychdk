"""CHDK PTP extension protocol.

Implements the CHDK-specific commands multiplexed through PTP
opcode 0x9999. Handles script execution, message passing, file
transfer, live view, and remote capture.
"""
import struct
import time
from collections import namedtuple
from enum import IntEnum

from pychdk.ptp import OperationCode


class ChdkCommand(IntEnum):
    VERSION = 0
    GET_MEMORY = 1
    SET_MEMORY = 2
    CALL_FUNCTION = 3
    TEMP_DATA = 4
    UPLOAD_FILE = 5
    DOWNLOAD_FILE = 6
    EXECUTE_SCRIPT = 7
    SCRIPT_STATUS = 8
    SCRIPT_SUPPORT = 9
    READ_SCRIPT_MSG = 10
    WRITE_SCRIPT_MSG = 11
    GET_DISPLAY_DATA = 12
    REMOTE_CAPTURE_IS_READY = 13
    REMOTE_CAPTURE_GET_DATA = 14


class ScriptLanguage(IntEnum):
    LUA = 0
    UBASIC = 1


class ScriptDataType(IntEnum):
    UNSUPPORTED = 0
    NIL = 1
    BOOLEAN = 2
    INTEGER = 3
    STRING = 4
    TABLE = 5


class MessageType(IntEnum):
    NONE = 0
    ERR = 1
    RET = 2
    USER = 3


class ScriptErrorType(IntEnum):
    """ptp_chdk_script_error_type, the subtype of an ERR message.

    SCRIPT_RUNNING is an ExecuteScript startup status only, never a
    message subtype, per core/ptp.h.
    """
    NONE = 0
    COMPILE = 1
    RUN = 2
    SCRIPT_RUNNING = 0x1000


class ScriptFlag(IntEnum):
    NONE = 0
    NOKILL = 0x100
    FLUSH = 0x200


# Remote capture format bits
REMOTE_CAP_JPEG = 0x01
REMOTE_CAP_RAW = 0x02
REMOTE_CAP_DNG_HDR = 0x04

# Not a data type: the status PTP_CHDK_RemoteCaptureIsReady reports when
# init_usb_capture has not run (PTP_CHDK_CAPTURE_NOTSET in core/ptp.h).
REMOTE_CAP_NOTSET = 0x10000000

# A full-resolution still needs a few hundred chunks at most; this only
# exists so a camera that never clears the "more" flag cannot hang us.
MAX_CAPTURE_CHUNKS = 10000


ScriptMessage = namedtuple("ScriptMessage", ["msg_type", "data_type", "script_id", "value"])
ScriptMessage.__doc__ = """One message from the camera's script queue.

The data_type field carries whichever subtype the message type calls
for, as core/ptp.h defines it: a ScriptDataType for RET and USER
messages, and a ScriptErrorType for ERR. The field keeps its name for
the sake of callers that already read it.
"""


def _script_error_name(error_type):
    """Name a script error type for a message a human has to read.

    Args:
        error_type: Value of an ERR message's subtype, or an
            ExecuteScript startup status.

    Returns:
        Lowercase name, or the raw value in hex if CHDK sends one we
        do not know.
    """
    try:
        return ScriptErrorType(error_type).name.lower()
    except ValueError:
        return f"unknown 0x{int(error_type):x}"


def _as_signed32(value):
    """Reinterpret a PTP uint32 parameter as a signed 32-bit integer.

    PTP parameters are unsigned, so CHDK's -1 sentinels arrive as
    0xFFFFFFFF and have to be folded back before they are used.

    Args:
        value: Parameter value as received (unsigned).

    Returns:
        Integer in the range -2**31 .. 2**31 - 1.
    """
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        return value - 0x100000000
    return value


def _decode_script_value(data_type, data):
    """Decode a script value from raw bytes given its type."""
    if data_type == ScriptDataType.NIL:
        return None
    elif data_type == ScriptDataType.BOOLEAN:
        if len(data) >= 4:
            return struct.unpack_from("<I", data)[0] != 0
        return False
    elif data_type == ScriptDataType.INTEGER:
        if len(data) >= 4:
            return struct.unpack_from("<i", data)[0]
        return 0
    elif data_type == ScriptDataType.STRING:
        return data.decode("utf-8", errors="replace")
    elif data_type == ScriptDataType.TABLE:
        return data.decode("utf-8", errors="replace")
    return None


class ChdkPTP:
    """CHDK PTP extension command interface.

    Wraps a PTPSession and provides methods for each CHDK PTP
    subcommand.
    """

    def __init__(self, session):
        self._session = session

    def get_version(self):
        """Get CHDK PTP protocol version.

        Returns:
            Tuple of (major, minor) version numbers.
        """
        params, _ = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.VERSION],
            receive_data=False,
        )
        return params[0], params[1]

    def execute_script(self, script, language=ScriptLanguage.LUA,
                       flags=ScriptFlag.NONE):
        """Execute a script on the camera.

        Args:
            script: Script source code string.
            language: ScriptLanguage.LUA or ScriptLanguage.UBASIC.
            flags: ScriptFlag bitmask (NOKILL, FLUSH).

        CHDK returns the script id in param1 and a startup status from
        ptp_chdk_script_error_type in param2 (core/ptp.h). A nonzero
        status means the script never started, so the id is not one:
        waiting on it would only burn the caller's whole timeout.

        Returns:
            Script ID assigned by the camera.

        Raises:
            RuntimeError: If the camera refused to start the script.
        """
        script_bytes = script.encode("utf-8") + b"\x00"
        params, _ = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.EXECUTE_SCRIPT, language | flags],
            send_data=script_bytes,
        )
        status = params[1] if len(params) > 1 else 0
        if status:
            if status == ScriptErrorType.SCRIPT_RUNNING:
                reason = ("a script is already running and NOKILL was set, "
                          "so this one was refused")
            else:
                reason = f"{_script_error_name(status)} error"
            raise RuntimeError(f"Script did not start: {reason}")
        return params[0] if params else 0

    def get_script_status(self):
        """Check if a script is running and if messages are pending.

        Returns:
            Tuple of (is_running, has_messages).
        """
        params, _ = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.SCRIPT_STATUS],
            receive_data=False,
        )
        status = params[0] if params else 0
        return bool(status & 1), bool(status & 2)

    def read_script_message(self):
        """Read next message from the camera's script message queue.

        Response params contain message metadata:
          [msg_type, data_type, script_id, data_size]
        Data phase contains the raw value bytes.

        Param2 is the message subtype, and what it means depends on the
        message type: for RET and USER it is a ScriptDataType, but for
        ERR it is a ScriptErrorType, and the data phase is the error
        text rather than an encoded value. Reading an ERR's subtype as
        a data type silently threw that text away — the two enums
        overlap, so COMPILE decoded as NIL and RUN as BOOLEAN.

        Returns:
            ScriptMessage namedtuple. Its data_type field holds an
            error type when msg_type is ERR, a data type otherwise.
        """
        params, data = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.READ_SCRIPT_MSG],
            receive_data=True,
        )
        if not params or params[0] == MessageType.NONE:
            return ScriptMessage(MessageType.NONE, ScriptDataType.NIL, 0, None)
        msg_type = params[0]
        data_type = params[1] if len(params) > 1 else ScriptDataType.NIL
        script_id = params[2] if len(params) > 2 else 0
        if msg_type == MessageType.ERR:
            # The header guarantees at least one zero byte, even empty.
            value = data.decode("utf-8", errors="replace").rstrip("\x00")
        else:
            value = _decode_script_value(data_type, data)
        return ScriptMessage(msg_type, data_type, script_id, value)

    def write_script_message(self, message, script_id=0):
        """Send a message to a running script on the camera."""
        msg_bytes = message.encode("utf-8") + b"\x00"
        self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.WRITE_SCRIPT_MSG, script_id],
            send_data=msg_bytes,
        )

    def upload_file(self, data, remote_path):
        """Upload a file to the camera.

        The data phase packs the remote filename and file contents
        together: [4-byte filename length][filename][file data].

        Args:
            data: File contents as bytes.
            remote_path: Destination path on camera (e.g., 'A/OWN.TXT').
        """
        path_bytes = remote_path.encode("utf-8")
        payload = struct.pack("<I", len(path_bytes)) + path_bytes + data
        self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.UPLOAD_FILE],
            send_data=payload,
        )

    def download_file(self, remote_path):
        """Download a file from the camera.

        Args:
            remote_path: Path on camera (e.g., 'A/DCIM/100CANON/IMG_0001.JPG').

        Returns:
            File contents as bytes.
        """
        path_bytes = remote_path.encode("utf-8")
        self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.TEMP_DATA, 0],
            send_data=path_bytes,
        )
        _, data = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.DOWNLOAD_FILE],
            receive_data=True,
        )
        return data

    def get_display_data(self, flags=0):
        """Get live view / viewport frame data.

        Returns:
            Raw frame data bytes.
        """
        _, data = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.GET_DISPLAY_DATA, flags],
            receive_data=True,
        )
        return data

    def remote_capture_is_ready(self):
        """Check if a remote capture is ready for download.

        CHDK's PTP_CHDK_RemoteCaptureIsReady (core/ptp.h) returns a
        status in param1: 0 is not ready yet, 0x10000000 says remote
        capture is not initialized, and any other value is a bitmask of
        the PTP_CHDK_CAPTURE_* data types that are ready.

        The uninitialized status is reported rather than raised on,
        because on its own it does not mean anything is wrong. CHDK
        acknowledges a script as loaded and scheduled, not as run, so a
        poll can arrive before init_usb_capture has executed and get
        this answer perfectly legitimately. It is a failure only once
        the script has had its chance — which the caller knows and this
        method does not. A caller seeing it after the script has ended
        is looking at an initialization that never happened.

        Returns:
            Tuple of (is_ready, status). status is REMOTE_CAP_NOTSET
            when the camera reports remote capture uninitialized, 0
            when there is simply nothing ready yet, and otherwise a
            bitmask of the data types ready to download.
        """
        params, _ = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.REMOTE_CAPTURE_IS_READY],
            receive_data=False,
        )
        status = params[0] if params else 0
        if status == 0 or status == REMOTE_CAP_NOTSET:
            return False, status
        return True, status

    def remote_capture_get_chunk(self, format_flag):
        """Fetch one chunk of a remote capture.

        CHDK's PTP_CHDK_RemoteCaptureGetData handler (core/ptp.c)
        answers one chunk per transaction and describes it in the
        response parameters:

          * param1 — the chunk's size in bytes;
          * param2 — 1 while further chunks follow, 0 on the last one;
          * param3 — "seek required to pos (-1 = no seek)", in the
            header's words. No seek means the chunk continues from the
            current write position, which is not the same as the end of
            the file once a chunk has seeked backwards. Parameters are
            unsigned, so -1 arrives as 0xFFFFFFFF and is folded back
            here.

        A camera that sends fewer parameters is read as a single
        appended chunk the size of the data phase.

        Args:
            format_flag: Which format to download (JPEG=1, RAW=2, DNG_HDR=4).

        Returns:
            Tuple of (chunk_bytes, more, position), where more is a bool
            and position is a sign-corrected int (-1 means no seek).
        """
        params, data = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.REMOTE_CAPTURE_GET_DATA, format_flag],
            receive_data=True,
        )
        params = params or []
        size = params[0] if len(params) > 0 else len(data)
        more = bool(params[1]) if len(params) > 1 else False
        position = _as_signed32(params[2]) if len(params) > 2 else -1
        # Trust the data phase when the camera claims more than it sent.
        if 0 <= size < len(data):
            data = data[:size]
        return data, more, position

    def remote_capture_get_data(self, format_flag):
        """Download remote capture image data.

        Loops over remote_capture_get_chunk until the camera clears its
        "more" flag, following the same write cursor a file would. A
        chunk that asks for a seek moves the cursor; every chunk is
        written at the cursor and advances it by its own length. That
        is what "-1 = no seek" means: the chunk continues from wherever
        the last one ended, which is only the end of the file while no
        chunk has seeked backwards.

        Args:
            format_flag: Which format to download (JPEG=1, RAW=2, DNG_HDR=4).

        Returns:
            Image data as bytes.

        Raises:
            RuntimeError: If the camera never clears its "more" flag.
        """
        image = bytearray()
        cursor = 0
        for _ in range(MAX_CAPTURE_CHUNKS):
            chunk, more, position = self.remote_capture_get_chunk(format_flag)
            if position >= 0:
                cursor = position
            end = cursor + len(chunk)
            if len(image) < end:
                image.extend(bytes(end - len(image)))
            image[cursor:end] = chunk
            cursor = end
            if not more:
                return bytes(image)
        raise RuntimeError(
            f"Remote capture did not end after {MAX_CAPTURE_CHUNKS} chunks"
        )

    def wait_for_script(self, timeout=30.0, script_id=None):
        """Wait until no script is running on the camera.

        Reads waiting messages as it goes, rather than watching only
        the running flag: a script that starts and then fails clears
        that flag like any other, so waiting without reading the queue
        reported a failed capture as a clean one and left the error
        behind for the next caller to trip over.

        Args:
            timeout: Max seconds to wait for the script to finish.
            script_id: Only raise on errors from this script. Starting
                a script does not flush the queue, so an error left by
                a previous one would otherwise fail this one. None
                matches any script, which is only right when the caller
                has no id to match.

        Raises:
            RuntimeError: If the script reported an error.
            TimeoutError: If the script is still running at the deadline.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            running, has_msgs = self.get_script_status()
            if has_msgs:
                msg = self.read_script_message()
                is_ours = script_id is None or msg.script_id == script_id
                if is_ours and msg.msg_type == MessageType.ERR:
                    kind = _script_error_name(msg.data_type)
                    raise RuntimeError(f"Script error ({kind}): {msg.value}")
                continue
            if not running:
                return
            time.sleep(0.5)
        raise TimeoutError(f"Script still running after {timeout}s")

    def drain_messages(self):
        """Drain all pending messages from the script message queue."""
        for _ in range(50):
            _, has_msgs = self.get_script_status()
            if not has_msgs:
                return
            self.read_script_message()

    def execute_lua_wait(self, script, timeout=10.0):
        """Execute a Lua script and wait for the return value.

        Drains stale messages first, then executes the script and
        waits for its specific return message (matched by script_id).

        Args:
            script: Lua script string. Should use 'return' for a value.
            timeout: Max seconds to wait for completion.

        Returns:
            The script's return value (Python type).
        """
        self.drain_messages()
        script_id = self.execute_script(script)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            running, has_msgs = self.get_script_status()
            if has_msgs:
                msg = self.read_script_message()
                # Skip messages from older scripts
                if msg.script_id != script_id:
                    continue
                if msg.msg_type == MessageType.RET:
                    return msg.value
                if msg.msg_type == MessageType.ERR:
                    kind = _script_error_name(msg.data_type)
                    raise RuntimeError(f"Script error ({kind}): {msg.value}")
            if not running and not has_msgs:
                return None
            time.sleep(0.05)
        raise TimeoutError(f"Script did not complete within {timeout}s")
