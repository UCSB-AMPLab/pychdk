"""PTP (Picture Transfer Protocol) session and transaction management.

Implements the container-based PTP protocol over USB bulk endpoints.
Each transaction follows: Command -> [Data] -> Response.
"""
import struct
import time
from enum import IntEnum


class ContainerType(IntEnum):
    COMMAND = 1
    DATA = 2
    RESPONSE = 3
    EVENT = 4


class OperationCode(IntEnum):
    GET_DEVICE_INFO = 0x1001
    OPEN_SESSION = 0x1002
    CLOSE_SESSION = 0x1003
    CHDK = 0x9999


class ResponseCode(IntEnum):
    OK = 0x2001
    GENERAL_ERROR = 0x2002
    SESSION_NOT_OPEN = 0x2003
    INVALID_TRANSACTION_ID = 0x2004
    OPERATION_NOT_SUPPORTED = 0x2005
    PARAMETER_NOT_SUPPORTED = 0x2006
    SESSION_ALREADY_OPEN = 0x201E


# PTP container header: length(4) + type(2) + code(2) + transaction_id(4) = 12 bytes
CONTAINER_HEADER_SIZE = 12
CONTAINER_HEADER_FMT = "<IHHI"


class PTPError(Exception):
    """PTP protocol error."""
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or f"PTP error 0x{code:04x}")


class PTPContainer:
    """A PTP protocol container (command, data, or response)."""

    def __init__(self, type, code, transaction_id, params=None, data=None):
        self.type = type
        self.code = code
        self.transaction_id = transaction_id
        self.params = params or []
        self.data = data or b""

    def pack(self):
        """Serialize to bytes for sending over USB."""
        if self.type == ContainerType.COMMAND:
            payload = b""
            for p in self.params:
                payload += struct.pack("<I", p & 0xFFFFFFFF)
            length = CONTAINER_HEADER_SIZE + len(payload)
            header = struct.pack(CONTAINER_HEADER_FMT,
                                 length, self.type, self.code,
                                 self.transaction_id)
            return header + payload
        elif self.type == ContainerType.DATA:
            length = CONTAINER_HEADER_SIZE + len(self.data)
            header = struct.pack(CONTAINER_HEADER_FMT,
                                 length, self.type, self.code,
                                 self.transaction_id)
            return header + self.data
        else:
            raise ValueError(f"Cannot pack container type {self.type}")

    @classmethod
    def unpack(cls, raw):
        """Deserialize from bytes received over USB."""
        if len(raw) < CONTAINER_HEADER_SIZE:
            raise ValueError(f"Container too short: {len(raw)} bytes")
        length, ctype, code, tx_id = struct.unpack_from(
            CONTAINER_HEADER_FMT, raw
        )
        payload = raw[CONTAINER_HEADER_SIZE:]
        params = []
        data = b""
        if ctype in (ContainerType.COMMAND, ContainerType.RESPONSE):
            # Params are 4-byte LE integers
            for i in range(0, len(payload), 4):
                if i + 4 <= len(payload):
                    params.append(struct.unpack_from("<I", payload, i)[0])
        elif ctype == ContainerType.DATA:
            data = payload
        return cls(ctype, code, tx_id, params=params, data=data)


class PTPSession:
    """Manages a PTP session over a USB transport.

    Handles session open/close, transaction ID sequencing, and the
    command -> data -> response transaction cycle.
    """

    # Minimum gap between PTP transactions in seconds.  The A2500 can
    # get overwhelmed by rapid-fire commands; this floor only kicks in
    # during tight loops like drain_messages.
    MIN_TRANSACTION_GAP = 0.02  # 20 ms

    def __init__(self, transport):
        self._transport = transport
        self.session_id = 0
        self._transaction_id = 0
        self._is_open = False
        self._last_transaction = 0.0

    def open(self):
        """Open a PTP session."""
        self.session_id = 1
        self._transaction_id = 1
        self._send_command(OperationCode.OPEN_SESSION, [self.session_id])
        resp = self._receive_response()
        if resp.code != ResponseCode.OK:
            raise PTPError(resp.code, "Failed to open PTP session")
        self._is_open = True

    def close(self):
        """Close the PTP session."""
        if not self._is_open:
            return
        try:
            self.transaction(OperationCode.CLOSE_SESSION)
        except PTPError:
            pass
        self._is_open = False

    def transaction(self, operation, params=None, send_data=None,
                    receive_data=False):
        """Execute a PTP transaction.

        Args:
            operation: PTP operation code.
            params: List of up to 5 uint32 parameters.
            send_data: Bytes to send in data phase (host->device).
            receive_data: If True, expect data phase (device->host).

        Returns:
            Tuple of (response_params, data_bytes). data_bytes is
            b"" if receive_data is False.
        """
        # Enforce minimum gap between transactions
        elapsed = time.monotonic() - self._last_transaction
        if elapsed < self.MIN_TRANSACTION_GAP:
            time.sleep(self.MIN_TRANSACTION_GAP - elapsed)

        tx_id = self._transaction_id
        self._transaction_id += 1

        # Command phase
        self._send_command(operation, params or [], tx_id)

        # Data phase (send)
        if send_data is not None:
            self._send_data(operation, send_data, tx_id)

        # Data phase (receive)
        data = b""
        if receive_data:
            data = self._receive_data(tx_id)

        # Response phase
        resp = self._receive_response()
        self._last_transaction = time.monotonic()
        if resp.code != ResponseCode.OK:
            raise PTPError(resp.code)

        return resp.params, data

    def _send_command(self, operation, params=None, tx_id=None):
        if tx_id is None:
            tx_id = self._transaction_id
        container = PTPContainer(ContainerType.COMMAND, operation,
                                 tx_id, params=params or [])
        self._transport.bulk_write(container.pack())

    def _send_data(self, operation, data, tx_id):
        container = PTPContainer(ContainerType.DATA, operation,
                                 tx_id, data=data)
        self._transport.bulk_write(container.pack())

    def _receive_response(self):
        raw = self._transport.bulk_read(size=512)
        container = PTPContainer.unpack(raw)
        if container.type == ContainerType.DATA:
            # Data arrived before response — read the response too
            raw = self._transport.bulk_read(size=512)
            container = PTPContainer.unpack(raw)
        if container.type != ContainerType.RESPONSE:
            raise PTPError(0, f"Expected response, got type {container.type}")
        return container

    def _receive_data(self, tx_id):
        raw = self._transport.bulk_read(size=524288)  # 512KB initial read
        container = PTPContainer.unpack(raw)
        if container.type != ContainerType.DATA:
            raise PTPError(0, f"Expected data, got type {container.type}")
        total_length = struct.unpack_from("<I", raw)[0]
        data = container.data
        # Read remaining chunks if data is larger than initial read
        while len(data) + CONTAINER_HEADER_SIZE < total_length:
            chunk = self._transport.bulk_read(size=524288)
            data += chunk
        return data[:total_length - CONTAINER_HEADER_SIZE]

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
