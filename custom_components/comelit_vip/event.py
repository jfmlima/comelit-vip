"""Doorbell event entity."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ComelitVipEntry
from .const import EVENT_TYPES
from .entity import ComelitVipEntity
from .hub import ComelitVipHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComelitVipEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the doorbell event entity."""
    async_add_entities([ComelitVipDoorbell(entry.runtime_data)])


class ComelitVipDoorbell(ComelitVipEntity, EventEntity):
    """Fires on an incoming call and when that call ends."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "doorbell"

    def __init__(self, hub: ComelitVipHub) -> None:
        """Initialize the entity."""
        super().__init__(hub, "doorbell")

    def _handle_hub_event(self, kind: str, data: dict) -> None:
        if kind == "connection":
            self.async_write_ha_state()
            return
        if kind != "event":
            return
        event_type = data.get("type")
        if event_type not in EVENT_TYPES:
            return
        self._trigger_event(event_type, {k: v for k, v in data.items() if k != "type"})
        self.async_write_ha_state()
