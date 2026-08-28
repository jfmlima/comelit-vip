"""The RTSP relay over real sockets."""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from custom_components.comelit_vip.viper.rtp import RtpPacket
from custom_components.comelit_vip.viper.rtsp import (
    DEFAULT_HOST,
    MAX_BODY,
    MAX_HEADERS,
    MAX_LINE,
    MAX_REMOTE_CLIENTS,
    RESTART_GAP,
    RtspRelay,
    _Client,
    _content_length,
    _interleaved,
)


class FakeCall:
    """A call that yields what it is given and then waits to be ended."""

    def __init__(self, packets: list[RtpPacket] | None = None) -> None:
        self.closed = False
        self.ended = asyncio.Event()
        self._packets = packets or []

    async def packets(self):
        for packet in self._packets:
            yield packet
        await self.ended.wait()

    async def close(self) -> None:
        self.closed = True
        self.ended.set()


class ScriptedCall:
    """A call the test feeds, one packet at a time."""

    def __init__(self) -> None:
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def packets(self):
        while True:
            packet = await self._queue.get()
            if packet is None:
                return
            yield packet

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)

    def push(self, packet: RtpPacket) -> None:
        self._queue.put_nowait(packet)


class Conversation:
    """One RTSP client, over a real socket, speaking the real protocol."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str) -> None:
        self.reader = reader
        self.writer = writer
        self.url = f"rtsp://{DEFAULT_HOST}/{path}"
        self.cseq = 0

    async def request(self, method: str, url: str | None = None, **headers: str):
        """Send one request and return ``(code, headers, body)``."""
        self.cseq += 1
        lines = [f"{method} {url or self.url} RTSP/1.0", f"CSeq: {self.cseq}"]
        lines += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self.writer.drain()
        return await self.reply()

    async def reply(self):
        """Read the next reply, stepping over any media that arrives first."""
        while True:
            first = await asyncio.wait_for(self.reader.readexactly(1), 5)
            if first == b"$":
                await self._frame()

                continue
            line = (first + await self.reader.readline()).decode().strip()
            code = int(line.split()[1])
            headers: dict[str, str] = {}
            while True:
                raw = (await self.reader.readline()).decode().strip()
                if not raw:
                    break
                name, _, value = raw.partition(":")
                headers[name.strip().lower()] = value.strip()
            body = await self.reader.readexactly(int(headers.get("content-length", 0)))
            return code, headers, body

    async def media(self) -> tuple[int, bytes]:
        """Read one interleaved frame. Nothing else may be outstanding."""
        first = await asyncio.wait_for(self.reader.readexactly(1), 5)
        assert first == b"$", f"expected media, got {first!r}"
        return await self._frame()

    async def play(self, transport: str = "RTP/AVP/TCP;unicast;interleaved=0-1") -> None:
        """Run the handshake a viewer runs."""
        assert (await self.request("DESCRIBE", Accept="application/sdp"))[0] == 200
        assert (await self.request("SETUP", Transport=transport))[0] == 200
        assert (await self.request("PLAY", Session="s"))[0] == 200

    async def _frame(self) -> tuple[int, bytes]:
        channel = (await self.reader.readexactly(1))[0]
        length = int.from_bytes(await self.reader.readexactly(2), "big")
        return channel, await self.reader.readexactly(length)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()


class FakeWriter:
    """Collects what would go to a client."""

    def __init__(self, *, buffered: int = 0, peer: tuple[str, int] = ("192.0.2.9", 5000)) -> None:
        self.frames: list[bytes] = []
        self.closed = False
        self.transport = _FakeTransport(buffered)
        self._peer = peer

    def get_extra_info(self, name: str):
        return self._peer if name == "peername" else None

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    def close(self) -> None:
        self.closed = True


class _FakeTransport:
    def __init__(self, buffered: int) -> None:
        self._buffered = buffered

    def get_write_buffer_size(self) -> int:
        return self._buffered


@pytest.fixture
async def relay(socket_enabled):
    """A relay bound to a free loopback port."""
    call = FakeCall()

    async def _factory(target=None):
        return call

    relay = RtspRelay(_factory, port=0)
    await relay.start()
    relay.port = relay._server.sockets[0].getsockname()[1]
    try:
        yield relay
    finally:
        await relay.stop()


@pytest.fixture
async def scripted(socket_enabled):
    """A relay whose calls the test feeds, and the list of calls it made."""
    calls: list[ScriptedCall] = []

    async def _factory(target=None):
        call = ScriptedCall()
        calls.append(call)
        return call

    relay = RtspRelay(_factory, port=0)
    await relay.start()
    relay.port = relay._server.sockets[0].getsockname()[1]
    relay.calls = calls
    try:
        yield relay
    finally:
        await relay.stop()


@pytest.fixture
async def talk():
    """Open RTSP conversations, and close them however the test ends."""
    opened: list[Conversation] = []

    async def _open(relay: RtspRelay) -> Conversation:
        reader, writer = await asyncio.open_connection(DEFAULT_HOST, relay.port)
        conversation = Conversation(reader, writer, relay.path)
        opened.append(conversation)
        return conversation

    try:
        yield _open
    finally:
        for conversation in opened:
            conversation.close()


# ------------------------------------------------------------------ exposure
def test_default_loopback():
    assert RtspRelay(_never).host == DEFAULT_HOST
    assert "127.0.0.1" in RtspRelay(_never).url()


# ------------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        ("RTP/AVP/TCP;unicast;interleaved=0-1", (0, 1)),
        ("RTP/AVP/TCP;unicast;interleaved=4-5", (4, 5)),
        ("RTP/AVP/TCP;unicast;interleaved=2", (2, 3)),
        ("RTP/AVP/TCP;unicast;interleaved=9-9", (0, 1)),
        ("RTP/AVP/TCP;unicast;interleaved=300-301", (0, 1)),
        ("RTP/AVP/TCP;unicast;interleaved=-4", (0, 1)),
        ("RTP/AVP/TCP;unicast;interleaved=x-y", (0, 1)),
        ("RTP/AVP/TCP;unicast", (0, 1)),
    ],
)
def test_interleaved_parsing(transport, expected):
    assert _interleaved(transport) == expected


def test_oversized_body():
    from custom_components.comelit_vip.viper.rtsp import RtspError

    with pytest.raises(RtspError) as err:
        _content_length({"content-length": str(MAX_BODY + 1)})

    assert err.value.code == 413


def test_bad_content_length():
    from custom_components.comelit_vip.viper.rtsp import RtspError

    with pytest.raises(RtspError):
        _content_length({"content-length": "not a number"})


# -------------------------------------------------------------- live service
async def test_header_flood_no_call(relay):
    calls = 0

    async def _counting(target=None):
        nonlocal calls
        calls += 1
        return FakeCall()

    relay._call_factory = _counting
    reader, writer = await asyncio.open_connection(DEFAULT_HOST, relay.port)
    writer.write(b"DESCRIBE rtsp://x/comelit RTSP/1.0\r\n")
    writer.write(b"".join(b"X-Pad-%d: filler\r\n" % i for i in range(MAX_HEADERS + 20)))
    with contextlib.suppress(Exception):
        await writer.drain()
    answer = await asyncio.wait_for(reader.read(200), 5)
    writer.close()

    assert b"431" in answer
    assert calls == 0


async def test_stalled_request_timeout(relay, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.viper.rtsp.REQUEST_TIMEOUT", 0.05)
    reader, writer = await asyncio.open_connection(DEFAULT_HOST, relay.port)
    writer.write(b"OPTIONS rtsp://x/comelit RTSP/1.0\r\n")
    await writer.drain()

    assert await asyncio.wait_for(reader.read(200), 5) == b""

    writer.close()
    await asyncio.sleep(0)
    assert relay._clients == set()


async def test_remote_client_cap(relay):
    relay._clients.update(_remote_client() for _ in range(MAX_REMOTE_CLIENTS))

    assert not relay._admit(_remote_client())
    assert relay._admit(_local_client()), "loopback exempt"


async def test_stop_clears_sessions(relay):
    reader, writer = await asyncio.open_connection(DEFAULT_HOST, relay.port)
    writer.write(b"OPTIONS rtsp://x/comelit RTSP/1.0\r\n\r\n")
    await writer.drain()
    await asyncio.wait_for(reader.read(100), 5)
    assert relay._sessions

    await relay.stop()

    assert relay._sessions == set()
    assert relay._clients == set()


# --------------------------------------------------------------------- media
def test_timestamps_monotonic_across_restart():
    """Each call starts its timestamps from an unrelated base."""
    client = _Client(FakeWriter(), "abcd")

    _timestamps(client, [0, 3000, 6000], generation=1)
    outputs = _timestamps(client, [500_000, 503_000], generation=2)

    assert outputs[0] == 6000 + RESTART_GAP
    assert outputs[1] > outputs[0]


def test_client_error_isolated():
    good, bad = _playing(FakeWriter()), _playing(FakeWriter())
    bad.writer.write = _explode
    relay = RtspRelay(_never)
    relay._clients.update({good, bad})

    relay._fan_out(_packet(1))

    assert bad.writer.closed
    assert not good.writer.closed
    assert len(good.writer.frames) == 1


async def test_backed_up_client_dropped():
    slow = _playing(FakeWriter(buffered=1 << 30))
    relay = RtspRelay(_never)
    relay._clients.add(slow)

    relay._fan_out(_packet(1))

    assert slow.writer.closed
    assert relay._clients == set()


# ------------------------------------------------------- one owner, one call
async def test_ended_call_replaced(scripted):
    await scripted._ensure_call()
    await scripted.calls[0].close()
    await _until(lambda: scripted._episode is None)

    await scripted._ensure_call()

    assert len(scripted.calls) == 2
    assert scripted._live() is not None
    await scripted.abandon_call()


async def test_two_viewers_share_one_call(scripted, talk):
    first, second = await talk(scripted), await talk(scripted)

    assert (await first.request("DESCRIBE"))[0] == 200
    assert (await second.request("DESCRIBE"))[0] == 200

    assert len(scripted.calls) == 1


async def test_stop_with_unhandled_connection(relay, socket_enabled):
    """A just-accepted connection has no handler task yet, so it is in none of the relay's sets."""
    # A blocking connect: no loop turn happens before stop() is called.
    with socket.create_connection((DEFAULT_HOST, relay.port)):
        assert relay._sessions == set()

        await asyncio.wait_for(relay.stop(), 5)


async def test_stop_closes_clients(relay, talk):
    conversation = await talk(relay)
    assert (await conversation.request("OPTIONS"))[0] == 200

    await relay.stop()

    assert await asyncio.wait_for(conversation.reader.read(100), 5) == b""


async def test_last_viewer_ends_call(scripted, talk, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.viper.rtsp.IDLE_GRACE", 0.01)
    conversation = await talk(scripted)
    await conversation.play()
    assert scripted.viewers == 1

    conversation.close()
    await _until(lambda: scripted.calls[0].closed)

    assert scripted._episode is None


# ----------------------------------------------------------- the conversation
async def test_handshake_delivers_video(scripted, talk):
    conversation = await talk(scripted)
    code, headers, body = await conversation.request("DESCRIBE", Accept="application/sdp")

    assert code == 200
    assert headers["content-type"] == "application/sdp"
    assert b"m=video" in body

    code, headers, _ = await conversation.request("SETUP", Transport="RTP/AVP/TCP;unicast;interleaved=0-1")

    assert code == 200
    assert "interleaved=0-1" in headers["transport"]
    assert headers["session"]

    assert (await conversation.request("PLAY", Session=headers["session"]))[0] == 200
    await _until(lambda: scripted.viewers == 1)
    scripted.calls[0].push(_packet(1000))

    channel, payload = await conversation.media()

    assert channel == 0
    assert payload[1] & 0x7F == 96, "payload type rewritten"


async def test_interleaved_channel_honoured(scripted, talk):
    conversation = await talk(scripted)
    await conversation.play("RTP/AVP/TCP;unicast;interleaved=4-5")
    await _until(lambda: scripted.viewers == 1)
    scripted.calls[0].push(_packet(1000))

    channel, _ = await conversation.media()

    assert channel == 4


async def test_udp_refused(relay, talk):
    conversation = await talk(relay)

    code, _, _ = await conversation.request("SETUP", Transport="RTP/AVP;unicast;client_port=5000-5001")

    assert code == 461


async def test_unknown_method(relay, talk):
    conversation = await talk(relay)

    code, _, _ = await conversation.request("FROB")

    assert code == 405


async def test_failed_call_503(relay, talk):

    async def _refuse(target=None):
        raise RuntimeError("the panel said no")

    relay._call_factory = _refuse
    conversation = await talk(relay)

    code, _, _ = await conversation.request("DESCRIBE")

    assert code == 503


async def test_long_request_line(relay, talk):
    conversation = await talk(relay)
    conversation.writer.write(b"DESCRIBE rtsp://x/" + b"a" * (MAX_LINE + 10) + b" RTSP/1.0\r\n\r\n")
    await conversation.writer.drain()

    code, _, _ = await conversation.reply()

    assert code == 414


async def test_remote_client_cap_on_wire(relay, talk, monkeypatch):
    monkeypatch.setattr(_Client, "local", property(lambda self: False))
    for _ in range(MAX_REMOTE_CLIENTS):
        assert (await (await talk(relay)).request("OPTIONS"))[0] == 200

    code, _, _ = await (await talk(relay)).request("OPTIONS")

    assert code == 503


# ------------------------------------------------------------------ the path
async def test_unknown_path_404(relay, talk):
    calls = 0

    async def _counting(target=None):
        nonlocal calls
        calls += 1
        return FakeCall()

    relay._call_factory = _counting
    conversation = await talk(relay)

    code, _, _ = await conversation.request("DESCRIBE", url="rtsp://127.0.0.1/somewhere-else")

    assert code == 404
    assert calls == 0


async def test_track_url_served(scripted, talk):
    """The SDP advertises `a=control:trackID=0`."""
    conversation = await talk(scripted)
    assert (await conversation.request("DESCRIBE"))[0] == 200

    code, _, _ = await conversation.request(
        "SETUP", url=f"{conversation.url}/trackID=0", Transport="RTP/AVP/TCP;unicast;interleaved=0-1"
    )

    assert code == 200


async def test_teardown_closes_client(relay, talk):
    conversation = await talk(relay)
    assert (await conversation.request("OPTIONS"))[0] == 200
    assert (await conversation.request("TEARDOWN", Session="s"))[0] == 200

    assert await asyncio.wait_for(conversation.reader.read(100), 5) == b""
    assert relay._clients == set()


def test_non_playing_client_gets_nothing():
    watching, browsing = _playing(FakeWriter()), _Client(FakeWriter(), "abcd")
    relay = RtspRelay(_never)
    relay._clients.update({watching, browsing})

    relay._fan_out(_packet(1))

    assert len(watching.writer.frames) == 1
    assert browsing.writer.frames == []


async def _until(ready, passes: int = 400) -> None:
    """Wait for something that has to cross a socket first."""
    for _ in range(passes):
        if ready():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("it never happened")


def _packet(timestamp: int) -> RtpPacket:
    return RtpPacket(marker=True, payload_type=96, sequence=1, timestamp=timestamp, ssrc=1, payload=b"\x65\x88")


def _timestamps(client: _Client, values: list[int], *, generation: int) -> list[int]:
    outputs = []
    for value in values:
        frame = client.rtp_frame(_packet(value), generation)
        outputs.append(int.from_bytes(frame[8:12], "big"))
    return outputs


def _playing(writer: FakeWriter) -> _Client:
    client = _Client(writer, "abcd")
    client.playing = True
    return client


def _remote_client() -> _Client:
    return _Client(FakeWriter(peer=("192.0.2.9", 5000)), "remote")


def _local_client() -> _Client:
    return _Client(FakeWriter(peer=("127.0.0.1", 5000)), "local")


def _explode(_data: bytes) -> None:
    raise OSError("gone")


async def _never(target=None):
    raise AssertionError("this test must not start a call")


# ------------------------------------------------------------------ entrances
@pytest.fixture
async def doors(socket_enabled):
    """A relay serving two entrances."""
    dialled: list[str] = []

    async def _factory(target):
        dialled.append(target)
        return FakeCall()

    relay = RtspRelay(_factory, targets=("SB900001", "SB900002"), port=0)
    await relay.start()
    relay.port = relay._server.sockets[0].getsockname()[1]
    relay.dialled = dialled
    try:
        yield relay
    finally:
        await relay.stop()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("*", "SB900001"),
        ("rtsp://h/comelit", "SB900001"),
        ("rtsp://h/comelit/", "SB900001"),
        ("rtsp://h/comelit/trackID=0", "SB900001"),
        ("rtsp://h/comelit/SB900002", "SB900002"),
        ("rtsp://h/comelit/SB900002/trackID=0", "SB900002"),
        ("rtsp://h/comelit/SB900001", "SB900001"),
        ("rtsp://h/comelit/SB999999", None),
        ("rtsp://h/comelit/SB900002/SB900001", None),
        ("rtsp://h/other", None),
        ("rtsp://h/comelitx", None),
    ],
)
def test_target_of(url, expected):
    relay = RtspRelay(_never, targets=("SB900001", "SB900002"))

    assert relay._target_of(url) == expected


def test_url_per_target():
    relay = RtspRelay(_never, targets=("SB900001", "SB900002"))

    assert relay.url().endswith("/comelit")
    assert relay.url(target="SB900001").endswith("/comelit")
    assert relay.url(target="SB900002").endswith("/comelit/SB900002")


async def test_named_target_dialled(doors, talk):
    viewer = await talk(doors)
    viewer.url = f"{viewer.url}/SB900002"

    await viewer.play()

    assert doors.dialled == ["SB900002"]
    assert doors.serving == "SB900002"


async def test_busy_with_other_target_503(doors, talk):
    first = await talk(doors)
    await first.play()
    second = await talk(doors)
    second.url = f"{second.url}/SB900002"

    code, _headers, _body = await second.request("DESCRIBE", Accept="application/sdp")

    assert code == 503
    assert doors.dialled == ["SB900001"]


async def test_targets_required():
    with pytest.raises(ValueError):
        RtspRelay(_never, targets=())


def test_sdp_parameter_sets_per_target():
    relay = RtspRelay(_never, targets=("SB900001", "SB900002"))
    relay._remember_parameter_sets("SB900001", RtpPacket(True, 96, 1, 0, 0, b"\x67\x42\x00\x1e"))
    relay._remember_parameter_sets("SB900001", RtpPacket(True, 96, 2, 0, 0, b"\x68\xce"))

    assert "sprop-parameter-sets" in relay._sdp("SB900001")
    assert "sprop-parameter-sets" not in relay._sdp("SB900002")


def test_viewers_is_derived():
    relay = RtspRelay(_never)
    client = _Client(FakeWriter(), "s")
    relay._clients.add(client)

    assert relay.viewers == 0
    client.playing = True
    assert relay.viewers == 1
