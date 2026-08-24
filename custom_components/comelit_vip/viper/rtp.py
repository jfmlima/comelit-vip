"""RTP parsing."""

from __future__ import annotations

import struct
from dataclasses import dataclass

NAL_SPS = 7
NAL_PPS = 8


@dataclass(slots=True)
class RtpPacket:
    """One RTP packet."""

    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes


def parse_rtp_packet(raw: bytes) -> RtpPacket:
    """Parse an RTP v2 packet, including CSRC and extension headers."""
    if len(raw) < 12:
        raise ValueError(f"RTP packet too short: {len(raw)}")
    first, second = raw[0], raw[1]
    if first >> 6 != 2:
        raise ValueError(f"unsupported RTP version: {first >> 6}")
    offset = 12 + (first & 0x0F) * 4
    if offset > len(raw):
        raise ValueError("truncated RTP CSRC list")
    if first & 0x10:
        if offset + 4 > len(raw):
            raise ValueError("truncated RTP extension header")
        extension_words = struct.unpack_from(">H", raw, offset + 2)[0]
        offset += 4 + extension_words * 4
        if offset > len(raw):
            raise ValueError("truncated RTP extension data")
    end = len(raw)
    if first & 0x20:
        padding = raw[-1]
        if padding == 0 or padding > end - offset:
            raise ValueError("invalid RTP padding")
        end -= padding
    return RtpPacket(
        marker=bool(second & 0x80),
        payload_type=second & 0x7F,
        sequence=struct.unpack_from(">H", raw, 2)[0],
        timestamp=struct.unpack_from(">I", raw, 4)[0],
        ssrc=struct.unpack_from(">I", raw, 8)[0],
        payload=raw[offset:end],
    )
