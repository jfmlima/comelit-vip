"""Rings landing on a connection id this client already holds.

Outbound connections are keyed by the id the panel answers with, which has the
top bit set; the panel's own connection ids have it set too.
"""

from __future__ import annotations

import asyncio
import struct
from itertools import count

import pytest
from fake_panel import ENTRANCE, OUR_ADDRESS, FakePanel, until

from custom_components.comelit_vip.viper.ctp import FLAG_SYN, OP_INVITE, encode_logaddr, peer_connection_id
from custom_components.comelit_vip.viper.frames import MGMT_HANDLE
from custom_components.comelit_vip.viper.session import FIRST_HANDLE, ViperConnectionError, ViperSession


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


def _events(session: ViperSession) -> list[tuple[str, bytes]]:
    seen: list[tuple[str, bytes]] = []
    session.on_ring = lambda event: seen.append(("ring", event.call_id))
    session.on_call_end = lambda event, cause: seen.append(("end", event.call_id))

    return seen


async def test_ring_on_registration_id(session, panel):
    seen = _events(session)
    assert panel.registration_connection is not None

    await panel.ring(connection=panel.registration_connection)
    await until(lambda: len(seen) == 1)

    assert seen == [("ring", b"\xaa\xbb\xcc\xdd")]
    assert session._registration is None
    assert session.registered


async def test_ring_on_door_open_id(session, panel):
    seen = _events(session)
    panel.answer_door = False
    opening = asyncio.create_task(session.open_door(ENTRANCE, 1))
    await until(lambda: panel.door_connection is not None)

    await panel.ring(connection=panel.door_connection)
    await until(lambda: len(seen) == 1)
    with pytest.raises(ViperConnectionError):
        async with asyncio.timeout(2):
            await opening

    assert seen == [("ring", b"\xaa\xbb\xcc\xdd")]
    acks_before = panel.acks
    await panel.release(panel.door_connection)
    await until(lambda: len(seen) == 2)

    assert seen[1] == ("end", b"\xaa\xbb\xcc\xdd")
    assert panel.acks > acks_before


async def test_retransmitted_invite_rings_once(session, panel):
    seen = _events(session)

    await panel.ring()
    await panel.ring()
    await until(lambda: len(seen) >= 1)
    await asyncio.sleep(0.1)

    assert seen == [("ring", b"\xaa\xbb\xcc\xdd")]


async def test_new_call_on_stale_id(session, panel):
    seen = _events(session)

    await panel.ring(call_id=b"\x00\x00\x00\x01")
    await until(lambda: len(seen) == 1)
    await panel.ring(call_id=b"\x00\x00\x00\x02")
    await until(lambda: len(seen) == 3)

    assert seen == [("ring", b"\x00\x00\x00\x01"), ("end", b"\x00\x00\x00\x01"), ("ring", b"\x00\x00\x00\x02")]

    await panel.release(b"\x77\x0c")
    await until(lambda: len(seen) == 4)

    assert seen[3] == ("end", b"\x00\x00\x00\x02")


async def test_failed_connection_sends_nothing(session, panel):
    conn = session.new_call_connection(ENTRANCE)
    conn.fail()

    with pytest.raises(ViperConnectionError):
        await conn.fin()
    with pytest.raises(ViperConnectionError):
        await conn.wait(timeout=1)
    with pytest.raises(ViperConnectionError):
        await conn.wait(timeout=1)


async def test_release_checks_identity(session, panel):
    conn = session.new_call_connection(ENTRANCE)
    session._displace(conn)
    other = session.new_call_connection(ENTRANCE)
    session._connections[conn.peer_id] = other

    session.release_connection(conn)

    assert session._connections[conn.peer_id] is other


async def test_invite_echo_not_a_ring(session, panel):
    seen = _events(session)
    conn = session.new_call_connection(ENTRANCE)
    conn.call_id = b"\xaa\xbb\xcc\xdd"

    # A panel that answers SYN+ACK, with our call id in the body.
    await _invite_on(panel, conn.peer_id, call_id=b"\xaa\xbb\xcc\xdd", flags=FLAG_SYN)
    await until(lambda: not conn.inbox.empty())

    assert seen == []
    assert (await conn.wait(timeout=1)).opcode == OP_INVITE


async def _invite_on(panel: FakePanel, connection: bytes, *, call_id: bytes, flags: int) -> None:
    body = (
        struct.pack(">H", OP_INVITE)
        + encode_logaddr(ENTRANCE)
        + encode_logaddr(OUR_ADDRESS)
        + b"\x01\x20"
        + call_id
        + encode_logaddr(ENTRANCE)
        + b"II"
    )
    await panel._send_ctp(connection, body, flags)


# ------------------------------------------------------------------ handles
async def test_handles_reset_per_socket(panel):
    session = ViperSession(panel.host, panel.port)
    await session.connect()
    first = await session.open_channel("INFO")
    await session.open_channel("INFO")
    await session.close()

    await session.connect()
    again = await session.open_channel("INFO")
    await session.close()

    assert first == FIRST_HANDLE
    assert again == FIRST_HANDLE


async def test_handle_zero_skipped(panel):
    session = ViperSession(panel.host, panel.port)
    await session.connect()
    session._handles = count(0xFFFF)

    handles = [await session.open_channel("INFO") for _ in range(2)]
    await session.close()

    assert MGMT_HANDLE not in handles
    assert handles == [0xFFFF, 0x0001]


def test_peer_id_top_bit():
    assert peer_connection_id(b"\x01\x11") == b"\x81\x11"


# ------------------------------------------------------------------ teardown
async def test_teardown_fails_waiters(session, panel):
    """A waiter on a CTP connection fails when the socket dies, not at its own timeout."""
    panel.answer_door = False
    opening = asyncio.create_task(session.open_door(ENTRANCE, 1))
    await until(lambda: panel.door_connection is not None)

    await panel.drop()
    with pytest.raises(ViperConnectionError, match="closed"):
        async with asyncio.timeout(2):
            await opening


async def test_handles_skip_open_channels(session, panel):
    assert session._ctpp_handle is not None
    session._handles = count(session._ctpp_handle)

    handle = await session.open_channel("INFO")

    assert handle == session._ctpp_handle + 2  # CTPP and CSPB are both open
