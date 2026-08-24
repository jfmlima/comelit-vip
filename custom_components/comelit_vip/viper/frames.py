r"""Wire framing and channel management.

Every message on the TCP stream is one frame:

    b"\\x00\\x06" + u16le(len(payload)) + u16le(handle) + b"\\x00\\x00" + payload

Handle 0 is the management channel. Channels are opened by a four character
name with a client-chosen handle; the panel acknowledges on handle 0.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER_MAGIC = b"\x00\x06"
HEADER_LEN = 8
MGMT_HANDLE = 0x0000

OPEN_MAGIC = b"\xcd\xab\x01\x00"
OPENACK_MAGIC = b"\xcd\xab\x02\x00"
CLOSE_MAGIC = b"\xef\x01\x03\x00"
CLOSEACK_MAGIC = b"\xef\x01\x04\x00"

CHANNEL_TYPE = 7  # the app uses this for every channel


@dataclass(slots=True)
class Frame:
    """One framed message."""

    handle: int
    payload: bytes


def encode_frame(handle: int, payload: bytes) -> bytes:
    """Wrap a payload for a channel handle."""
    if len(payload) > 0xFFFF:
        raise ValueError("frame payload too large")
    return HEADER_MAGIC + struct.pack("<HH", len(payload), handle) + b"\x00\x00" + payload


def parse_header(header: bytes) -> tuple[int, int]:
    """Return ``(payload_length, handle)`` from a frame header."""
    if len(header) != HEADER_LEN or header[:2] != HEADER_MAGIC:
        raise ValueError(f"bad frame header: {header.hex()}")
    length, handle = struct.unpack_from("<HH", header, 2)
    return length, handle


def encode_open_channel(name: str, handle: int, registration: bytes = b"") -> bytes:
    """Build the payload that opens channel ``name`` as ``handle``.

    ``registration`` is the local 10-byte logical address, appended when
    opening CTPP.
    """
    if len(name) != 4:
        raise ValueError("channel names are four ASCII characters")
    body = OPEN_MAGIC + struct.pack("<I", CHANNEL_TYPE) + name.encode("ascii") + struct.pack("<H", handle) + b"\x00"
    if registration:
        body += b"\x00" + struct.pack("<I", len(registration)) + registration
    return body


def encode_close_channel(handle: int) -> bytes:
    """Build the management payload that closes ``handle``."""
    return CLOSE_MAGIC + struct.pack("<I", 2) + struct.pack("<H", handle)


def encode_open_ack(handle: int) -> bytes:
    """Acknowledge a channel the panel opened."""
    return OPENACK_MAGIC + struct.pack("<I", 4) + struct.pack("<H", handle) + b"\x00\x00"


def parse_mgmt(payload: bytes) -> tuple[bytes, int, str | None]:
    """Decode a management payload into ``(magic, handle, channel_name)``; the name is only in an open request."""
    if len(payload) < 10:
        raise ValueError("management payload too short")
    magic = payload[:4]
    if magic == OPEN_MAGIC:
        if len(payload) < 15:
            raise ValueError("open request too short")
        name = payload[8:12].decode("ascii", "replace")
        handle = struct.unpack_from("<H", payload, 12)[0]
        return magic, handle, name
    handle = struct.unpack_from("<H", payload, 8)[0]
    return magic, handle, None
