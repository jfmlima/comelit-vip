"""Data models shared by the client and the integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .ctp import encode_logaddr
from .errors import ViperError


@dataclass(slots=True, frozen=True)
class Door:
    """An entry in the opendoor address book."""

    id: int
    name: str
    address: str
    output_index: int = 1
    secure_mode: bool = False

    @property
    def key(self) -> str:
        """Return the unique id suffix for this door."""
        return f"door_{self.id}_{self.address}_{self.output_index}"


@dataclass(slots=True, frozen=True)
class Actuator:
    """An entry in the actuator address book."""

    id: int
    name: str
    address: str
    module_index: int = 0
    output_index: int = 1

    @property
    def key(self) -> str:
        """Return the unique id suffix for this actuator."""
        return f"actuator_{self.id}_{self.address}_{self.output_index}"


@dataclass(slots=True)
class PanelConfig:
    """The ``get-configuration`` response."""

    apt_address: str
    apt_subaddress: int
    description: str = ""
    entrances: list[tuple[str, str]] = field(default_factory=list)  # (name, address)
    doors: list[Door] = field(default_factory=list)
    actuators: list[Actuator] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def source(self) -> str:
        """Return our full ViP address: apartment address plus subaddress."""
        return f"{self.apt_address}{self.apt_subaddress}"

    @property
    def entrance(self) -> str | None:
        """Return the first entrance panel address, if there is one."""
        return self.entrances[0][1] if self.entrances else None

    def is_entrance(self, address: str) -> bool:
        """Return whether ``address`` is one of this apartment's entrances."""
        return any(address == entrance for _name, entrance in self.entrances)

    def entrance_name(self, address: str) -> str | None:
        """Return the installer's name for the entrance at ``address``."""
        return next((name for name, entrance in self.entrances if entrance == address), None)

    @classmethod
    def from_response(cls, resp: dict) -> PanelConfig:
        """Build from a ``get-configuration`` response, tolerating a malformed document."""
        vip = _table(resp)
        vip = _table(vip.get("vip"))
        params = _table(vip.get("user-parameters"))
        apt_address = _address(vip.get("apt-address"))
        if not apt_address:
            raise ViperError("the panel's configuration names no apartment address")
        entrances = [
            (_text(e.get("name")), _address(e.get("apt-address")))
            for e in map(_table, _rows(params.get("entrance-address-book")))
            if _address(e.get("apt-address"))
        ]
        doors = [
            Door(
                id=_number(d.get("id"), i),
                name=_text(d.get("name")) or f"Door {i + 1}",
                address=_address(d.get("apt-address")),
                output_index=_byte(d.get("output-index"), 1),
                secure_mode=bool(d.get("secure-mode", False)),
            )
            for i, d in enumerate(map(_table, _rows(params.get("opendoor-address-book"))))
            if _address(d.get("apt-address"))
        ]
        actuators = [
            Actuator(
                id=_number(a.get("id"), i),
                name=_text(a.get("name")) or f"Actuator {i + 1}",
                address=_address(a.get("apt-address")),
                module_index=_byte(a.get("module-index"), 0),
                output_index=_byte(a.get("output-index"), 1),
            )
            for i, a in enumerate(map(_table, _rows(params.get("actuator-address-book"))))
            if _address(a.get("apt-address"))
        ]
        return cls(
            apt_address=apt_address,
            apt_subaddress=_number(vip.get("apt-subaddress"), 1),
            description=_text(_table(_table(resp).get("viper-client")).get("description")),
            entrances=entrances,
            doors=doors,
            actuators=actuators,
            raw=_table(resp),
        )


@dataclass(slots=True, frozen=True)
class RingEvent:
    """An inbound call seen on the CTPP channel; ``tag`` tells an entrance call from a floor call."""

    caller: str
    callee: str
    connection: bytes
    call_id: bytes
    received_at: datetime
    body: bytes = b""
    origin: str = ""
    tag: bytes = b""


def _table(value: object) -> dict:
    """Return ``value`` if it is a dict, else an empty one."""
    return value if isinstance(value, dict) else {}


def _rows(value: object) -> list:
    """Return ``value`` if it is a list, else an empty one."""
    return value if isinstance(value, list) else []


def _text(value: object) -> str:
    """Return ``value`` if it is a str, else an empty one."""
    return value if isinstance(value, str) else ""


def _address(value: object) -> str:
    """Return a ViP address the wire can carry, or an empty string."""
    text = _text(value)
    try:
        encode_logaddr(text)
    except ValueError:
        return ""
    return text


def _byte(value: object, default: int) -> int:
    """Return a whole number that fits one byte, or the default."""
    number = _number(value, default)
    return number if 0 <= number <= 0xFF else default


def _number(value: object, default: int) -> int:
    """Return ``value`` as an int, or ``default``."""
    try:
        return int(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return default
