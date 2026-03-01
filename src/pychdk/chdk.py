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


class ScriptFlag(IntEnum):
    NONE = 0
    NOKILL = 0x100
    FLUSH = 0x200


# Remote capture format bits
REMOTE_CAP_JPEG = 0x01
REMOTE_CAP_RAW = 0x02
REMOTE_CAP_DNG_HDR = 0x04


ScriptMessage = namedtuple("ScriptMessage", ["msg_type", "data_type", "script_id", "value"])


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

        Returns:
            Script ID assigned by the camera.
        """
        script_bytes = script.encode("utf-8") + b"\x00"
        params, _ = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.EXECUTE_SCRIPT, language | flags],
            send_data=script_bytes,
        )
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

        Returns:
            ScriptMessage namedtuple.
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

        Args:
            data: File contents as bytes.
            remote_path: Destination path on camera (e.g., 'A/OWN.TXT').
        """
        # Upload protocol: send remote path via TEMP_DATA, then file via UPLOAD_FILE
        path_bytes = remote_path.encode("utf-8") + b"\x00"
        self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.TEMP_DATA, 0],
            send_data=path_bytes,
        )
        self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.UPLOAD_FILE],
            send_data=data,
        )

    def download_file(self, remote_path):
        """Download a file from the camera.

        Args:
            remote_path: Path on camera (e.g., 'A/DCIM/100CANON/IMG_0001.JPG').

        Returns:
            File contents as bytes.
        """
        path_bytes = remote_path.encode("utf-8") + b"\x00"
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

        Returns:
            Tuple of (is_ready, image_format).
        """
        params, _ = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.REMOTE_CAPTURE_IS_READY],
            receive_data=False,
        )
        if not params or params[0] == 0:
            return False, 0
        return True, params[0]

    def remote_capture_get_data(self, format_flag):
        """Download remote capture image data.

        Args:
            format_flag: Which format to download (JPEG=1, RAW=2, DNG_HDR=4).

        Returns:
            Image data as bytes.
        """
        _, data = self._session.transaction(
            OperationCode.CHDK,
            params=[ChdkCommand.REMOTE_CAPTURE_GET_DATA, format_flag],
            receive_data=True,
        )
        return data

    def wait_for_script(self, timeout=30.0):
        """Wait until no script is running on the camera."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            running, _ = self.get_script_status()
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
                    raise RuntimeError(f"Script error: {msg.value}")
            if not running and not has_msgs:
                return None
            time.sleep(0.05)
        raise TimeoutError(f"Script did not complete within {timeout}s")
