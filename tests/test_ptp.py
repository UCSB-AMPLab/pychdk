"""Tests for PTP session and transaction layer."""
import struct
from unittest.mock import MagicMock, patch
import pytest
from pychdk.ptp import (
    PTPSession,
    PTPContainer,
    ContainerType,
    OperationCode,
    ResponseCode,
    PTPError,
)


def _make_response_bytes(code, tx_id, params=None):
    """Build a raw PTP response container."""
    params = params or []
    payload = b""
    for p in params:
        payload += struct.pack("<I", p)
    length = 12 + len(payload)
    header = struct.pack("<IHHI", length, ContainerType.RESPONSE, code, tx_id)
    return header + payload


def _make_data_bytes(code, tx_id, data):
    """Build a raw PTP data container."""
    length = 12 + len(data)
    header = struct.pack("<IHHI", length, ContainerType.DATA, code, tx_id)
    return header + data


class TestPTPContainer:
    def test_pack_command(self):
        c = PTPContainer(ContainerType.COMMAND, OperationCode.OPEN_SESSION, 1, [1])
        raw = c.pack()
        length, ctype, code, tx_id = struct.unpack_from("<IHHI", raw)
        assert length == len(raw)
        assert ctype == ContainerType.COMMAND
        assert code == OperationCode.OPEN_SESSION
        assert tx_id == 1

    def test_unpack_response(self):
        raw = _make_response_bytes(ResponseCode.OK, 1)
        c = PTPContainer.unpack(raw)
        assert c.type == ContainerType.RESPONSE
        assert c.code == ResponseCode.OK
        assert c.transaction_id == 1


class TestPTPSession:
    def test_open_session(self):
        mock_transport = MagicMock()
        mock_transport.bulk_read.return_value = _make_response_bytes(
            ResponseCode.OK, 1
        )
        session = PTPSession(mock_transport)
        session.open()
        assert session.session_id == 1

    def test_close_session(self):
        mock_transport = MagicMock()
        # open response, then close response
        mock_transport.bulk_read.side_effect = [
            _make_response_bytes(ResponseCode.OK, 1),
            _make_response_bytes(ResponseCode.OK, 2),
        ]
        session = PTPSession(mock_transport)
        session.open()
        session.close()

    def test_transaction_increments_id(self):
        mock_transport = MagicMock()
        mock_transport.bulk_read.side_effect = [
            _make_response_bytes(ResponseCode.OK, 1),  # open
            _make_response_bytes(ResponseCode.OK, 2),  # first op
            _make_response_bytes(ResponseCode.OK, 3),  # second op
        ]
        session = PTPSession(mock_transport)
        session.open()
        session.transaction(OperationCode.GET_DEVICE_INFO)
        session.transaction(OperationCode.GET_DEVICE_INFO)
        assert session._transaction_id == 3

    def test_error_response_raises(self):
        mock_transport = MagicMock()
        mock_transport.bulk_read.side_effect = [
            _make_response_bytes(ResponseCode.OK, 1),          # open
            _make_response_bytes(ResponseCode.GENERAL_ERROR, 2),  # fail
        ]
        session = PTPSession(mock_transport)
        session.open()
        with pytest.raises(PTPError):
            session.transaction(OperationCode.GET_DEVICE_INFO)
