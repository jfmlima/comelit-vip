"""CTP, the transport carried inside the CTPP channel.

Packet layout:

    flags:u8  version:u8(=0x18)  connection:2B  seq:u8  ack:u8  body_len:u16be
    body  zero-pad-to-4  ff ff ff ff  source:logaddr(10)  destination:logaddr(10)

Flags: 0xC0 opens a connection, 0x40 data, 0x00 ack, 0x20 closes. The panel
sends 0x60 on a connection it owns. The connection id is a 15-bit number
picked by whoever opens it; the peer uses the same id with bit 15 set.

The first two body bytes are the opcode.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CTP_VERSION = 0x18

FLAG_ACK = 0x00
FLAG_FIN = 0x20
FLAG_DATA = 0x40
FLAG_SYN = 0xC0

OP_INVITE = 0x0001  # call setup; inbound means somebody is calling
OP_CAPABILITIES = 0x0003
OP_SETUP_ACK = 0x000C
OP_RELEASE = 0x000E  # ends a connection, carries a cause byte
OP_MEDIA_REQUEST = 0x0011
OP_OPEN_DOOR = 0x002D
OP_REGISTER = 0x0040
OP_REGISTER_RESPONSE = 0x0041  # the panel repeats this as a renewal

LOGADDR_LEN = 10
# Separates the padded body from the two addresses that close every packet.
TRAILER_MARKER = b"\xff\xff\xff\xff"


@dataclass(slots=True)
class CtpPacket:
    """Decoded CTP packet."""

    flags: int
    connection: bytes
    sequence: int
    acknowledgement: int
    body: bytes
    source: str
    destination: str

    @property
    def opcode(self) -> int | None:
        """Opcode from the first two body bytes, if any."""
        return struct.unpack_from(">H", self.body)[0] if len(self.body) >= 2 else None

    @property
    def is_syn(self) -> bool:
        """Return whether this packet opens a connection."""
        return bool(self.flags & 0x80)

    @property
    def is_fin(self) -> bool:
        """Return whether this packet closes a connection."""
        return bool(self.flags & 0x20)

    @property
    def release_cause(self) -> int | None:
        """Cause byte of a RELEASE packet."""
        if self.opcode != OP_RELEASE or len(self.body) < 3:
            return None
        return self.body[2]


def decode_logaddr(raw: bytes) -> str:
    """Decode a 10-byte ViP logical address (latin-1, so any byte round-trips)."""
    if len(raw) != LOGADDR_LEN:
        raise ValueError(f"logaddr must be {LOGADDR_LEN} bytes, got {len(raw)}")
    return raw.rstrip(b"\x00").decode("latin-1")


def encode_logaddr(address: str) -> bytes:
    """Encode a ViP logical address as 10 bytes."""
    try:
        raw = address.encode("latin-1")
    except UnicodeEncodeError as err:
        raise ValueError(f"not a ViP logical address: {address!r}") from err
    if len(raw) > LOGADDR_LEN or b"\x00" in raw:
        raise ValueError(f"ViP logical address must be at most {LOGADDR_LEN} bytes and hold no NUL")
    return raw.ljust(LOGADDR_LEN, b"\x00")


def parse_ctp_packet(payload: bytes) -> CtpPacket:
    """Parse one CTPP channel payload."""
    if len(payload) < 32:
        raise ValueError(f"CTP packet too short: {len(payload)}")
    if payload[1] != CTP_VERSION:
        raise ValueError(f"unsupported CTP version byte: {payload[1]:#x}")
    body_len = struct.unpack_from(">H", payload, 6)[0]
    body_end = 8 + body_len
    trailer_start = body_end + ((-body_len) % 4)
    if len(payload) != trailer_start + 24:
        raise ValueError(f"CTP length mismatch: body={body_len}, packet={len(payload)}")
    trailer = payload[trailer_start:]
    if trailer[:4] != TRAILER_MARKER:
        raise ValueError(f"bad CTP trailer marker: {trailer[:4].hex()}")
    return CtpPacket(
        flags=payload[0],
        connection=payload[2:4],
        sequence=payload[4],
        acknowledgement=payload[5],
        body=payload[8:body_end],
        source=decode_logaddr(trailer[4:14]),
        destination=decode_logaddr(trailer[14:24]),
    )


def build_ctp_packet(
    *,
    flags: int,
    connection: bytes,
    sequence: int,
    acknowledgement: int,
    body: bytes,
    source: str,
    destination: str,
) -> bytes:
    """Build one CTPP channel payload; the inverse of ``parse_ctp_packet``."""
    for field, value in (("flags", flags), ("sequence", sequence), ("acknowledgement", acknowledgement)):
        if not 0 <= value <= 0xFF:
            raise ValueError(f"CTP {field} does not fit in one byte: {value}")
    if len(connection) != 2:
        raise ValueError(f"CTP connection id is two bytes, got {len(connection)}")
    header = struct.pack(">BB2sBBH", flags, CTP_VERSION, connection, sequence, acknowledgement, len(body))
    padding = b"\x00" * (-len(body) % 4)
    trailer = TRAILER_MARKER + encode_logaddr(source) + encode_logaddr(destination)
    return header + body + padding + trailer


# Last two bytes of an INVITE. A client sends II when it activates a camera.
TAG_FLOOR_CALL = b"FF"
TAG_ENTRANCE = b"PP"


def parse_invite(body: bytes) -> tuple[str, str, bytes, bytes]:
    """Return ``(origin, destination, call_id, tag)`` from an INVITE body.

    Observed layout, forty bytes:

        opcode(2) origin(10) destination(10) flags(2) call-id(4) origin(10) tag(2)

    On a two-wire system bridged by the monitor a floor call and an entrance
    call carry identical addresses; only the tag differs.
    """
    if len(body) < 28:
        raise ValueError(f"INVITE body too short: {len(body)}")
    return (
        decode_logaddr(body[2:12]),
        decode_logaddr(body[12:22]),
        body[24:28],
        body[38:40] if len(body) >= 40 else b"",
    )


def peer_connection_id(connection: bytes) -> bytes:
    """Return the id the peer uses for this connection."""
    value = struct.unpack(">H", connection)[0]
    return struct.pack(">H", value ^ 0x8000)
