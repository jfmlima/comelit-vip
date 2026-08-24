"""RTP parsing."""

import struct

import pytest

from custom_components.comelit_vip.viper.rtp import parse_rtp_packet


def _rtp(payload: bytes, *, sequence: int = 1, marker: bool = False, pt: int = 99) -> bytes:
    return struct.pack(">BBHII", 0x80, (0x80 if marker else 0) | pt, sequence, 3000, 0xDEADBEEF) + payload


def test_parse_basic():
    packet = parse_rtp_packet(_rtp(b"\x41\x01", sequence=7, marker=True))

    assert packet.sequence == 7
    assert packet.marker is True
    assert packet.payload_type == 99
    assert packet.payload == b"\x41\x01"


def test_rejects_short_packets():
    with pytest.raises(ValueError):
        parse_rtp_packet(b"\x80\x63")


def test_rejects_bad_version():
    raw = bytearray(_rtp(b"\x41"))
    raw[0] = 0x40

    with pytest.raises(ValueError):
        parse_rtp_packet(bytes(raw))


def test_skips_csrc_entries():
    header = struct.pack(">BBHII", 0x82, 99, 5, 3000, 0xDEADBEEF) + b"\x00" * 8

    assert parse_rtp_packet(header + b"payload").payload == b"payload"


def test_strips_padding():
    raw = struct.pack(">BBHII", 0xA0, 99, 5, 3000, 0xDEADBEEF) + b"data" + b"\x00\x00\x03"

    assert parse_rtp_packet(raw).payload == b"data"
