"""A fake panel speaking enough of the wire protocol for the tests.

It answers channel opens, the JSON channels and registration, and can push an
INVITE, release a call, and drop the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct

from custom_components.comelit_vip.viper.ctp import (
    FLAG_DATA,
    FLAG_SYN,
    OP_CAPABILITIES,
    OP_INVITE,
    OP_MEDIA_REQUEST,
    OP_OPEN_DOOR,
    OP_REGISTER,
    OP_REGISTER_RESPONSE,
    OP_RELEASE,
    OP_SETUP_ACK,
    TAG_ENTRANCE,
    build_ctp_packet,
    encode_logaddr,
    parse_ctp_packet,
    peer_connection_id,
)
from custom_components.comelit_vip.viper.frames import (
    CLOSE_MAGIC,
    CLOSEACK_MAGIC,
    MGMT_HANDLE,
    OPEN_MAGIC,
    OPENACK_MAGIC,
    encode_frame,
    encode_open_ack,
    encode_open_channel,
    parse_header,
    parse_mgmt,
)

APARTMENT = "SB000042"
SUBADDRESS = 2
OUR_ADDRESS = f"{APARTMENT}{SUBADDRESS}"
ENTRANCE = "SB900001"

CONFIGURATION = {
    "viper-client": {"description": "Home Assistant"},
    "vip": {
        "apt-address": APARTMENT,
        "apt-subaddress": SUBADDRESS,
        "user-parameters": {
            "entrance-address-book": [{"id": 0, "name": "Entrance", "apt-address": ENTRANCE}],
            "opendoor-address-book": [
                {"id": 0, "name": "Entrance lock", "apt-address": ENTRANCE, "output-index": 1, "secure-mode": False}
            ],
            "actuator-address-book": [],
        },
    },
}


class FakePanel:
    """One client at a time, like the real panel."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 0
        self.channels: dict[int, str] = {}
        self.ctpp_handle: int | None = None
        self.registrations = 0
        self.acks = 0
        self.calls = 0
        self.answer_call = True
        self.refuse_call_cause: int | None = None
        # A 6741W offers media on every ring, 0.2 s after the INVITE.
        self.offer_media_on_ring = True
        self.call_connection: bytes | None = None
        self.media_handle: int | None = None
        self.media_requests = 0
        self.open_cause = 0
        self.answer_door = True
        self.door_connection: bytes | None = None
        self.releases = 0
        self.registration_connection: bytes | None = None
        self.client_acked: list[int] = []
        self.rtpc_handles: list[int] = []
        self.opened: list[tuple[str, int]] = []
        self.connections = 0
        self.answer_registration = True
        self.answer_server_info = True
        self.server_infos = 0
        # How long the panel takes to answer ``get-configuration``, which
        # is the slowest step of the handshake on a real panel.
        self.configuration_delay = 0.0
        self.connected = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._client: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Bind on a free port."""
        self._server = await asyncio.start_server(self._serve, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Drop the client, then close the listener.

        In that order: ``wait_closed`` does not return while a handler still
        holds an open transport.
        """
        client, self._client = self._client, None
        if client is not None and client is not asyncio.current_task():
            client.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await client
        await self.drop()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def drop(self) -> None:
        """Close the connection without warning."""
        writer, self._writer = self._writer, None
        self.ctpp_handle = None
        self.connected.clear()
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ------------------------------------------------------------------ pushing
    async def ring(
        self,
        *,
        tag: bytes = TAG_ENTRANCE,
        connection: bytes = b"\x77\x0c",  # a 6741W picks ids with the top bit clear
        source_raw: bytes | None = None,
        call_id: bytes = b"\xaa\xbb\xcc\xdd",
    ) -> bytes:
        """Push an INVITE and return the connection id used.

        ``source_raw`` replaces the ten source-address bytes in the trailer.
        """
        body = (
            struct.pack(">H", OP_INVITE)
            + encode_logaddr(ENTRANCE)
            + encode_logaddr(OUR_ADDRESS)
            + b"\x01\x20"
            + call_id
            + encode_logaddr(ENTRANCE)
            + tag
        )
        await self._send_ctp(connection, body, FLAG_SYN, source_raw=source_raw)
        if self.offer_media_on_ring:
            await self._send_ctp(connection, struct.pack(">H", OP_CAPABILITIES) + b"\x50\x03\x3b\x00\x00\x00", FLAG_DATA)
        return connection

    async def knock(self, opcode: int, *, connection: bytes = b"\x82\x22") -> bytes:
        """Open a connection towards the client with an opcode other than INVITE."""
        await self._send_ctp(connection, struct.pack(">H", opcode) + b"\x00" * 30, FLAG_SYN)
        return connection

    async def release(self, connection: bytes, cause: int = 0) -> None:
        """End a call the panel raised."""
        await self._send_ctp(connection, struct.pack(">HB", OP_RELEASE, cause), FLAG_DATA)

    async def push_video(self, payload: bytes = b"\x65\x88", *, sequence: int = 1, timestamp: int = 3000) -> None:
        """Send one RTP packet on the handle the client is listening on."""
        if self.media_handle is None:
            raise RuntimeError("the client has not opened its media channels")
        header = struct.pack(">BBHII", 0x80, 0x80 | 96, sequence, timestamp, 0x11223344)
        await self._send(self.media_handle, header + payload)

    async def renew_registration(self) -> None:
        """Repeat the registration answer; the client should answer with a FIN. A 6741W never does this."""
        if self.registration_connection is None:
            raise RuntimeError("the client has not registered")
        body = struct.pack(">H", OP_REGISTER_RESPONSE) + peer_connection_id(self.registration_connection) + b"\x00" * 12
        await self._send_ctp(self.registration_connection, body, FLAG_DATA)

    async def open_channel_towards_client(self, name: str = "RTPC") -> int:
        """Open a channel from this side, the way the panel opens a media one."""
        handle = 0x5000 + len(self.channels)
        await self._send(MGMT_HANDLE, encode_open_channel(name, handle))
        return handle

    async def end_call(self, cause: int = 0) -> None:
        """Release the call the client placed."""
        if self.call_connection is None:
            raise RuntimeError("no call is up")
        await self.release(self.call_connection, cause)

    # ------------------------------------------------------------------ serving
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writer = writer
        self.channels = {}
        self.ctpp_handle = None
        self.connected.set()
        self._client = asyncio.current_task()
        try:
            while True:
                header = await reader.readexactly(8)
                length, handle = parse_header(header)
                payload = await reader.readexactly(length) if length else b""
                await self._on_frame(handle, payload)
        except asyncio.IncompleteReadError, ConnectionResetError, OSError:
            pass
        finally:
            self.connected.clear()
            # Close, but do not await it here. A cancelled handler awaiting the
            # close waiter cancels that shared future, and every later
            # ``wait_closed`` on the same writer then raises.
            writer.close()

    async def _on_frame(self, handle: int, payload: bytes) -> None:
        if handle == MGMT_HANDLE:
            magic, channel, name = parse_mgmt(payload)
            if magic == OPEN_MAGIC:
                self.channels[channel] = name or ""
                if name == "CTPP":
                    self.ctpp_handle = channel
                if name == "RTPC":
                    self.rtpc_handles.append(channel)
                    # The client listens for video on the handle after the one
                    # it offers, which is the second RTPC channel it opens.
                    if len(self.rtpc_handles) == 2:
                        self.media_handle = (channel + 1) & 0xFFFF
                await self._send(MGMT_HANDLE, encode_open_ack(channel))
            elif magic == OPENACK_MAGIC:
                self.client_acked.append(channel)
            elif magic == CLOSE_MAGIC:
                self.channels.pop(channel, None)
                await self._send(MGMT_HANDLE, CLOSEACK_MAGIC + struct.pack("<I", 2) + struct.pack("<H", channel))
            return
        if handle == self.ctpp_handle:
            await self._on_ctp(payload)
            return
        if payload[:1] == b"{":
            await self._on_json(handle, payload)

    async def _on_json(self, handle: int, payload: bytes) -> None:
        request = json.loads(payload)
        message = request.get("message")
        response: dict[str, object] = {
            "message": message,
            "message-type": "response",
            "message-id": request.get("message-id"),
            "response-code": 200,
            "response-string": "ok",
        }
        if message == "get-configuration":
            if self.configuration_delay:
                await asyncio.sleep(self.configuration_delay)
            response.update(CONFIGURATION)
        elif message == "server-info":
            self.server_infos += 1
            if not self.answer_server_info:
                return
            response.update({"model": "MSVF", "version": "2.1.3"})
        await self._send(handle, json.dumps(response).encode())

    async def _on_ctp(self, payload: bytes) -> None:
        packet = parse_ctp_packet(payload)
        if not packet.body:
            # An ack or a FIN; counted so tests can see the client answered.
            self.acks += 1
            return
        if packet.opcode == OP_REGISTER:
            self.registrations += 1
            if not self.answer_registration:
                return
            self.registration_connection = peer_connection_id(packet.connection)
            body = struct.pack(">H", OP_REGISTER_RESPONSE) + packet.connection + b"\x00" * 12
            await self._send_ctp(self.registration_connection, body, FLAG_DATA)
            return
        if packet.opcode == OP_RELEASE:
            self.releases += 1
            return
        if packet.opcode == OP_OPEN_DOOR:
            self.opened.append((packet.destination, packet.body[-1]))
            self.door_connection = peer_connection_id(packet.connection)
            if not self.answer_door:
                return
            await self._send_ctp(
                peer_connection_id(packet.connection), struct.pack(">HB", OP_RELEASE, self.open_cause), FLAG_DATA
            )
            return
        await self._on_call(packet)

    async def _on_call(self, packet) -> None:
        """Play the panel's half of an outgoing video call, answering on the peer of the client's connection."""
        answer = peer_connection_id(packet.connection)
        if packet.opcode == OP_INVITE:
            self.calls += 1
            if self.refuse_call_cause is not None:
                await self._send_ctp(answer, b"", 0)
                await self._send_ctp(answer, struct.pack(">HB", OP_RELEASE, self.refuse_call_cause), FLAG_DATA)
                return
            if not self.answer_call:
                return
            self.call_connection = answer
            await self._send_ctp(answer, struct.pack(">H", OP_INVITE) + b"\x00" * 30, FLAG_DATA)
            return
        if packet.opcode == OP_CAPABILITIES:
            await self._send_ctp(answer, struct.pack(">H", OP_CAPABILITIES) + b"\x00" * 6, FLAG_DATA)
            await self._send_ctp(answer, struct.pack(">H", OP_SETUP_ACK) + b"\x00" * 6, FLAG_DATA)
            return
        if packet.opcode == OP_MEDIA_REQUEST:
            self.media_requests += 1
            # Only the audio offer (the first) is answered; the client expects no answer to the video one.
            if self.media_requests == 1:
                await self._send_ctp(answer, struct.pack(">H", OP_MEDIA_REQUEST) + b"\x00" * 6, FLAG_DATA)

    # ------------------------------------------------------------------ writing
    async def _send_ctp(self, connection: bytes, body: bytes, flags: int, *, source_raw: bytes | None = None) -> None:
        if self.ctpp_handle is None:
            raise RuntimeError("the client has not opened CTPP")
        packet = build_ctp_packet(
            flags=flags,
            connection=connection,
            sequence=1,
            acknowledgement=1,
            body=body,
            source=ENTRANCE,
            destination=OUR_ADDRESS,
        )
        if source_raw is not None:
            if len(source_raw) != 10:
                raise ValueError("a source address is ten bytes")
            # Trailer layout: marker, source, destination.
            packet = packet[:-20] + source_raw + packet[-10:]
        await self._send(self.ctpp_handle, packet)

    async def _send(self, handle: int, payload: bytes) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            return
        writer.write(encode_frame(handle, payload))
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()


async def until(ready, passes: int = 400) -> None:
    """Poll with a real delay: ``sleep(0)`` need not poll sockets."""
    for _ in range(passes):
        if ready():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("it never happened")
