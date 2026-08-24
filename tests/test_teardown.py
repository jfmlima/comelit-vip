"""Teardown of the session and the relay."""

from __future__ import annotations

import asyncio

import pytest
from fake_panel import ENTRANCE, FakePanel, until

from custom_components.comelit_vip.viper.rtsp import RtspRelay
from custom_components.comelit_vip.viper.session import ViperSession


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
    await _register(session)
    try:
        yield session
    finally:
        await session.close()


# ---------------------------------------------------------------- the session
async def test_ring_after_reconnect(session, panel):
    rings = []
    session.on_ring = rings.append

    await panel.ring()
    await until(lambda: len(rings) == 1)
    await panel.drop()
    await until(lambda: not session.connected)
    await _register(session)
    await panel.ring()
    await until(lambda: len(rings) == 2)

    assert len(rings) == 2


async def test_drop_clears_state(session, panel):
    await panel.ring()
    await until(lambda: bool(session._connections and session._call_tasks))

    await panel.drop()
    # `connected` goes false before `_teardown` clears the connections.
    await until(lambda: not session.connected and not session._connections and not session._call_tasks)

    assert session._connections == {}
    assert session._call_tasks == set()
    assert session._ctpp_handle is None
    assert not session.connected


async def test_close_reports_call_end(session, panel):
    ended = []
    session.on_call_end = lambda event, cause: ended.append(cause)

    await panel.ring()
    await until(lambda: bool(session._call_tasks))
    await session.close()
    await until(lambda: ended == [None])

    assert ended == [None]


async def test_close_from_callback(session, panel):
    closes = []

    def _on_end(event, cause):
        closes.append(asyncio.get_running_loop().create_task(session.close()))

    session.on_call_end = _on_end
    connection = await panel.ring()
    await until(lambda: bool(session._call_tasks))
    await panel.release(connection)
    await until(lambda: bool(closes))

    assert closes
    async with asyncio.timeout(5):
        await asyncio.gather(*closes)
    assert not session.connected


async def test_close_no_disconnect_callback(session, panel):
    lost = []
    session.on_disconnect = lost.append

    await session.close()
    await until(lambda: not session.connected)

    assert lost == []


# ------------------------------------------------------------------ the relay
class FakeCall:
    """Stands in for a video call without a panel behind it."""

    def __init__(self) -> None:
        self.closed = False
        self.ended = asyncio.Event()

    async def packets(self):
        await self.ended.wait()
        return
        yield  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        self.closed = True
        self.ended.set()


class FakeViewer:
    """A client that counts as watching."""

    playing = True
    writer = None


async def test_abandon_call():
    call = FakeCall()
    relay = RtspRelay(_factory_for([call]))
    await relay._ensure_call()

    await relay.abandon_call()

    assert relay._episode is None
    assert call.closed


async def test_ended_call_not_retained():
    call = FakeCall()
    relay = RtspRelay(_factory_for([call]))
    await relay._ensure_call()

    call.ended.set()
    await until(lambda: relay._episode is None)

    assert relay._episode is None


async def test_abandon_during_restart():
    """The pump holds the lock while it asks for the next call."""
    first, second = FakeCall(), FakeCall()
    blocked = asyncio.Event()
    started = asyncio.Event()

    async def _factory(target=None):
        if first.ended.is_set():
            started.set()
            await blocked.wait()
            return second
        return first

    relay = RtspRelay(_factory)
    relay._clients.add(FakeViewer())
    await relay._ensure_call()

    first.ended.set()
    await asyncio.wait_for(started.wait(), 5)
    async with asyncio.timeout(5):
        await relay.abandon_call()

    assert relay._episode is None
    blocked.set()


def _factory_for(calls):
    queue = list(calls)

    async def _factory(target=None):
        return queue.pop(0)

    return _factory


async def _register(session: ViperSession) -> None:
    await session.connect()
    await session.authenticate("t" * 32)
    await session.get_configuration()
    await session.start_ctp()
    assert session.config is not None and session.config.entrance == ENTRANCE


# ------------------------------------------------------------------ end_task
async def test_end_task_reraises_own_cancellation():
    from custom_components.comelit_vip.viper.tasks import end_task

    inner = asyncio.create_task(asyncio.sleep(10))

    async def outer() -> None:
        await end_task(inner)

    task = asyncio.create_task(outer())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner.cancelled()


async def test_end_task_swallows_inner_cancellation():
    from custom_components.comelit_vip.viper.tasks import end_task

    inner = asyncio.create_task(asyncio.sleep(10))
    await end_task(inner)

    assert inner.cancelled()
