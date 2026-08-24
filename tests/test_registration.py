"""Registration: the lease that decides availability.

The panel grants a lease of about an hour and never asks for it back. When it
runs out calls stop arriving over a connection that stays up and answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

import pytest
from fake_panel import FakePanel, until

from custom_components.comelit_vip.const import (
    CONNECTION_MAX_AGE,
    KEEPALIVE_INTERVAL,
    REGISTRATION_REFRESH,
    REGISTRATION_TTL,
    STABLE_CONNECTION,
    WATCHDOG_INTERVAL,
)
from custom_components.comelit_vip.hub import ComelitVipHub
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
def impatient(monkeypatch):
    """Short CTP timeout. Request it last: it also applies to the fixtures before it."""
    monkeypatch.setattr("custom_components.comelit_vip.viper.session.CTP_TIMEOUT", 0.25)


@pytest.fixture
def quick_backoff(monkeypatch):
    """Zero first backoff."""
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0,))


@pytest.fixture
def brisk(monkeypatch):
    """Fast watchdog ticks."""
    monkeypatch.setattr("custom_components.comelit_vip.hub.WATCHDOG_INTERVAL", 0.01)


@pytest.fixture
async def hub(hass, panel):
    """A hub connected to that panel, with no relay and no supervisor task."""
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    await hub._connect()
    try:
        yield hub
    finally:
        hub._stopping = True
        await hub.session.close()


@pytest.fixture
async def supervised(hub):
    """The hub with its supervisor running. Separate from ``hub``: tests on that drive one job at a time and would race it."""
    hub._supervisor = asyncio.create_task(hub._supervise(), name="test.link")
    try:
        yield hub
    finally:
        hub._stopping = True
        hub._changed.set()
        # A test that unloads the hub has already stopped it.
        if hub._supervisor is not None:
            await _cancel(hub._supervisor)
            hub._supervisor = None


# ------------------------------------------------------------------ the timing
def test_refresh_interval():
    assert REGISTRATION_REFRESH * 4 <= REGISTRATION_TTL
    assert WATCHDOG_INTERVAL < REGISTRATION_REFRESH


def test_recycle_before_ttl():
    assert CONNECTION_MAX_AGE + WATCHDOG_INTERVAL < REGISTRATION_TTL


# ----------------------------------------------------------------- the session
async def test_register(panel):
    session = ViperSession(panel.host, panel.port)
    await _connect(session)

    assert session.registered
    assert session.registration_age is not None
    await session.close()


async def test_unanswered_register_raises(panel, impatient):
    panel.answer_registration = False
    session = ViperSession(panel.host, panel.port)
    await session.connect()
    await session.authenticate("t" * 32)
    await session.get_configuration()

    with pytest.raises(ViperError):
        await session.start_ctp()

    assert not session.registered
    await session.close()


async def test_refresh_keeps_connection(panel):
    session = ViperSession(panel.host, panel.port)
    await _connect(session)
    first = session.registered_at

    await session.refresh_registration()

    assert panel.registrations == 2
    assert panel.connections == 1
    assert session.registered_at != first
    assert session.refreshes == 1
    await session.close()


async def test_refresh_no_leak(panel):
    session = ViperSession(panel.host, panel.port)
    await _connect(session)
    before = len(session._connections)

    await session.refresh_registration()
    await session.refresh_registration()

    assert len(session._connections) == before
    await session.close()


async def test_close_clears_registration(panel):
    session = ViperSession(panel.host, panel.port)
    await _connect(session)

    await session.close()

    assert not session.registered
    assert session.registration_age is None


# --------------------------------------------------------------------- the hub
async def test_available_means_registered(hub):
    assert hub.available

    hub.session.registered_at = None

    assert hub.session.connected
    assert not hub.available


async def test_watchdog_renews_old_lease(hub, panel):
    hub.session.registered_at -= REGISTRATION_REFRESH + 1

    assert await hub._keep_registered()
    assert panel.registrations == 2
    assert hub.available


async def test_watchdog_skips_young_lease(hub, panel):
    assert await hub._keep_registered()

    assert panel.registrations == 1


async def test_failed_refresh_reconnects(quick_backoff, supervised, panel, impatient):
    hub = supervised
    panel.answer_registration = False
    hub.session.registered_at -= REGISTRATION_REFRESH + 1

    assert not await hub._keep_registered()

    assert not hub.available
    panel.answer_registration = True
    await until(lambda: hub.available)

    assert panel.connections == 2


async def test_probe_independent_of_refresh(hub, panel, caplog):
    hub.session.last_rx -= KEEPALIVE_INTERVAL + 1

    with caplog.at_level(logging.DEBUG):
        await hub._watchdog_pass()

    assert panel.registrations == 1
    assert hub.available


async def test_recycle_old_connection(quick_backoff, brisk, supervised, panel):
    hub = supervised
    hub._connected_at -= CONNECTION_MAX_AGE + 1

    await until(lambda: panel.connections >= 2)
    await until(lambda: hub.available)

    assert panel.connections == 2
    assert hub._attempt == 0


async def test_no_recycle_during_call(hub, panel):
    hub._connected_at -= CONNECTION_MAX_AGE + 1
    await panel.ring()
    for _ in range(8):
        await asyncio.sleep(0)
    assert hub.session.calls_active

    await hub._recycle_if_stale(asyncio.get_running_loop().time())

    assert panel.connections == 1


async def test_supervisor_survives_exception(hub, brisk):
    passes = 0
    kept_going = asyncio.Event()

    async def _explode() -> None:
        nonlocal passes
        passes += 1
        if passes >= 3:
            kept_going.set()
        raise RuntimeError("boom")

    hub._watchdog_pass = _explode
    task = asyncio.create_task(hub._supervise())
    await asyncio.wait_for(kept_going.wait(), 5)
    hub._stopping = True
    await _cancel(task)

    assert passes >= 3


# ------------------------------------------------------------ the supervisor
async def test_socket_dies_during_connect(quick_backoff, supervised, panel, monkeypatch):
    hub = supervised
    dropped = False

    async def _drop_once(host: str, **kwargs):
        nonlocal dropped
        if not dropped:
            dropped = True
            # Discovery runs beside the handshake; drop only once that is done.
            await until(lambda: hub.session.registered)
            await panel.drop()
            await until(lambda: not hub.session.connected)
        return None

    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _drop_once)
    await panel.drop()
    await until(lambda: panel.connections >= 3, passes=600)
    await until(lambda: hub.available)

    assert panel.connections == 3
    await panel.ring()
    await until(lambda: hub.last_ring is not None)


async def test_shutdown_during_recycle(quick_backoff, brisk, supervised, panel):
    hub = supervised
    closing = asyncio.Event()
    real_close = hub.session.close

    async def _stubborn() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0.2)

    async def _watched_close() -> None:
        closing.set()
        await real_close()

    stubborn = asyncio.create_task(_stubborn())
    hub.session._call_tasks.add(stubborn)
    # The stub task above must not count as a call.
    hub._calls_in_progress = lambda: False
    hub.session.close = _watched_close
    hub._connected_at -= CONNECTION_MAX_AGE + 1
    await asyncio.wait_for(closing.wait(), 5)

    await hub.async_shutdown()
    await stubborn

    assert panel.connections == 1


async def test_slow_handshake_kept(quick_backoff, brisk, supervised, panel):
    hub = supervised
    panel.configuration_delay = 0.2

    await panel.drop()
    await until(lambda: panel.connections >= 2)
    await until(lambda: hub.available, passes=600)

    assert panel.connections == 2
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert panel.connections == 2


async def test_unregistered_link_rebuilt(quick_backoff, brisk, supervised, panel):
    hub = supervised
    hub.session.registered_at = None

    assert not hub.available
    await until(lambda: panel.connections >= 2, passes=600)
    await until(lambda: hub.available)

    assert panel.connections == 2


async def test_silent_panel_reconnects(quick_backoff, brisk, supervised, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.viper.session.REQUEST_TIMEOUT", 0.2)
    hub = supervised
    panel.answer_server_info = False
    hub.session.last_rx -= KEEPALIVE_INTERVAL + 1

    await until(lambda: panel.server_infos >= 1, passes=600)
    await until(lambda: panel.connections >= 2, passes=600)
    await until(lambda: hub.available)

    assert panel.connections == 2


# ---------------------------------------------------------------- the ladder
async def test_backoff_grows_on_failure(hass, socket_enabled, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0.05, 0.5, 0.5))
    hub = ComelitVipHub(hass, "entry", host="127.0.0.1", token="t" * 32, port=_closed_port(), rtsp_port=0)
    task = asyncio.create_task(hub._supervise())
    started = asyncio.get_running_loop().time()
    try:
        await until(lambda: hub._attempt >= 4, passes=600)
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        hub._stopping = True
        await _cancel(task)

    assert elapsed >= 1.0, f"four attempts took {elapsed:.2f}s"


async def test_stable_link_resets_backoff(supervised, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0.05, 5.0, 5.0))
    hub = supervised
    hub._attempt = 2
    hub._connected_at -= STABLE_CONNECTION

    await panel.drop()
    await until(lambda: panel.connections >= 2, passes=200)
    await until(lambda: hub.available)

    assert panel.connections == 2
    assert hub._attempt == 1


async def test_unregistered_link_keeps_backoff(supervised, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0.01,))
    # With STABLE_CONNECTION at 0 every connection counts as stable.
    monkeypatch.setattr("custom_components.comelit_vip.hub.STABLE_CONNECTION", 0.0)
    hub = supervised

    async def _drop_always(host: str, **kwargs):
        # Discovery runs beside the handshake; drop only once that is done.
        await until(lambda: hub.session.registered)
        await panel.drop()
        await until(lambda: not hub.session.connected)
        return None

    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _drop_always)
    await panel.drop()

    await until(lambda: hub._attempt >= 4, passes=600)


def _closed_port() -> int:
    """A port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _connect(session: ViperSession) -> None:
    await session.connect()
    await session.authenticate("t" * 32)
    await session.get_configuration()
    await session.start_ctp()


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
