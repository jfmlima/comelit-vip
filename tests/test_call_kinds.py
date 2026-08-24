"""Telling a visitor apart from a call raised inside."""

from datetime import UTC, datetime

from custom_components.comelit_vip.const import EVENT_INTERNAL_CALL, EVENT_RING
from custom_components.comelit_vip.viper.models import PanelConfig, RingEvent

CONFIG = PanelConfig.from_response(
    {
        "vip": {
            "apt-address": "SB000042",
            "apt-subaddress": 2,
            "user-parameters": {
                "entrance-address-book": [{"id": 0, "name": "Entrance", "apt-address": "SB900001"}],
            },
        }
    }
)


def _event(origin: str = "SB900001", tag: bytes = b"PP") -> RingEvent:
    return RingEvent(
        caller="SB000042",
        callee="SB0000422",
        connection=b"\x00\x01",
        call_id=b"",
        received_at=datetime.now(UTC),
        origin=origin,
        tag=tag,
    )


def test_entrance_recognised():
    assert CONFIG.is_entrance("SB900001")
    assert CONFIG.entrance_name("SB900001") == "Entrance"


def test_apartment_not_entrance():
    # Every call arrives carried from the apartment address.
    assert not CONFIG.is_entrance("SB000042")
    assert CONFIG.entrance_name("SB000042") is None


def _hub(hass):
    from custom_components.comelit_vip.hub import ComelitVipHub

    hub = ComelitVipHub(hass, "test", host="h", token="t", port=64100, rtsp_port=8554)
    hub.config = CONFIG
    return hub


def test_floor_call_is_internal(hass):
    """Both name the entrance panel as origin; only the tag differs. Captured from a 6741W."""
    hub = _hub(hass)
    seen: list[dict] = []
    hub.add_listener(lambda kind, data: seen.append(data) if kind == "event" else None)

    hub._on_ring(_event(tag=b"PP"))
    hub._on_ring(_event(tag=b"FF"))

    assert [item["type"] for item in seen] == [EVENT_RING, EVENT_INTERNAL_CALL]
    assert seen[0]["entrance"] == "Entrance"
    assert seen[0]["kind"] == "PP"
    assert "entrance" not in seen[1]
    assert seen[1]["kind"] == "FF"
    assert seen[0]["origin"] == seen[1]["origin"] == "SB900001"


def test_unknown_tag_uses_address_book(hass):
    hub = _hub(hass)
    seen: list[dict] = []
    hub.add_listener(lambda kind, data: seen.append(data) if kind == "event" else None)

    hub._on_ring(_event(origin="SB900001", tag=b"ZZ"))
    hub._on_ring(_event(origin="SB000099", tag=b""))

    assert [item["type"] for item in seen] == [EVENT_RING, EVENT_INTERNAL_CALL]


def test_last_ring_visitor_only(hass):
    hub = _hub(hass)

    hub._on_ring(_event(tag=b"FF"))
    assert hub.last_ring is None

    hub._on_ring(_event(tag=b"PP"))
    assert hub.last_ring is not None
