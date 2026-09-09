"""Tests for CHDK PTP extension protocol."""
import struct
from unittest.mock import MagicMock, patch
import pytest
from pychdk.chdk import (
    ChdkPTP,
    ChdkCommand,
    ScriptLanguage,
    ScriptDataType,
    ScriptErrorType,
    MessageType,
    ScriptMessage,
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


class TestScriptErrors:
    """An ERR message's subtype is an error type, not a data type."""

    def _make_chdk(self):
        mock_session = MagicMock()
        return ChdkPTP(mock_session), mock_session

    def test_a_compile_error_keeps_its_text_and_kind(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = (
            [MessageType.ERR, ScriptErrorType.COMPILE, 7, 28],
            b"attempt to call a nil value\x00",
        )
        msg = chdk.read_script_message()
        assert msg.data_type == ScriptErrorType.COMPILE
        assert msg.value == "attempt to call a nil value"

    def test_a_run_error_keeps_its_text_and_kind(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = (
            [MessageType.ERR, ScriptErrorType.RUN, 7, 15],
            b"bad argument\x00",
        )
        msg = chdk.read_script_message()
        assert msg.data_type == ScriptErrorType.RUN
        assert msg.value == "bad argument"

    def test_an_error_with_no_text_is_empty_but_still_named(self):
        chdk, session = self._make_chdk()
        # The header promises at least one zero byte even with no message.
        session.transaction.return_value = (
            [MessageType.ERR, ScriptErrorType.RUN, 7, 1],
            b"\x00",
        )
        msg = chdk.read_script_message()
        assert msg.data_type == ScriptErrorType.RUN
        assert msg.value == ""

    def test_a_return_value_with_a_colliding_subtype_still_decodes(self):
        chdk, session = self._make_chdk()
        # Subtype 2 is BOOLEAN for a RET and ERRTYPE_RUN for an ERR.
        session.transaction.return_value = (
            [MessageType.RET, ScriptDataType.BOOLEAN, 7, 4],
            struct.pack("<I", 1),
        )
        msg = chdk.read_script_message()
        assert msg.value is True

    def test_execute_lua_wait_reports_the_kind_and_the_text(self):
        chdk, session = self._make_chdk()
        failure = ScriptMessage(
            MessageType.ERR, ScriptErrorType.COMPILE, 7,
            "attempt to call a nil value",
        )
        with patch.object(chdk, "drain_messages"), \
             patch.object(chdk, "execute_script", return_value=7), \
             patch.object(chdk, "get_script_status", return_value=(True, True)), \
             patch.object(chdk, "read_script_message", return_value=failure):
            with pytest.raises(
                RuntimeError,
                match=r"Script error \(compile\): attempt to call a nil value",
            ):
                chdk.execute_lua_wait("bogus(")


class TestScriptStartupStatus:
    """ExecuteScript reports in param2 whether the script actually ran."""

    def _make_chdk(self):
        mock_session = MagicMock()
        return ChdkPTP(mock_session), mock_session

    def test_a_compile_error_refuses_the_script(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([0, ScriptErrorType.COMPILE], b"")
        with pytest.raises(RuntimeError, match="compile"):
            chdk.execute_script("bogus(")

    def test_a_run_error_refuses_the_script(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([0, ScriptErrorType.RUN], b"")
        with pytest.raises(RuntimeError, match="run"):
            chdk.execute_script("error()")

    def test_nokill_refusal_says_what_it_means(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = (
            [0, ScriptErrorType.SCRIPT_RUNNING], b"",
        )
        with pytest.raises(RuntimeError, match="already running"):
            chdk.execute_script("shoot()")

    def test_a_zero_status_returns_the_script_id(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([7, ScriptErrorType.NONE], b"")
        assert chdk.execute_script("return 1") == 7

    def test_a_refused_script_fails_without_waiting(self):
        chdk, session = self._make_chdk()
        session.transaction.side_effect = [
            ([0], b""),                             # drain: nothing pending
            ([0, ScriptErrorType.COMPILE], b""),    # execute: refused
        ]
        with pytest.raises(RuntimeError, match="compile"):
            chdk.execute_lua_wait("bogus(", timeout=30)
        # The drain and the execute, and no polling loop after them.
        assert session.transaction.call_count == 2


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


class TestRemoteCaptureIsReady:
    def _make_chdk(self):
        mock_session = MagicMock()
        return ChdkPTP(mock_session), mock_session

    def test_uninitialized_capture_raises(self):
        chdk, session = self._make_chdk()
        # 0x10000000 is PTP_CHDK_CAPTURE_NOTSET, not a data type bitmask.
        session.transaction.return_value = ([0x10000000], b"")
        with pytest.raises(RuntimeError, match="init_usb_capture"):
            chdk.remote_capture_is_ready()

    def test_status_zero_is_not_ready(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([0], b"")
        assert chdk.remote_capture_is_ready() == (False, 0)

    def test_a_nonzero_status_is_the_ready_bitmask(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([0x03], b"")
        assert chdk.remote_capture_is_ready() == (True, 0x03)


class TestRemoteCaptureGetData:
    """Chunk assembly for PTP_CHDK_RemoteCaptureGetData."""

    def _make_chdk(self):
        mock_session = MagicMock()
        return ChdkPTP(mock_session), mock_session

    def test_two_chunks_are_joined_in_order(self):
        chdk, session = self._make_chdk()
        # [size, more, position]; position -1 means "append".
        session.transaction.side_effect = [
            ([4, 1, 0xFFFFFFFF], b"AAAA"),
            ([4, 0, 0xFFFFFFFF], b"BBBB"),
        ]
        assert chdk.remote_capture_get_data(1) == b"AAAABBBB"
        assert session.transaction.call_count == 2

    def test_explicit_position_places_a_late_chunk_first(self):
        chdk, session = self._make_chdk()
        # Chunk B arrives first but belongs at offset 8.
        session.transaction.side_effect = [
            ([4, 1, 8], b"BBBB"),
            ([8, 0, 0], b"AAAAAAAA"),
        ]
        assert chdk.remote_capture_get_data(1) == b"AAAAAAAABBBB"

    def test_position_ffffffff_is_no_seek_not_a_huge_offset(self):
        chdk, session = self._make_chdk()
        session.transaction.side_effect = [
            ([2, 1, 0xFFFFFFFF], b"hi"),
            ([5, 0, 0xFFFFFFFF], b"there"),
        ]
        assert chdk.remote_capture_get_data(1) == b"hithere"

    def test_single_chunk_capture_is_returned_unchanged(self):
        chdk, session = self._make_chdk()
        session.transaction.side_effect = [
            ([5, 0, 0xFFFFFFFF], b"\xff\xd8\xff\xe0\x00"),
        ]
        assert chdk.remote_capture_get_data(1) == b"\xff\xd8\xff\xe0\x00"

    def test_an_unseeked_chunk_continues_from_the_write_cursor(self):
        chdk, session = self._make_chdk()
        # CHDK seeks back to 0 to rewrite the first two bytes; the chunk
        # after it carries no seek, so it continues from byte 2 — not
        # from the end of what has been written so far.
        session.transaction.side_effect = [
            ([8, 1, 0], b"ABCDEFGH"),
            ([2, 1, 0], b"xy"),
            ([2, 0, 0xFFFFFFFF], b"zw"),
        ]
        assert chdk.remote_capture_get_data(1) == b"xyzwEFGH"

    def test_a_forward_seek_leaves_a_zero_filled_gap(self):
        chdk, session = self._make_chdk()
        session.transaction.side_effect = [
            ([2, 1, 0], b"AB"),
            ([2, 1, 6], b"CD"),
            ([2, 0, 0xFFFFFFFF], b"EF"),
        ]
        assert chdk.remote_capture_get_data(1) == b"AB\x00\x00\x00\x00CDEF"

    def test_camera_that_never_clears_more_raises(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([1, 1, 0xFFFFFFFF], b"x")
        with pytest.raises(RuntimeError, match="10000"):
            chdk.remote_capture_get_data(1)


class TestRemoteCaptureGetChunk:
    def _make_chdk(self):
        mock_session = MagicMock()
        return ChdkPTP(mock_session), mock_session

    def test_reports_size_more_and_position(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([3, 1, 16], b"abc")
        chunk, more, position = chdk.remote_capture_get_chunk(1)
        assert chunk == b"abc"
        assert more is True
        assert position == 16

    def test_short_params_mean_one_appended_chunk(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([], b"abc")
        chunk, more, position = chdk.remote_capture_get_chunk(1)
        assert chunk == b"abc"
        assert more is False
        assert position == -1

    def test_size_smaller_than_the_data_phase_truncates(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([2, 0, 0xFFFFFFFF], b"abcd\x00\x00")
        chunk, _, _ = chdk.remote_capture_get_chunk(1)
        assert chunk == b"ab"

    def test_size_larger_than_the_data_phase_keeps_what_arrived(self):
        chdk, session = self._make_chdk()
        session.transaction.return_value = ([64, 0, 0xFFFFFFFF], b"abcd")
        chunk, _, _ = chdk.remote_capture_get_chunk(1)
        assert chunk == b"abcd"
