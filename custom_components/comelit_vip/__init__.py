"""The Comelit ViP intercom integration."""

from __future__ import annotations

import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EXPOSE_STREAM,
    CONF_RECORD_ON_RING,
    CONF_RECORD_PATH,
    CONF_RECORD_SECONDS,
    CONF_RTSP_HOST,
    CONF_RTSP_PORT,
    CONF_SNAPSHOT_ON_RING,
    CONF_TOKEN,
    CONF_WEB_PORT,
    DEFAULT_PORT,
    DEFAULT_RECORD_SECONDS,
    DEFAULT_RTSP_PORT,
    DEFAULT_WEB_PORT,
    DOMAIN,
)
from .hub import ComelitVipHub, default_record_path
from .viper.session import ViperAuthError, ViperError

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.CAMERA, Platform.EVENT]

_MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", re.IGNORECASE)

type ComelitVipEntry = ConfigEntry[ComelitVipHub]


async def async_setup_entry(hass: HomeAssistant, entry: ComelitVipEntry) -> bool:
    """Set up one intercom."""
    hub = ComelitVipHub(
        hass,
        entry.entry_id,
        unique_id=_panel_id(entry),
        host=entry.data[CONF_HOST],
        token=entry.data[CONF_TOKEN],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        rtsp_port=entry.options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
        rtsp_host=entry.options.get(CONF_RTSP_HOST) or None,
        expose_stream=entry.options.get(CONF_EXPOSE_STREAM, False),
        web_port=entry.data.get(CONF_WEB_PORT, DEFAULT_WEB_PORT),
    )
    _apply_options(hub, entry)
    try:
        await hub.async_setup()
    except ViperAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (ViperError, OSError) as err:
        await hub.async_shutdown()
        raise ConfigEntryNotReady(f"cannot reach the intercom at {entry.data[CONF_HOST]}: {err}") from err

    entry.runtime_data = hub
    _rekey_first_entrance(hass, hub)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


def _panel_id(entry: ConfigEntry) -> str | None:
    """Return the panel's MAC if that is what the entry is keyed by; None otherwise."""
    return entry.unique_id if entry.unique_id and _MAC_RE.fullmatch(entry.unique_id) else None


@callback
def _rekey_first_entrance(hass: HomeAssistant, hub: ComelitVipHub) -> None:
    """Move the first entrance's entities from position-keyed to address-keyed unique ids."""
    if hub.config is None or hub.config.entrance is None:
        return
    registry = er.async_get(hass)
    for domain, key in (("camera", "camera"), ("button", "snapshot")):
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{hub.unique_id}_{key}")
        new_unique_id = f"{hub.unique_id}_{key}_{hub.config.entrance}"
        if entity_id is not None and registry.async_get_entity_id(domain, DOMAIN, new_unique_id) is None:
            registry.async_update_entity(entity_id, new_unique_id=new_unique_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ComelitVipEntry) -> bool:
    """Rekey entities and the device from the entry id to the panel MAC (version 1 to 2)."""
    if entry.version >= 2:
        return True
    panel = _panel_id(entry)
    if panel is not None and panel != entry.entry_id:
        old = f"{entry.entry_id}_"

        @callback
        def _rekey(item: er.RegistryEntry) -> dict[str, str] | None:
            if not item.unique_id.startswith(old):
                return None
            return {"new_unique_id": f"{panel}_{item.unique_id[len(old) :]}"}

        await er.async_migrate_entries(hass, entry.entry_id, _rekey)
        devices = dr.async_get(hass)
        device = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        if device is not None:
            devices.async_update_device(device.id, new_identifiers={(DOMAIN, panel)})
    hass.config_entries.async_update_entry(entry, version=2)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ComelitVipEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_update_options(hass: HomeAssistant, entry: ComelitVipEntry) -> None:
    """Apply changed options, reloading if the RTSP listener has to move."""
    hub = entry.runtime_data
    moved = entry.options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT) != hub.rtsp_port
    exposed = entry.options.get(CONF_EXPOSE_STREAM, False) != hub.expose_stream
    if moved or exposed:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    _apply_options(hub, entry)


def _apply_options(hub: ComelitVipHub, entry: ComelitVipEntry) -> None:
    """Copy the options onto the hub."""
    hub.record_on_ring = entry.options.get(CONF_RECORD_ON_RING, False)
    hub.snapshot_on_ring = entry.options.get(CONF_SNAPSHOT_ON_RING, False)
    hub.record_seconds = entry.options.get(CONF_RECORD_SECONDS, DEFAULT_RECORD_SECONDS)
    hub.record_path = entry.options.get(CONF_RECORD_PATH) or default_record_path(hub.hass)
    hub.rtsp_host = entry.options.get(CONF_RTSP_HOST) or None
