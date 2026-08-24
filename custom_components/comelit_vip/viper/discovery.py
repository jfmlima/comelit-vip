"""UDP discovery on port 24199. Reports model, firmware and MAC."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

DISCOVERY_PORT = 24199
INFO_REQUEST = b"INFO" + b"\x00" * 8

MODEL_NAMES = {
    "MSVF": "6741W Mini hands-free Wi-Fi",
    "MSVU": "6741W/6701W Mini hands-free Wi-Fi",
    "MnWi": "6742W Mini ViP Wi-Fi",
    "MxWi": "6842W Maxi ViP Wi-Fi",
    "Vist": "Visto Wi-Fi ViP",
    "Extd": "1456 ViP gateway",
    "ExtS": "1456S ViP gateway",
    "HSrv": "Comelit Hub",
}


@dataclass(slots=True, frozen=True)
class DeviceInfo:
    """Decoded INFO reply."""

    vip_address: str
    mac: str
    hw_id: str
    app_id: str
    firmware: str
    system_id: str
    model_id: str

    @property
    def model(self) -> str:
        """Return the product name for this model id."""
        return MODEL_NAMES.get(self.model_id, self.model_id)


def parse_info_reply(data: bytes) -> DeviceInfo:
    """Decode an ``info`` reply."""
    if len(data) < 160 or not data.startswith(b"info"):
        raise ValueError("not an INFO reply")

    def text(a: int, b: int) -> str:
        return data[a:b].split(b"\x00", 1)[0].decode("ascii", "replace")

    return DeviceInfo(
        vip_address=text(4, 14),
        mac=":".join(f"{b:02x}" for b in data[14:20]),
        hw_id=text(20, 24),
        app_id=text(24, 28),
        firmware=text(32, 112),
        system_id=text(112, 116),
        model_id=text(156, 160),
    )


class _InfoProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.reply: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if not self.reply.done():
            self.reply.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.reply.done():
            self.reply.set_exception(exc)


async def async_discover(host: str, timeout: float = 3.0) -> DeviceInfo | None:
    """Send an INFO datagram to ``host``, returning None if nothing answers."""
    loop = asyncio.get_running_loop()
    transport = None
    try:
        transport, protocol = await loop.create_datagram_endpoint(_InfoProtocol, remote_addr=(host, DISCOVERY_PORT))
        transport.sendto(INFO_REQUEST)
        data = await asyncio.wait_for(protocol.reply, timeout)
        return parse_info_reply(data)
    except TimeoutError, OSError, ValueError:
        return None
    finally:
        if transport is not None:
            transport.close()
