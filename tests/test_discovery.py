"""UDP discovery decoding."""

import pytest

from custom_components.comelit_vip.viper.discovery import parse_info_reply


def test_rejects_short_or_foreign_payloads():
    with pytest.raises(ValueError):
        parse_info_reply(b"nope")
    with pytest.raises(ValueError):
        parse_info_reply(b"info" + b"\x00" * 20)


def test_parse_reply():
    raw = bytearray(166)
    raw[0:4] = b"info"
    raw[4:14] = b"SB900001\x00\x00"
    raw[14:20] = bytes.fromhex("aabbccddeeff")
    raw[20:24] = b"D486"
    raw[24:28] = b"Port"
    raw[32:37] = b"2.1.3"
    raw[112:116] = b"ViP_"
    raw[156:160] = b"MSVF"
    info = parse_info_reply(bytes(raw))
    assert info.vip_address == "SB900001"
    assert info.mac == "aa:bb:cc:dd:ee:ff"
    assert info.firmware == "2.1.3"
    assert info.model_id == "MSVF"
    assert "6741W" in info.model


def test_rejects_non_info_reply():
    from custom_components.comelit_vip.viper.discovery import parse_info_reply

    with pytest.raises(ValueError):
        parse_info_reply(b"nope" + b"\x00" * 200)


async def test_endpoint_failure_is_no_reply(monkeypatch):
    import asyncio

    from custom_components.comelit_vip.viper.discovery import async_discover

    async def _boom(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", _boom)

    assert await async_discover("192.0.2.1") is None
