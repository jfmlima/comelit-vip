"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .hub import ComelitVipHub


class ComelitVipEntity(Entity):
    """An entity backed by one intercom hub."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: ComelitVipHub, key: str) -> None:
        """Initialize the entity."""
        self.hub = hub
        self._attr_unique_id = f"{hub.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(**hub.device_info)

    async def async_added_to_hass(self) -> None:
        """Subscribe to hub notifications."""
        self.async_on_remove(self.hub.add_listener(self._handle_hub_event))

    @property
    def available(self) -> bool:
        """Return whether the intercom connection is up."""
        return self.hub.available

    def _handle_hub_event(self, kind: str, data: dict) -> None:
        """Handle a hub notification."""
        if kind == "connection":
            self.async_write_ha_state()
