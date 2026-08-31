"""The session against the fake panel, over a real socket."""

from __future__ import annotations

import asyncio

import pytest
from fake_panel import ENTRANCE, OUR_ADDRESS, FakePanel, until

from custom_components.comelit_vip.viper.models import RingEvent
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


async def test_register(session, panel):
    assert session.source == OUR_ADDRESS
    assert panel.registrations == 1
    assert set(panel.channels.values()) >= {"CTPP", "CSPB"}


async def test_invite_rings(session, panel):
    rings: list[RingEvent] = []
    session.on_ring = rings.append

    await panel.ring()
    await until(lambda: len(rings) == 1)

    assert len(rings) == 1
    assert rings[0].origin == ENTRANCE
    assert rings[0].tag == b"PP"


async def test_release_ends_call(session, panel):
    ended: list[int | None] = []
    session.on_call_end = lambda event, cause: ended.append(cause)

    connection = await panel.ring()
    await until(lambda: bool(session._call_tasks))
    await panel.release(connection, cause=3)
    await until(lambda: ended == [3])

    assert ended == [3]


# ------------------------------------------------------------------ rings
async def test_ring_with_non_ascii_caller(session, panel):
    """The caller's address is echoed in the acknowledgement."""
    rings: list[RingEvent] = []
    session.on_ring = rings.append
    answered = panel.acks

    await panel.ring(source_raw=b"SB\xc3\x9c0001\x00\x00")
    await until(lambda: bool(rings))
    await until(lambda: panel.acks > answered)

    assert rings[0].origin == ENTRANCE


async def test_ring_reported_before_ack(session, panel):
    rings: list[RingEvent] = []
    ended: list[int | None] = []
    session.on_ring = rings.append
    session.on_call_end = lambda event, cause: ended.append(cause)
    real = session._send_frame
    refused = False

    async def _refuse_once(handle, payload):
        nonlocal refused
        if handle == session._ctpp_handle and not refused:
            refused = True
            raise OSError("write failed")
        return await real(handle, payload)

    session._send_frame = _refuse_once

    await panel.ring()
    await until(lambda: bool(rings))
    await until(lambda: bool(ended))

    assert ended == [None]
    assert session._connections.get(b"\x77\x0c") is None


async def test_non_invite_syn_not_kept(session, panel):
    before = len(session._connections)
    # The registration already counted one ack.
    answered = panel.acks

    await panel.knock(0x0099)
    await until(lambda: panel.acks > answered)

    assert len(session._connections) == before
    assert session._call_tasks == set()


async def test_open_door(session, panel):
    await session.open_door(ENTRANCE, 1)

    assert panel.opened == [(ENTRANCE, 1)]


async def test_drop_reported(session, panel):
    lost: list[Exception | None] = []
    session.on_disconnect = lost.append

    await panel.drop()
    await until(lambda: bool(lost))

    assert len(lost) == 1
    assert not session.connected


# ------------------------------------------------------------------- wire
async def test_open_door_refused(session, panel):
    panel.open_cause = 5

    with pytest.raises(ViperError, match="cause 5"):
        await session.open_door(ENTRANCE, 1)


async def test_open_door_during_ring_is_busy(session, panel):
    """A 6741W answers a door release with cause 8 while it rings."""
    panel.open_cause = 8

    with pytest.raises(ViperError, match="busy with a call"):
        await session.open_door(ENTRANCE, 1)


async def test_incoming_channel_acked(session, panel):
    handle = await panel.open_channel_towards_client("RTPC")

    await until(lambda: handle in panel.client_acked)


async def test_close_fails_waiters(session, panel):
    panel.answer_server_info = False
    pending = asyncio.create_task(session.server_info())
    await until(lambda: panel.server_infos >= 1)

    await session.close()

    with pytest.raises(ViperError):
        await asyncio.wait_for(pending, 5)


async def test_panel_renewal_answered(session, panel):
    """The panel may renew in band and the client answers with a FIN. A 6741W never does."""
    await panel.renew_registration()
    await until(lambda: session.renewals >= 1)

    assert session.renewals == 1


async def test_close_from_cancelled_task(session, panel):
    """A task the teardown cancels may itself call close()."""
    ran = 0
    real = session._fail_waiters

    def _count(error):
        nonlocal ran
        ran += 1
        real(error)

    async def _asks_again() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await session.close()
            raise

    session._fail_waiters = _count
    session._call_tasks.add(asyncio.create_task(_asks_again()))

    await session.close()

    assert ran == 1
    assert session._tearing_down is False
