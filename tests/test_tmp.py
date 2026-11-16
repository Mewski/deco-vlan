"""The protocol client against a stand-in for `tmpsvr` that follows the behaviour seen on a Deco."""

import json
import socket
import threading
import zlib

import pytest

from deco_vlan import tmp
from deco_vlan.tmp import Flag, Frame, Opcode


def receive(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data


def receive_frame(sock):
    header = receive(sock, tmp.OUTER.size)
    return Frame.unpack(header + receive(sock, int.from_bytes(header[4:6], "big")))


def serve(sock, handler, think=0, corrupt=False):
    """Answer requests the way the service does.

    Every fragment but the last is acknowledged with the byte count so far. The last one is
    answered with `think` more acknowledgements and then the reply, of which further fragments
    are served on PULL.
    """
    try:
        assert receive(sock, 4) == tmp.HELLO
        sock.sendall(tmp.HELLO_REPLY)
        assert receive(sock, 4) == tmp.HELLO_REPLY
        body, reply, waiting = bytearray(), b"", think
        while True:
            request = receive_frame(sock)

            def send(flag, fragment=b"", request=request, **fields):
                sock.sendall(
                    Frame(request.opcode, request.sequence, flag, fragment, **fields).pack()
                )

            if request.flag == Flag.PUSH:
                if request.offset == 0:
                    body, waiting = bytearray(), think
                body += request.fragment
                if len(body) < request.total:
                    send(Flag.ACK, offset=len(body))
                    continue
                assert zlib.crc32(body) == request.crc
                token = request.opcode == Opcode.TOKEN
                reply = b"x" * 32 if token else handler(request.opcode, bytes(body))
            if waiting:
                waiting -= 1
                send(Flag.ACK, offset=len(body))
                continue
            offset = request.offset if request.flag == Flag.PULL else 0
            damage = 1 if corrupt and request.opcode != Opcode.TOKEN else 0
            send(
                Flag.DATA,
                reply[offset : offset + tmp.FRAGMENT],
                total=len(reply),
                crc=zlib.crc32(reply) ^ damage,
                offset=offset,
            )
    except (ConnectionError, OSError):
        pass


@pytest.fixture
def connect():
    sockets = []

    def start(handler, **behaviour):
        client, server = socket.socketpair()
        sockets.extend([client, server])
        threading.Thread(
            target=serve, args=(server, handler), kwargs=behaviour, daemon=True
        ).start()
        return tmp.Session(client, timeout=5)

    yield start
    for sock in sockets:
        sock.close()


def echo(opcode, body):
    return json.dumps({"error_code": 0, "result": json.loads(body)["params"]}).encode()


def test_frame_round_trip():
    frame = Frame(Opcode.WRITE_FILE, 7, Flag.PUSH, b"abc", total=10, crc=99, offset=4)
    assert Frame.unpack(frame.pack()) == frame


def test_damaged_frame_is_rejected():
    data = bytearray(Frame(Opcode.TOKEN, 1, Flag.PUSH, b"abc").pack())
    data[-1] ^= 1
    with pytest.raises(tmp.ProtocolError):
        Frame.unpack(bytes(data))


def test_call(connect):
    assert connect(echo).call(Opcode.READ_FILE, {"file": "/proc/version"}) == {
        "file": "/proc/version"
    }


def test_request_larger_than_one_fragment(connect):
    content = "0123456789" * 3000
    assert connect(echo).call(Opcode.WRITE_FILE, {"content": content}) == {"content": content}


def test_reply_larger_than_one_fragment(connect):
    data = "x" * (3 * tmp.FRAGMENT)

    def big(opcode, body):
        return json.dumps({"error_code": 0, "result": {"data": data}}).encode()

    assert connect(big).call(Opcode.READ_FILE, {"file": "/big"}) == {"data": data}


def test_reply_that_takes_a_while(connect):
    assert connect(echo, think=3).call(Opcode.READ_FILE, {"n": 1}) == {"n": 1}


def test_handler_error_is_raised(connect):
    def failing(opcode, body):
        return json.dumps({"error_code": 1, "msg": "Lua exception"}).encode()

    with pytest.raises(tmp.RequestError, match="Lua exception"):
        connect(failing).call(Opcode.AUTO_UPDATE_SET, {})


def test_damaged_reply_is_rejected(connect):
    with pytest.raises(tmp.ProtocolError, match="checksum"):
        connect(echo, corrupt=True).call(Opcode.READ_FILE, {"n": 1})
