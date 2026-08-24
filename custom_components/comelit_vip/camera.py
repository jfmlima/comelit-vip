"""Entrance camera, served from the integration's RTSP relay."""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ComelitVipEntry
from .entity import ComelitVipEntity
from .hub import ComelitVipHub

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComelitVipEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one camera per entrance, if there is a relay to serve them."""
    hub = entry.runtime_data
    if hub.config is None or not hub.config.entrances:
        _LOGGER.debug("no entrance panel in the configuration, skipping the camera")
        return
    if hub.relay is None:
        _LOGGER.warning("the RTSP relay is not listening, so there is no camera")
        return
    only = len(hub.config.entrances) == 1
    async_add_entities([ComelitVipCamera(hub, address, name, only=only) for name, address in hub.config.entrances])


class ComelitVipCamera(ComelitVipEntity, Camera):
    """One entrance panel, served through the RTSP relay."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, hub: ComelitVipHub, address: str, name: str, *, only: bool) -> None:
        """Set up the camera for one entrance, keyed by its address."""
        ComelitVipEntity.__init__(self, hub, f"camera_{address}")
        Camera.__init__(self)
        self.address = address
        if only:
            self._attr_translation_key = "entrance"
        else:
            self._attr_name = name or address

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        """Return the URL other consumers such as Frigate can pull."""
        taken = self.hub.snapshots_at.get(self.address)
        return {
            "entrance": self.address,
            "stream_url": self.hub.stream_url(target=self.address),
            "viewers": self.hub.relay.viewers if self.hub.relay else 0,
            "snapshot_at": taken.isoformat() if taken else None,
        }

    async def stream_source(self) -> str | None:
        """Return the relay URL, as reachable from inside Home Assistant."""
        return self.hub.stream_url("127.0.0.1", self.address)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return the last still; fetching one would start a call on the panel."""
        return self.hub.snapshots.get(self.address)

    def _handle_hub_event(self, kind: str, data: dict) -> None:
        super()._handle_hub_event(kind, data)
        if kind == "snapshot" and data.get("entrance") == self.address:
            self.async_write_ha_state()
