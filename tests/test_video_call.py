"""The outgoing video call over a real socket."""

from __future__ import annotations

import asyncio

import pytest
from fake_panel import ENTRANCE, FakePanel, until

from custom_components.comelit_vip.viper.call import VIDEO_QUEUE_SIZE, VideoCall
from custom_components.comelit_vip.viper.rtp import RtpPacket
from custom_components.comelit_vip.viper.session import ViperError, ViperSession


@pytest.fixture
async def panel(socket_enabled):
    """A panel listening on a free port."""
    panel = FakePanel()
    await panel.start()
    try:
        yield panel
    finally:
        await panel.stop()


@pytest.fixture
async def session(panel):
    """A session already registered with that panel."""
    session = ViperSession(panel.host, panel.port)
    await session.connect()
    await session.authenticate("t" * 32)
    await session.get_configuration()
    await session.start_ctp()
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
def brisk(monkeypatch):
    """Short CTP timeout."""
    monkeypatch.setattr("custom_components.comelit_vip.viper.session.CTP_TIMEOUT", 0.25)


async def test_open_call(session, panel):
    call = VideoCall(session, ENTRANCE)

    await call.open()

    assert panel.calls == 1
    assert call.conn is not None
    assert call.audio_handle is not None
    assert call.video_handle is not None
    assert call.media_handle == (call.video_handle + 1) & 0xFFFF
    await call.close()


async def test_video_on_next_handle(session, panel):
    """The panel sends video on the handle after the one it was offered."""
    call = VideoCall(session, ENTRANCE)
    await call.open()
    seen = []

    async def _drain() -> None:
        async for packet in call.packets():
            seen.append(packet)

    reader = asyncio.create_task(_drain())
    await panel.push_video(b"\x65hello")
    await until(lambda: bool(seen))

    assert seen[0].payload == b"\x65hello"
    assert seen[0].payload_type == 96
    await call.close()
    await reader


async def test_release_ends_stream(session, panel):
    call = VideoCall(session, ENTRANCE)
    await call.open()
    finished = asyncio.Event()

    async def _drain() -> None:
        async for _packet in call.packets():
            pass
        finished.set()

    reader = asyncio.create_task(_drain())
    await panel.end_call()

    await asyncio.wait_for(finished.wait(), 5)
    await call.close()
    await reader


async def test_close_releases_channels(session, panel):
    call = VideoCall(session, ENTRANCE)
    await call.open()
    handles = set(panel.channels)

    await call.close()
    await until(lambda: set(panel.channels) < handles)

    assert call.conn is None
    assert call.video_handle is None
    assert session._connections.get(call.conn.peer_id if call.conn else b"") is None


async def test_unanswered_open_cleans_up(session, panel, brisk):
    panel.answer_call = False
    call = VideoCall(session, ENTRANCE)

    with pytest.raises(ViperError):
        await call.open(timeout=0.25)

    assert panel.calls == 1
    assert call.conn is None
    assert call.audio_handle is None


async def test_end_marker_on_full_queue(session, panel):
    call = VideoCall(session, ENTRANCE)
    await call.open()
    for _ in range(VIDEO_QUEUE_SIZE):
        call._video_q.put_nowait(_packet())
    assert call._video_q.full()
    finished = asyncio.Event()

    async def _drain() -> None:
        async for _packet_in in call.packets():
            pass
        finished.set()

    reader = asyncio.create_task(_drain())
    call._signal_end()

    await asyncio.wait_for(finished.wait(), 5)
    await call.close()
    await reader


async def test_open_twice_noop(session, panel):
    call = VideoCall(session, ENTRANCE)
    await call.open()

    await call.open()

    assert panel.calls == 1
    await call.close()


async def test_close_twice(session, panel):
    call = VideoCall(session, ENTRANCE)
    await call.open()

    await call.close()
    await call.close()

    assert call.conn is None


def _packet() -> RtpPacket:
    return RtpPacket(marker=True, payload_type=96, sequence=1, timestamp=1, ssrc=1, payload=b"\x65")


async def test_ring_on_call_id(session, panel):
    rings = []
    session.on_ring = rings.append
    call = VideoCall(session, ENTRANCE)
    await call.open()
    assert panel.call_connection is not None

    await panel.ring(connection=panel.call_connection)
    await until(lambda: len(rings) == 1)
    async with asyncio.timeout(2):
        assert [packet async for packet in call.packets()] == [], "the stream ends"
    await call.close()

    assert panel.releases == 0
    assert panel.call_connection in session._connections


async def test_media_handle_reserved(session, panel):
    """The next channel opened must not land on the handle the panel sends video on."""
    call = VideoCall(session, ENTRANCE)
    await call.open()

    handle = await session.open_channel("INFO")

    assert handle != call.media_handle
    await session.close_channel(handle)
    await call.close()


async def test_open_cancelled_cleans_up(session, panel):
    panel.answer_call = False
    call = VideoCall(session, ENTRANCE)
    opening = asyncio.create_task(call.open())
    await until(lambda: panel.calls == 1)

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    await until(lambda: panel.releases == 1)

    assert call.conn is None
    assert session._binary_handlers == {}
    assert session._incoming_channel_handler is None
    assert len(session._connections) == 1, "only the registration remains"


async def test_panel_opened_handle_not_reused(session, panel):
    call = VideoCall(session, ENTRANCE)
    await call.open()
    theirs = await panel.open_channel_towards_client("RTPC")
    await until(lambda: call.remote_media_handle == theirs)
    session._handles = iter([theirs, theirs + 1])

    assert await session.open_channel("INFO") == theirs + 1
    await call.close()


async def test_close_cut_short_is_finished_by_the_next_close(session, panel):
    call = VideoCall(session, ENTRANCE)
    await call.open()
    handles = {call.audio_handle, call.video_handle, call.udp_handle}
    sent = asyncio.Event()
    real_send = session.send_ctp

    async def _slow_send(packet):
        sent.set()
        await asyncio.sleep(10)
        await real_send(packet)

    session.send_ctp = _slow_send
    closing = asyncio.create_task(call.close())
    await sent.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert call.conn is None and session._binary_handlers == {}, "local state was cleaned before the send"
    assert panel.releases == 0

    session.send_ctp = real_send
    await call.close()
    await until(lambda: panel.releases == 1)
    await until(lambda: not handles & set(panel.channels))


async def test_refused_call_fails_at_once(session, panel):
    """A 6741W answers an INVITE during a ring with RELEASE cause 8, not a timeout."""
    from custom_components.comelit_vip.viper.session import ViperRefusedError

    panel.refuse_call_cause = 8
    call = VideoCall(session, ENTRANCE)
    started = asyncio.get_running_loop().time()
    with pytest.raises(ViperRefusedError) as err:
        await call.open(timeout=5)

    assert err.value.cause == 8
    assert asyncio.get_running_loop().time() - started < 1
    assert call.conn is None


async def test_silent_call_ends(session, panel, monkeypatch):
    """A 6741W stops sending video after ~36 s and never releases the call."""
    monkeypatch.setattr("custom_components.comelit_vip.viper.call.MEDIA_SILENCE", 0.3)
    call = VideoCall(session, ENTRANCE)
    await call.open()
    await panel.push_video()
    await panel.push_video(sequence=2)
    started = asyncio.get_running_loop().time()
    async with asyncio.timeout(3):
        received = [packet async for packet in call.packets()]

    assert len(received) == 2
    assert 0.2 <= asyncio.get_running_loop().time() - started < 2
    await call.close()


async def test_call_with_no_video_at_all_ends(session, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.viper.call.MEDIA_START_TIMEOUT", 0.3)
    call = VideoCall(session, ENTRANCE)
    await call.open()
    async with asyncio.timeout(3):
        assert [packet async for packet in call.packets()] == []
    await call.close()


async def test_video_request_is_repeated(session, panel, monkeypatch):
    """A 6741W stops video ~36 s after the last request; repeating it keeps video flowing."""
    monkeypatch.setattr("custom_components.comelit_vip.viper.call.MEDIA_REFRESH", 0.1)
    call = VideoCall(session, ENTRANCE)
    await call.open()
    at_open = panel.media_requests

    await until(lambda: panel.media_requests >= at_open + 3)
    await call.close()
    await asyncio.sleep(0.3)
    settled = panel.media_requests
    await asyncio.sleep(0.3)

    assert panel.media_requests == settled, "no requests after close"
