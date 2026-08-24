"""Diagnostics for a config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ComelitVipEntry

REDACT = {"token", "user-token", "serial-code", "duuid", "email"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ComelitVipEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry, loaded or not."""
    hub = getattr(entry, "runtime_data", None)
    if hub is None:
        return {
            "loaded": False,
            "state": str(entry.state),
            "version": entry.version,
            "entry": async_redact_data(dict(entry.data), REDACT),
            "options": dict(entry.options),
        }
    config = hub.config
    return {
        "loaded": True,
        "available": hub.available,
        "connected": hub.session.connected,
        "registered": hub.session.registered,
        "registration_age": hub.session.registration_age,
        "registration_refreshes": hub.session.refreshes,
        # Always 0 on a 6741W; other panels may renew on their own.
        "panel_renewals": hub.session.renewals,
        "last_ring": hub.last_ring.isoformat() if hub.last_ring else None,
        "stream_url": hub.stream_url(),
        "viewers": hub.relay.viewers if hub.relay else 0,
        "serving": hub.relay.serving if hub.relay else None,
        "auth_failures": hub._auth_failures,
        "device": {
            "model": hub.device.model if hub.device else None,
            "model_id": hub.device.model_id if hub.device else None,
            "firmware": hub.device.firmware if hub.device else None,
        },
        "panel": {
            "source": config.source if config else None,
            "entrances": config.entrances if config else [],
            "doors": [d.name for d in config.doors] if config else [],
            "actuators": [a.name for a in config.actuators] if config else [],
        },
        "raw_configuration": async_redact_data(config.raw if config else {}, REDACT),
    }
