"""The doorbell end to end: an INVITE on the socket to an event entity."""

from __future__ import annotations

import logging

import pytest
from fake_panel import ENTRANCE, FakePanel, until
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comelit_vip.const import BUS_EVENT, CONF_RTSP_PORT, CONF_SNAPSHOT_ON_RING, CONF_TOKEN, DOMAIN
from custom_components.comelit_vip.diagnostics import async_get_config_entry_diagnostics

UNIQUE_ID = "aa:bb:cc:dd:ee:ff"


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
async def entry(hass, panel, monkeypatch):
    """The integration, set up against that panel."""
    monkeypatch.setattr("custom_components.comelit_vip.config_flow.async_discover", _no_discovery)
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    monkeypatch.setattr("custom_components.comelit_vip.hub.RECONNECT_BACKOFF", (0,))
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=UNIQUE_ID,
        data={CONF_HOST: panel.host, CONF_PORT: panel.port, CONF_TOKEN: "t" * 32},
        options={CONF_RTSP_PORT: 0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        yield entry
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_ring_fires_doorbell(hass, entry, panel):
    doorbell = _doorbell(hass)
    events = _listen(hass)

    await panel.ring()
    await _ring_seen(hass, doorbell, "ring")

    state = hass.states.get(doorbell)
    assert state.attributes["event_type"] == "ring"
    assert state.attributes["origin"] == ENTRANCE
    assert state.attributes["kind"] == "PP"
    assert [event["type"] for event in events] == ["ring"]


async def test_floor_call_is_internal(hass, entry, panel):
    """Same addresses; only the two byte tag differs."""
    doorbell = _doorbell(hass)

    await panel.ring(tag=b"FF")
    await _ring_seen(hass, doorbell, "internal_call")

    assert hass.states.get(doorbell).attributes["event_type"] == "internal_call"


async def test_call_ended_event(hass, entry, panel):
    doorbell = _doorbell(hass)

    connection = await panel.ring()
    await _ring_seen(hass, doorbell, "ring")
    await panel.release(connection, cause=0)
    await _ring_seen(hass, doorbell, "call_ended")

    assert hass.states.get(doorbell).attributes["event_type"] == "call_ended"


async def test_ring_after_reconnect(hass, entry, panel):
    doorbell = _doorbell(hass)
    await panel.ring()
    await _ring_seen(hass, doorbell, "ring")

    await panel.drop()
    await until(lambda: panel.connections == 2 and panel.registrations == 2)
    await hass.async_block_till_done()

    events = _listen(hass)
    await panel.ring()
    await until(lambda: bool(events))
    await hass.async_block_till_done()

    assert [event["type"] for event in events] == ["ring"]
    assert hass.states.get(doorbell).state != "unavailable"


async def test_unavailable_when_unregistered(hass, entry, panel):
    hub = entry.runtime_data
    doorbell = _doorbell(hass)
    assert hass.states.get(doorbell).state != "unavailable"

    hub.session.registered_at = None
    hub._notify("connection", {"available": False})
    await hass.async_block_till_done()

    assert hub.session.connected
    assert hass.states.get(doorbell).state == "unavailable"


async def test_door_button(hass, entry, panel):
    await hass.services.async_call("button", "press", {"entity_id": _entity(hass, "door_0_SB900001_1")}, blocking=True)
    await hass.async_block_till_done()

    assert panel.opened == [(ENTRANCE, 1)]


async def test_unknown_event_type_ignored(hass, entry, panel, caplog):
    """`_trigger_event` raises on a type the entity did not declare."""
    hub = entry.runtime_data
    doorbell = _doorbell(hass)
    before = hass.states.get(doorbell).state

    with caplog.at_level(logging.ERROR):
        hub._notify("event", {"type": "something_else"})
        await hass.async_block_till_done()

    assert hass.states.get(doorbell).state == before
    assert "listener failed" not in caplog.text


async def test_rtsp_port_change_reloads(hass, entry, panel):
    first = entry.runtime_data

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_RTSP_PORT: 1})
    await hass.async_block_till_done()

    assert entry.runtime_data is not first


async def test_other_option_change_no_reload(hass, entry, panel):
    first = entry.runtime_data

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_SNAPSHOT_ON_RING: True})
    await hass.async_block_till_done()

    assert entry.runtime_data is first
    assert first.snapshot_on_ring is True


async def test_diagnostics_unloaded_entry(hass, panel):
    unloaded = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="ff:ee:dd:cc:bb:aa",
        data={CONF_HOST: panel.host, CONF_PORT: panel.port, CONF_TOKEN: "t" * 32},
    )
    unloaded.add_to_hass(hass)

    report = await async_get_config_entry_diagnostics(hass, unloaded)

    assert report["loaded"] is False
    assert report["entry"][CONF_TOKEN] == "**REDACTED**"


async def test_diagnostics_loaded_entry(hass, entry, panel):
    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["loaded"] is True
    assert report["registered"] is True


async def test_configuration_url_port(hass, entry, panel):
    assert entry.runtime_data.device_info["configuration_url"] == f"http://{panel.host}:8080/"


async def test_unload_reload(hass, entry, panel):
    doorbell = _doorbell(hass)
    connections = panel.connections

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert panel.connections == connections
    assert hass.states.get(doorbell).state == "unavailable"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert panel.connections == connections + 1
    await panel.ring()
    await _ring_seen(hass, _doorbell(hass), "ring")


def _doorbell(hass) -> str:
    return _entity(hass, "doorbell")


def _entity(hass, key: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, f"{UNIQUE_ID}_{key}") or registry.async_get_entity_id(
        "button", DOMAIN, f"{UNIQUE_ID}_{key}"
    )
    assert entity_id, f"no entity for {key}"
    return entity_id


def _listen(hass) -> list[dict]:
    events: list[dict] = []
    hass.bus.async_listen(BUS_EVENT, lambda event: events.append(event.data))
    return events


async def _no_discovery(host: str, **kwargs):
    return None


async def _ring_seen(hass, doorbell: str, kind: str) -> None:
    await until(lambda: hass.states.get(doorbell).attributes.get("event_type") == kind)
    await hass.async_block_till_done()


async def test_camera_per_entrance(hass, panel, monkeypatch):
    import copy

    import fake_panel

    two = copy.deepcopy(fake_panel.CONFIGURATION)
    two["vip"]["user-parameters"]["entrance-address-book"].append({"id": 1, "name": "Garage", "apt-address": "SB900002"})
    monkeypatch.setattr(fake_panel, "CONFIGURATION", two)
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=UNIQUE_ID,
        data={CONF_HOST: panel.host, CONF_PORT: panel.port, CONF_TOKEN: "t" * 32},
        options={CONF_RTSP_PORT: 0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    try:
        first = registry.async_get_entity_id("camera", DOMAIN, f"{UNIQUE_ID}_camera_{ENTRANCE}")
        second = registry.async_get_entity_id("camera", DOMAIN, f"{UNIQUE_ID}_camera_SB900002")

        assert first is not None and second is not None
        assert hass.states.get(second).attributes["stream_url"].endswith("/comelit/SB900002")
        assert hass.states.get(first).attributes["stream_url"].endswith("/comelit")
        assert registry.async_get_entity_id("button", DOMAIN, f"{UNIQUE_ID}_snapshot_SB900002") is not None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_legacy_first_entrance_rekeyed(hass, panel, monkeypatch):
    """Entities keyed `<uid>_camera` / `<uid>_snapshot` move to the entrance address and keep their entity ids."""
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=UNIQUE_ID,
        data={CONF_HOST: panel.host, CONF_PORT: panel.port, CONF_TOKEN: "t" * 32},
        options={CONF_RTSP_PORT: 0},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    camera = registry.async_get_or_create("camera", DOMAIN, f"{UNIQUE_ID}_camera", config_entry=entry).entity_id
    button = registry.async_get_or_create("button", DOMAIN, f"{UNIQUE_ID}_snapshot", config_entry=entry).entity_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        assert registry.async_get_entity_id("camera", DOMAIN, f"{UNIQUE_ID}_camera_{ENTRANCE}") == camera
        assert registry.async_get_entity_id("button", DOMAIN, f"{UNIQUE_ID}_snapshot_{ENTRANCE}") == button
        assert registry.async_get_entity_id("camera", DOMAIN, f"{UNIQUE_ID}_camera") is None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_host_keyed_entry_uses_entry_id(hass, panel, monkeypatch):
    """An entry whose unique id is a host name keys its entities by entry id, as version 1 did."""
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=panel.host,
        data={CONF_HOST: panel.host, CONF_PORT: panel.port, CONF_TOKEN: "t" * 32},
        options={CONF_RTSP_PORT: 0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    try:
        assert registry.async_get_entity_id("event", DOMAIN, f"{entry.entry_id}_doorbell") is not None
        assert registry.async_get_entity_id("event", DOMAIN, f"{panel.host}_doorbell") is None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_rekey_leaves_a_clash_alone(hass, panel, monkeypatch):
    monkeypatch.setattr("custom_components.comelit_vip.hub.async_discover", _no_discovery)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=UNIQUE_ID,
        data={CONF_HOST: panel.host, CONF_PORT: panel.port, CONF_TOKEN: "t" * 32},
        options={CONF_RTSP_PORT: 0},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create("camera", DOMAIN, f"{UNIQUE_ID}_camera", config_entry=entry).entity_id
    current = registry.async_get_or_create("camera", DOMAIN, f"{UNIQUE_ID}_camera_{ENTRANCE}", config_entry=entry).entity_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        assert registry.async_get_entity_id("camera", DOMAIN, f"{UNIQUE_ID}_camera") == legacy
        assert registry.async_get_entity_id("camera", DOMAIN, f"{UNIQUE_ID}_camera_{ENTRANCE}") == current
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
