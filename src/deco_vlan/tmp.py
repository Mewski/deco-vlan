"""TMP, the request/response protocol of `tmpsvr`, the service behind the Deco app.

`tmpsvr` listens on 127.0.0.1:20002 on the Deco. A request is an opcode plus a JSON body; the
reply is JSON. Bodies are cut into fragments of at most 8156 bytes, each in its own frame:

    outer header, 16 bytes   version 1.1, type 5, length of what follows, CRC-32 of the frame
    inner header, 20 bytes   opcode, flag, sequence number, CRC-32 and length of the whole body,
                             offset of this fragment
    fragment

The CRC-32 of the frame is computed with the checksum field set to a fixed placeholder.

A request fragment travels in a PUSH frame, and the service answers each with an ACK that
carries the number of bytes it has so far. After the last fragment it answers ACK again while it
works, or DATA with the first fragment of the reply. PULL asks for the reply from an offset.

The frame layout and the opcodes for raw file access were published by CPlusPlus17
(github.com/CPlusPlus17/deco-filepush-disclosure). Fragmented requests are documented here.
"""

import json
import struct
import time
import zlib
from enum import IntEnum
from typing import Any, NamedTuple, Protocol

OUTER = struct.Struct("!BBBBHBBII")
INNER = struct.Struct("!BBHBBHIII")
CHECKSUM_PLACEHOLDER = 0x5A6B7C8D
FRAGMENT = 8156
TOKEN_LENGTH = 32

HELLO = bytes.fromhex("01010100")
HELLO_REPLY = bytes.fromhex("01010200")


class Flag(IntEnum):
    PUSH = 2
    ACK = 3
    PULL = 4
    DATA = 5


class Opcode(IntEnum):
    TOKEN = 0x0001
    READ_FILE = 0x00F0  # {"file": path} -> {"data": contents}, as root
    AUTO_UPDATE_GET = 0x427E  # -> {"enable_auto_update": bool, "time_begin": "180", ...}
    AUTO_UPDATE_SET = 0x427F  # the same fields, the times as numbers (minutes after midnight)
    WRITE_FILE = 0xC456  # {"file": path, "content": text}, as root; always reports success


class ProtocolError(Exception):
    """The conversation did not follow the protocol."""


class RequestError(Exception):
    """The service ran the request and reported that it failed."""


class Channel(Protocol):
    """What a session needs from its transport: a connected socket, or anything like one."""

    def settimeout(self, seconds: float) -> None: ...
    def sendall(self, data: bytes) -> None: ...
    def recv(self, count: int) -> bytes: ...


class Frame(NamedTuple):
    opcode: int
    sequence: int
    flag: int
    fragment: bytes = b""
    total: int = 0
    crc: int = 0
    offset: int = 0

    def pack(self) -> bytes:
        inner = INNER.pack(
            1, 2, self.opcode, self.flag, 0, self.sequence, self.crc, self.total, self.offset
        )
        length = len(inner) + len(self.fragment)
        outer = OUTER.pack(1, 1, 5, 0, length, 0, 0, 0, CHECKSUM_PLACEHOLDER)
        data = bytearray(outer + inner + self.fragment)
        struct.pack_into("!I", data, 12, zlib.crc32(data))
        return bytes(data)

    @classmethod
    def unpack(cls, data: bytes) -> "Frame":
        major, minor, kind, _, length, flags, error, _, checksum = OUTER.unpack_from(data)
        if (major, minor, kind, flags, error) != (1, 1, 5, 0, 0):
            raise ProtocolError("unexpected frame header")
        if length != len(data) - OUTER.size:
            raise ProtocolError("frame length mismatch")
        unsigned = bytearray(data)
        struct.pack_into("!I", unsigned, 12, CHECKSUM_PLACEHOLDER)
        if zlib.crc32(unsigned) != checksum:
            raise ProtocolError("frame checksum mismatch")
        inner = INNER.unpack_from(data, OUTER.size)
        _, _, opcode, flag, _, sequence, crc, total, offset = inner
        return cls(opcode, sequence, flag, data[OUTER.size + INNER.size :], total, crc, offset)


class Session:
    """One TMP conversation over a connected channel."""

    def __init__(self, channel: Channel, timeout: float = 30) -> None:
        self.channel = channel
        self.timeout = timeout
        self.sequence = 0
        self._deadline = time.monotonic() + timeout
        self._send(HELLO)
        if self._receive(len(HELLO_REPLY)) != HELLO_REPLY:
            raise ProtocolError("unexpected greeting")
        self._send(HELLO_REPLY)
        if len(self.request(Opcode.TOKEN)) != TOKEN_LENGTH:
            raise ProtocolError("unexpected session token")

    def request(self, opcode: int, body: bytes = b"") -> bytes:
        """Send a request body and return the reply body."""
        self._deadline = time.monotonic() + self.timeout
        self.sequence += 1
        return self._pull(self._push(opcode, body))

    def call(self, opcode: int, params: dict[str, Any] | None = None) -> Any:
        """Send `{"params": params}` and return the `result` of the reply."""
        body = b""
        if params is not None:
            body = json.dumps({"params": params}, separators=(",", ":")).encode()
        reply = json.loads(self.request(opcode, body).decode("latin-1"))
        if reply.get("error_code") != 0:
            raise RequestError(f"opcode {opcode:#06x} failed: {reply.get('msg') or reply}")
        return reply.get("result")

    def _push(self, opcode: int, body: bytes) -> Frame:
        """Send the body fragment by fragment. Returns the answer to the last fragment."""
        crc, sent = zlib.crc32(body), 0
        while True:
            fragment = body[sent : sent + FRAGMENT]
            push = Frame(opcode, self.sequence, Flag.PUSH, fragment, len(body), crc, sent)
            reply = self._exchange(push)
            sent += len(fragment)
            if sent >= len(body):
                return reply
            if reply.flag != Flag.ACK or reply.offset != sent:
                raise ProtocolError("request fragment was not acknowledged")

    def _pull(self, reply: Frame) -> bytes:
        """Collect the reply body, starting from the answer to the last request fragment."""
        body = bytearray()
        while True:
            if reply.flag == Flag.DATA:
                if reply.offset != len(body):
                    raise ProtocolError("reply fragment out of order")
                body.extend(reply.fragment)
                if len(body) >= reply.total:
                    break
            elif reply.flag == Flag.ACK:
                time.sleep(0.05)  # the service is still working on the request
            else:
                raise ProtocolError(f"unexpected flag {reply.flag}")
            pull = Frame(reply.opcode, reply.sequence, Flag.PULL, offset=len(body))
            reply = self._exchange(pull)
        if zlib.crc32(body) != reply.crc:
            raise ProtocolError("reply checksum mismatch")
        return bytes(body)

    def _exchange(self, frame: Frame) -> Frame:
        """Send one frame and return the frame that answers it."""
        self._send(frame.pack())
        header = self._receive(OUTER.size)
        reply = Frame.unpack(header + self._receive(int.from_bytes(header[4:6], "big")))
        if (reply.opcode, reply.sequence) != (frame.opcode, frame.sequence):
            raise ProtocolError("reply does not match the request")
        return reply

    def _send(self, data: bytes) -> None:
        self.channel.settimeout(self._remaining())
        self.channel.sendall(data)

    def _receive(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            self.channel.settimeout(self._remaining())
            chunk = self.channel.recv(count - len(data))
            if not chunk:
                raise ConnectionError("the Deco closed the connection")
            data.extend(chunk)
        return bytes(data)

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the Deco did not answer in time")
        return remaining
