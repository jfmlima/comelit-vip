"""Hub behaviour that needs no socket, and a few that do."""

from __future__ import annotations

import asyncio

import pytest
from fake_panel import ENTRANCE, FakePanel, until

from custom_components.comelit_vip.const import CONNECTION_MAX_AGE, DEFAULT_RTSP_PORT
from custom_components.comelit_vip.hub import ComelitVipHub
from custom_components.comelit_vip.viper.rtsp import DEFAULT_HOST, EXPOSED_HOST
from custom_components.comelit_vip.viper.session import ViperAuthError, ViperError, ViperRefusedError


@pytest.fixture
def hub(hass):
    """A hub with no panel behind it."""
    return ComelitVipHub(hass, "entry", host="192.0.2.1", token="t" * 32, port=64100, rtsp_port=DEFAULT_RTSP_PORT)


@pytest.fixture
async def panel(socket_enabled):
    panel = FakePanel()
    await panel.start()
    try:
        yield panel
    finally:
        await panel.stop()


# ------------------------------------------------------------ what is offered
async def test_open_requires_link(hub):
    with pytest.raises(ViperError, match="not connected"):
        await hub.async_open(ENTRANCE, 1)


def test_stream_url_loopback_unless_exposed(hub):
    hub.relay = _FakeRelay()
    hub.rtsp_host = "192.0.2.50"

    assert "127.0.0.1" in hub.stream_url()

    hub.expose_stream = True

    assert "192.0.2.50" in hub.stream_url()


def test_no_relay_no_url(hub):
    assert hub.stream_url() is None


def test_listener_error_isolated(hub):
    seen = []
    hub.add_listener(lambda kind, data: (_ for _ in ()).throw(RuntimeError("no")))
    hub.add_listener(lambda kind, data: seen.append(kind))

    hub._notify("connection", {"available": True})

    assert seen == ["connection"]


def test_listener_unsubscribe(hub):
    seen = []
    remove = hub.add_listener(lambda kind, data: seen.append(kind))

    remove()
    hub._notify("connection", {"available": True})

    assert seen == []


# ---------------------------------------------------- what the link does next
async def test_relay_bind_host(hass, panel, monkeypatch):
    bound = []

    async def _remember(self):
        bound.append(self.host)
        return ""

    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    monkeypatch.setattr("custom_components.comelit_vip.viper.rtsp.RtspRelay.start", _remember)
    for expose in (False, True):
        hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0, expose_stream=expose)
        await hub.async_setup()
        await hub.async_shutdown()

    assert bound == [DEFAULT_HOST, EXPOSED_HOST]


async def test_lost_link_abandons_call(hass, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0,))
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    await hub._connect()
    relay = _FakeRelay()
    hub.relay = relay
    hub._supervisor = asyncio.create_task(hub._supervise())
    try:
        await panel.drop()
        await until(lambda: panel.connections >= 2)
        await until(lambda: hub.available)
    finally:
        hub._stopping = True
        hub._changed.set()
        hub._supervisor.cancel()
        await asyncio.gather(hub._supervisor, return_exceptions=True)
        await hub.session.close()

    assert relay.abandoned == 1


async def test_reauth_after_repeated_refusals(hass, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0,))
    asked = []
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    monkeypatch.setattr(hub, "_connect", _raise_auth)
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda entry_id: _FakeEntry(asked))

    assert await hub._raise_link() is True
    assert await hub._raise_link() is True
    assert asked == []

    assert await hub._raise_link() is False
    assert asked == ["reauth"]


async def test_auth_failures_reset(hass, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0,))
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    asked = []
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda entry_id: _FakeEntry(asked))
    real_connect = hub._connect
    monkeypatch.setattr(hub, "_connect", _raise_auth)
    await hub._raise_link()
    await hub._raise_link()
    monkeypatch.setattr(hub, "_connect", real_connect)

    assert await hub._raise_link() is True
    assert hub.available
    assert hub._auth_failures == 0

    hub._stopping = True
    await hub.session.close()


async def test_recycle_is_quiet(hass, panel, monkeypatch):
    """A planned replacement is not two logbook rows every fifty minutes."""
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0,))
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    await hub._connect()
    seen = []
    hub.add_listener(lambda kind, data: seen.append(data.get("available")))
    hub._connected_at -= CONNECTION_MAX_AGE + 1

    await hub._recycle_if_stale(asyncio.get_running_loop().time())

    assert seen == []
    assert hub.available, "still up as far as the entities know"

    assert await hub._raise_link() is True

    assert seen == [True]
    assert hub.session.registered
    hub._stopping = True
    await hub.session.close()


async def test_failed_replacement_reported(hass, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0,))
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    await hub._connect()
    seen = []
    hub.add_listener(lambda kind, data: seen.append(data.get("available")))
    hub._connected_at -= CONNECTION_MAX_AGE + 1
    await hub._recycle_if_stale(asyncio.get_running_loop().time())
    await panel.stop()

    assert await hub._raise_link() is True

    assert seen == [False]
    assert not hub.available
    hub._stopping = True
    await hub.session.close()


async def test_connect_notifies_available(hass, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    seen = []
    hub.add_listener(lambda kind, data: seen.append(data.get("available")))

    await hub._connect()

    assert seen == [True]
    hub._stopping = True
    await hub.session.close()


async def test_snapshot_on_ring(hass, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    await hub._connect()
    hub.snapshot_on_ring = True
    taken = []
    monkeypatch.setattr(hub, "async_capture_snapshot", lambda target: _record(taken, target))

    connection = await panel.ring()
    await asyncio.sleep(0.05)
    assert taken == [], "the panel refuses an outbound call during a ring"

    await panel.release(connection, 10)
    await until(lambda: bool(taken))

    assert taken == [ENTRANCE]

    hub._stopping = True
    await hub.session.close()


class _FakeRelay:
    viewers = 0
    serving = None

    def __init__(self) -> None:
        self.abandoned = 0

    def url(self, host: str | None = None, target: str | None = None) -> str:
        return f"rtsp://{host or '127.0.0.1'}:8554/comelit"

    async def abandon_call(self) -> None:
        self.abandoned += 1

    async def prepare(self, target: str | None = None) -> None:
        return

    async def stop(self) -> None:
        return


class _FakeEntry:
    def __init__(self, asked: list) -> None:
        self._asked = asked

    def async_start_reauth(self, hass) -> None:
        self._asked.append("reauth")


async def _record(taken: list, target: str) -> bool:
    taken.append(target)
    return True


async def _raise_auth() -> None:
    raise ViperAuthError("token rejected")


async def _no_discovery(host: str, **kwargs):
    return None


async def test_no_snapshot_on_floor_call(hass, panel, monkeypatch):
    """A floor call names the entrance as its origin too."""
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    await hub._connect()
    hub.snapshot_on_ring = True
    hub.record_on_ring = True
    taken = []
    monkeypatch.setattr(hub, "async_capture_snapshot", lambda target: _record(taken, target))
    monkeypatch.setattr(hub, "async_record_clip", lambda *a, **k: _record(taken, "clip"))
    rung = []
    hub.add_listener(lambda kind, data: rung.append(data) if kind == "event" else None)

    await panel.ring(tag=b"FF")
    await until(lambda: bool(rung))
    await asyncio.sleep(0.05)

    assert taken == []

    hub._stopping = True
    await hub.session.close()


async def test_capture_skipped_when_busy(hub, monkeypatch):
    from fake_panel import CONFIGURATION

    from custom_components.comelit_vip.viper.models import PanelConfig

    hub.config = PanelConfig.from_response(CONFIGURATION)
    hub.relay = _FakeRelay()
    hub.relay.serving = "SB900009"
    ran = []

    async def _ffmpeg(args, *, timeout, capture):
        ran.append(args)
        return 0, b"jpeg", b""

    monkeypatch.setattr(hub, "_run_ffmpeg", _ffmpeg)

    with pytest.raises(ViperError, match="a call towards SB900009 is up"):
        await hub.async_capture_snapshot(ENTRANCE)
    assert await hub.async_record_clip(1, target=ENTRANCE) is None
    assert ran == []

    hub.relay.serving = ENTRANCE

    await hub.async_capture_snapshot(ENTRANCE)
    assert hub.snapshots[ENTRANCE] == b"jpeg"


async def test_capture_refused_in_words(hub, monkeypatch):
    """During a ring the panel answers the call with cause 8; the user reads why, not an RTSP 503."""
    from fake_panel import CONFIGURATION

    from custom_components.comelit_vip.viper.models import PanelConfig

    hub.config = PanelConfig.from_response(CONFIGURATION)
    hub.relay = _FakeRelay()
    ran = []

    async def _refuse(target=None):
        raise ViperRefusedError(8)

    async def _ffmpeg(args, *, timeout, capture):
        ran.append(args)
        return 0, b"jpeg", b""

    hub.relay.prepare = _refuse
    monkeypatch.setattr(hub, "_run_ffmpeg", _ffmpeg)

    with pytest.raises(ViperError, match="busy with a call"):
        await hub.async_capture_snapshot(ENTRANCE)
    assert await hub.async_record_clip(1, target=ENTRANCE) is None
    assert ran == []


def test_default_record_path(hass):
    """Off Docker the media folder is under the config directory, not /media."""
    from custom_components.comelit_vip.hub import default_record_path

    hass.config.media_dirs = {"local": "/somewhere/media"}

    assert default_record_path(hass) == "/somewhere/media/comelit_vip"

    hass.config.media_dirs = {}

    assert default_record_path(hass) == hass.config.path("media", "comelit_vip")


async def test_open_value_error_becomes_viper_error(hub, monkeypatch):
    monkeypatch.setattr(ComelitVipHub, "available", property(lambda self: True))

    async def _open(address, relay):
        raise ValueError("relay must fit in one byte")

    monkeypatch.setattr(hub.session, "open_door", _open)
    with pytest.raises(ViperError, match="one byte"):
        await hub.async_open(ENTRANCE, 300)


async def test_floor_call_ending_does_not_release_a_visitors_capture(hass, panel, monkeypatch):
    """A landing-bell chime during a visitor's ring ends first; the capture waits for the ring."""
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    hub = ComelitVipHub(hass, "entry", host=panel.host, token="t" * 32, port=panel.port, rtsp_port=0)
    await hub._connect()
    hub.snapshot_on_ring = True
    taken = []
    monkeypatch.setattr(hub, "async_capture_snapshot", lambda target: _record(taken, target))

    visitor = await panel.ring(connection=b"\x77\x0c")
    chime = await panel.ring(tag=b"FF", connection=b"\x0a\xac")
    await panel.release(chime, 0)
    await asyncio.sleep(0.05)
    assert taken == []

    await panel.release(visitor, 10)
    await until(lambda: bool(taken))
    assert taken == [ENTRANCE]

    hub._stopping = True
    await hub.session.close()
