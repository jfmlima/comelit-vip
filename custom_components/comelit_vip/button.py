"""Buttons for doors, actuators and the camera snapshot."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ComelitVipEntry
from .entity import ComelitVipEntity
from .hub import ComelitVipHub
from .viper.models import Actuator, Door
from .viper.session import ViperError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComelitVipEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a button per door and actuator, plus the snapshot button."""
    hub = entry.runtime_data
    config = hub.config
    if config is None:
        return
    entities: list[ButtonEntity] = [ComelitVipOpenButton(hub, door) for door in config.doors]
    entities += [ComelitVipOpenButton(hub, actuator) for actuator in config.actuators]
    if hub.relay is not None:
        only = len(config.entrances) == 1
        entities += [ComelitVipSnapshotButton(hub, address, name, only=only) for name, address in config.entrances]
    async_add_entities(entities)


class ComelitVipOpenButton(ComelitVipEntity, ButtonEntity):
    """Fires a door release or an actuator."""

    _attr_icon = "mdi:door-open"

    def __init__(self, hub: ComelitVipHub, target: Door | Actuator) -> None:
        super().__init__(hub, target.key)
        self._target = target
        self._attr_name = target.name
        if isinstance(target, Actuator):
            self._attr_icon = "mdi:electric-switch"

    async def async_press(self) -> None:
        """Press the button."""
        try:
            await self.hub.async_open(self._target.address, self._target.output_index)
        except ViperError as err:
            raise HomeAssistantError(f"could not open {self._target.name}: {err}") from err


class ComelitVipSnapshotButton(ComelitVipEntity, ButtonEntity):
    """Takes a still from one entrance camera."""

    def __init__(self, hub: ComelitVipHub, address: str, name: str, *, only: bool) -> None:
        """Set up the button for one entrance, keyed by its address."""
        super().__init__(hub, f"snapshot_{address}")
        self.address = address
        if only:
            self._attr_translation_key = "snapshot"
        else:
            self._attr_name = f"Update snapshot {name or address}"

    async def async_press(self) -> None:
        """Press the button."""
        if not await self.hub.async_capture_snapshot(self.address):
            raise HomeAssistantError(f"could not take a picture from the entrance panel at {self.address}")
