"""Tests for CHDK PTP extension protocol."""
import struct
from unittest.mock import MagicMock
import pytest
from pychdk.chdk import (
    ChdkPTP,
    ChdkCommand,
    ScriptLanguage,
    ScriptDataType,
    MessageType,
    _decode_script_value,
)
from pychdk.ptp import ResponseCode, PTPContainer, ContainerType


class TestChdkPTP:
    def _make_chdk(self):
        mock_session = MagicMock()
        return ChdkPTP(mock_session), mock_session

    def test_get_version(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([2, 6], b"")
        major, minor = chdk.get_version()
        assert major == 2
        assert minor == 6
        session.transaction.assert_called_once_with(
            0x9999, params=[ChdkCommand.VERSION], receive_data=False
        )

    def test_execute_script_lua(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([0], b"")
        chdk.execute_script("return 1")
        call_args = session.transaction.call_args
        assert call_args[1]["send_data"] == b"return 1\x00"

    def test_get_script_status(self):
        chdk, session = self._make_chdk()
        # Bit 0 = script running, bit 1 = messages pending
        session.transaction.return_value = ([0b11], b"")
        running, has_msgs = chdk.get_script_status()
        assert running is True
        assert has_msgs is True

    def test_read_script_message_integer(self):
        chdk, session = self._make_chdk()
        # Response params: [msg_type=RET, data_type=INTEGER, script_id=1, size=4]
        # Data: the integer 42
        session.transaction.return_value = (
            [MessageType.RET, ScriptDataType.INTEGER, 1, 4],
            struct.pack("<i", 42),
        )
        msg = chdk.read_script_message()
        assert msg.msg_type == MessageType.RET
        assert msg.value == 42

    def test_read_script_message_string(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = (
            [MessageType.RET, ScriptDataType.STRING, 1, 5],
            b"hello",
        )
        msg = chdk.read_script_message()
        assert msg.value == "hello"

    def test_read_script_message_none(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = (
            [MessageType.NONE],
            b"",
        )
        msg = chdk.read_script_message()
        assert msg.msg_type == MessageType.NONE
        assert msg.value is None


class TestDecodeScriptValue:
    def test_integer(self):
        data = struct.pack("<i", 42)
        assert _decode_script_value(ScriptDataType.INTEGER, data) == 42

    def test_boolean_true(self):
        data = struct.pack("<I", 1)
        assert _decode_script_value(ScriptDataType.BOOLEAN, data) is True

    def test_boolean_false(self):
        data = struct.pack("<I", 0)
        assert _decode_script_value(ScriptDataType.BOOLEAN, data) is False

    def test_nil(self):
        assert _decode_script_value(ScriptDataType.NIL, b"") is None

    def test_string(self):
        assert _decode_script_value(ScriptDataType.STRING, b"hello") == "hello"
