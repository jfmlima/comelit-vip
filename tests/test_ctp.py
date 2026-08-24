"""CTP framing round-trips and decoding."""

import struct

import pytest

from custom_components.comelit_vip.viper.ctp import (
    FLAG_DATA,
    OP_OPEN_DOOR,
    OP_RELEASE,
    build_ctp_packet,
    decode_logaddr,
    encode_logaddr,
    parse_ctp_packet,
    peer_connection_id,
)


def test_logaddr_round_trip():
    assert decode_logaddr(encode_logaddr("SB0000421")) == "SB0000421"
    assert len(encode_logaddr("SB900001")) == 10


def test_logaddr_rejects_long_address():
    with pytest.raises(ValueError):
        encode_logaddr("SB0000421TOOLONG")


def test_logaddr_bytes_round_trip():
    """The caller's address is echoed back in the acknowledgement, so every byte must survive."""
    raw = b"SB\xc3\x9c0001\x00\x00"

    assert encode_logaddr(decode_logaddr(raw)) == raw


def test_logaddr_rejects_non_latin1():
    with pytest.raises(ValueError):
        encode_logaddr("SB\u20ac0001")


def test_logaddr_rejects_nul():
    with pytest.raises(ValueError):
        encode_logaddr("SB\x000001")


@pytest.mark.parametrize("body_len", [0, 1, 3, 4, 13, 26])
def test_ctp_round_trip_pads_body(body_len):
    body = bytes(range(body_len))
    raw = build_ctp_packet(
        flags=FLAG_DATA,
        connection=b"\x7d\x90",
        sequence=7,
        acknowledgement=9,
        body=body,
        source="SB0000421",
        destination="SB900001",
    )
    assert len(raw) % 4 == 0
    packet = parse_ctp_packet(raw)
    assert packet.body == body
    assert packet.sequence == 7
    assert packet.acknowledgement == 9
    assert packet.source == "SB0000421"
    assert packet.destination == "SB900001"


def test_open_door_body_and_release_cause():
    body = struct.pack(">H", OP_OPEN_DOOR) + encode_logaddr("SB900001") + b"\x01"
    packet = parse_ctp_packet(
        build_ctp_packet(
            flags=FLAG_DATA,
            connection=b"\x00\x01",
            sequence=0,
            acknowledgement=0,
            body=body,
            source="SB0000421",
            destination="SB900001",
        )
    )
    assert packet.opcode == OP_OPEN_DOOR

    release = parse_ctp_packet(
        build_ctp_packet(
            flags=FLAG_DATA,
            connection=b"\x00\x01",
            sequence=1,
            acknowledgement=1,
            body=struct.pack(">HB", OP_RELEASE, 0),
            source="SB900001",
            destination="SB0000421",
        )
    )
    assert release.release_cause == 0


def test_bad_trailer_rejected():
    raw = bytearray(
        build_ctp_packet(
            flags=FLAG_DATA,
            connection=b"\x00\x01",
            sequence=0,
            acknowledgement=0,
            body=b"",
            source="SB0000421",
            destination="SB900001",
        )
    )
    raw[8:12] = b"\x00\x00\x00\x00"

    with pytest.raises(ValueError):
        parse_ctp_packet(bytes(raw))


def test_peer_connection_id_flips_top_bit():
    assert peer_connection_id(b"\x7d\x90") == b"\xfd\x90"
    assert peer_connection_id(b"\xfd\x90") == b"\x7d\x90"


def test_parse_rejects_truncated_and_bad_version():
    with pytest.raises(ValueError):
        parse_ctp_packet(b"\x00" * 8)
    raw = bytearray(
        build_ctp_packet(
            flags=0,
            connection=b"\x00\x01",
            sequence=0,
            acknowledgement=0,
            body=b"",
            source="A",
            destination="B",
        )
    )
    raw[1] = 0x19
    with pytest.raises(ValueError):
        parse_ctp_packet(bytes(raw))


# Captured from a 6741W: somebody at the entrance panel. The packet is carried
# from the apartment address while the body names the entrance panel.
ENTRANCE_INVITE = bytes.fromhex("0001534239303030303100005342303030303432000001006c24aa4d534239303030303100005050")


def test_parse_invite():
    from custom_components.comelit_vip.viper.ctp import parse_invite

    origin, destination, call_id, tag = parse_invite(ENTRANCE_INVITE)
    assert origin == "SB900001"
    assert destination == "SB000042"
    assert call_id == bytes.fromhex("6c24aa4d")
    assert tag == b"PP"


def test_short_invite_rejected():
    from custom_components.comelit_vip.viper.ctp import parse_invite

    with pytest.raises(ValueError):
        parse_invite(ENTRANCE_INVITE[:20])


def test_invite_missing_call_id_rejected():
    """24 bytes slices without error and yields an empty call id and tag."""
    from custom_components.comelit_vip.viper.ctp import parse_invite

    with pytest.raises(ValueError):
        parse_invite(ENTRANCE_INVITE[:24])


# Captured minutes apart from the same 6741W: a visitor, then a floor call.
# Every address matches and only the trailing tag differs.
FLOOR_INVITE = bytes.fromhex("00015342393030303031000053423030303034320000010099b09e23534239303030303100004646")


def test_floor_call_tag():
    from custom_components.comelit_vip.viper.ctp import TAG_ENTRANCE, TAG_FLOOR_CALL, parse_invite

    entrance = parse_invite(ENTRANCE_INVITE)
    floor = parse_invite(FLOOR_INVITE)

    assert entrance[0] == floor[0] == "SB900001"
    assert entrance[1] == floor[1] == "SB000042"
    assert entrance[3] == TAG_ENTRANCE
    assert floor[3] == TAG_FLOOR_CALL
