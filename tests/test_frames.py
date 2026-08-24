"""Frame and channel-management encoding."""

import pytest

from custom_components.comelit_vip.viper.frames import (
    CLOSE_MAGIC,
    OPEN_MAGIC,
    OPENACK_MAGIC,
    encode_close_channel,
    encode_frame,
    encode_open_channel,
    parse_header,
    parse_mgmt,
)


def test_frame_header_round_trip():
    frame = encode_frame(0x2115, b"hello")
    assert frame[:2] == b"\x00\x06"
    assert parse_header(frame[:8]) == (5, 0x2115)
    assert frame[8:] == b"hello"


def test_open_channel_matches_captured_bytes():
    # Captured from the app opening UCFG as handle 0x2116.
    assert encode_open_channel("UCFG", 0x2116).hex() == "cdab01000700000055434647162100"


def test_open_channel_rejects_wrong_name_length():
    with pytest.raises(ValueError):
        encode_open_channel("UCF", 1)


def test_open_channel_with_registration_appends_address():
    body = encode_open_channel("CTPP", 0x2117, b"SB0000421\x00")
    assert body.startswith(OPEN_MAGIC)
    assert body.endswith(b"SB0000421\x00")


def test_close_channel_and_mgmt_parsing():
    assert encode_close_channel(0x2117).startswith(CLOSE_MAGIC)
    magic, handle, name = parse_mgmt(OPENACK_MAGIC + b"\x04\x00\x00\x00\x16\x21\x00\x00")
    assert magic == OPENACK_MAGIC
    assert handle == 0x2116
    assert name is None


def test_parse_header_rejects_bad_magic():
    with pytest.raises(ValueError):
        parse_header(b"\x01\x02\x00\x00\x00\x00\x00\x00")
