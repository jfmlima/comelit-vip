"""Session with a Comelit ViP panel over TCP 64100.

One TCP connection, one reader task, one CTPP channel. Registration, door
open, inbound calls and the outgoing video call are all CTP connections on
that channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import socket
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count

from .ctp import (
    FLAG_ACK,
    FLAG_DATA,
    FLAG_FIN,
    FLAG_SYN,
    OP_INVITE,
    OP_OPEN_DOOR,
    OP_REGISTER,
    OP_REGISTER_RESPONSE,
    OP_RELEASE,
    CtpPacket,
    build_ctp_packet,
    encode_logaddr,
    parse_ctp_packet,
    parse_invite,
    peer_connection_id,
)
from .errors import ViperError
from .frames import (
    CLOSEACK_MAGIC,
    MGMT_HANDLE,
    OPEN_MAGIC,
    OPENACK_MAGIC,
    Frame,
    encode_close_channel,
    encode_frame,
    encode_open_ack,
    encode_open_channel,
    parse_header,
    parse_mgmt,
)
from .models import PanelConfig, RingEvent
from .tasks import end_task

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 64100
REQUEST_TIMEOUT = 10.0
CTP_TIMEOUT = 10.0
# Backstop for a ring the panel never releases.
INBOUND_CALL_TIMEOUT = 600.0
_REGISTER_TAIL = b"\x10\x0e\x00"
FIRST_HANDLE = 0x7474

RingCallback = Callable[[RingEvent], None]
CallEndCallback = Callable[[RingEvent, int | None], None]
DisconnectCallback = Callable[[Exception | None], None]


class ViperAuthError(ViperError):
    """The panel did not accept the user token; ``code`` is its response code."""

    def __init__(self, message: str, code: object = None) -> None:
        super().__init__(message)
        self.code = code


class ViperConnectionError(ViperError):
    """The TCP connection failed or was lost."""


class ViperRefusedError(ViperError):
    """The panel released a connection instead of answering it."""

    def __init__(self, cause: int | None) -> None:
        super().__init__(f"the panel refused the request (cause {cause})")
        self.cause = cause


class ViperTimeoutError(ViperError):
    """The panel did not answer in time."""


class CtpConnection:
    """One transaction on the CTPP channel."""

    def __init__(self, session: ViperSession, local_id: bytes, peer_id: bytes, *, source: str, destination: str) -> None:
        self.session = session
        self.local_id = local_id
        self.peer_id = peer_id
        self.source = source
        self.destination = destination
        self.sequence = secrets.randbelow(256)
        self.acknowledgement = secrets.randbelow(256)
        self.inbox: asyncio.Queue[CtpPacket | None] = asyncio.Queue()
        self.auto_ack = True
        self.persistent = False  # registration keeps routing after its FIN
        self.call_id: bytes | None = None
        self.event: RingEvent | None = None
        self.failed = False
        self.failure = ""
        self.end_reported = False

    async def send(self, body: bytes, flags: int = FLAG_DATA) -> None:
        """Send a packet on this connection."""
        # A displaced connection's id belongs to the inbound call now; a
        # RELEASE or FIN sent here would end that call on the panel.
        if self.failed:
            raise ViperConnectionError(self.failure)
        packet = build_ctp_packet(
            flags=flags,
            connection=self.local_id,
            sequence=self.sequence,
            acknowledgement=self.acknowledgement,
            body=body,
            source=self.source,
            destination=self.destination,
        )
        await self.session.send_ctp(packet)
        if body:
            self.sequence = (self.sequence + 1) & 0xFF

    async def ack(self, packet: CtpPacket) -> None:
        """Acknowledge a packet."""
        self.acknowledgement = (packet.sequence + 1) & 0xFF
        await self.send(b"", FLAG_ACK)

    async def fin(self) -> None:
        """Close this connection."""
        await self.send(b"", FLAG_FIN)

    async def handle_incoming(self, packet: CtpPacket) -> None:
        """Take a packet the reader routed here."""
        if packet.body and self.auto_ack:
            await self.ack(packet)
        self.inbox.put_nowait(packet)

    def fail(self, reason: str = "connection displaced by an inbound call") -> None:
        """End this connection and wake anything waiting on it."""
        self.failed = True
        self.failure = reason
        self.inbox.put_nowait(None)

    async def wait(self, *, opcode: int | None = None, timeout: float | None = None) -> CtpPacket:
        """Wait for the next packet, optionally matching an opcode."""
        timeout = CTP_TIMEOUT if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ViperTimeoutError(f"no CTP packet (opcode={opcode}) within {timeout}s")
            try:
                packet = await asyncio.wait_for(self.inbox.get(), remaining)
            except TimeoutError as exc:
                raise ViperTimeoutError(f"no CTP packet (opcode={opcode}) within {timeout}s") from exc
            if packet is None:
                self.inbox.put_nowait(None)  # keep the sentinel for the next waiter
                raise ViperConnectionError(self.failure)
            if opcode is None or packet.opcode == opcode:
                return packet
            if packet.opcode == OP_RELEASE:
                raise ViperRefusedError(packet.release_cause)


class ViperSession:
    """Client for one panel."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, *, logger: logging.Logger | None = None) -> None:
        self.host = host
        self.port = port
        self.log = logger or _LOGGER
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._handles = count(FIRST_HANDLE)
        self._msg_ids = count(1)
        self._open_waiters: dict[int, asyncio.Future[int]] = {}
        self._open_handles: set[int] = set()
        self._json_waiters: dict[tuple[int, int], asyncio.Future[dict]] = {}
        self._binary_handlers: dict[int, Callable[[int, bytes], None]] = {}
        self._incoming_channel_handler: Callable[[str, int], None] | None = None
        self._ctpp_handle: int | None = None
        self._connections: dict[bytes, CtpConnection] = {}
        self._call_tasks: set[asyncio.Task] = set()
        self.config: PanelConfig | None = None
        self.source: str | None = None
        self.on_ring: RingCallback | None = None
        self.on_call_end: CallEndCallback | None = None
        self.on_disconnect: DisconnectCallback | None = None
        self.last_rx: float = 0.0
        self.registered_at: float | None = None
        self.refreshes = 0
        self.renewals = 0
        self._registration: CtpConnection | None = None
        self._tearing_down = False

    # ------------------------------------------------------------------ transport
    @property
    def local_address(self) -> str | None:
        """Return our IP on the interface that reaches the panel."""
        if self._writer is None:
            return None
        sockname = self._writer.get_extra_info("sockname")
        return sockname[0] if sockname else None

    @property
    def connected(self) -> bool:
        """Return whether the socket is open and the reader is running."""
        return self._writer is not None and not self._writer.is_closing() and self._reader_task is not None

    @property
    def registered(self) -> bool:
        """Return whether the last registration handshake succeeded."""
        return self.registered_at is not None

    @property
    def registration_age(self) -> float | None:
        """Return how long ago the registration was last confirmed."""
        if self.registered_at is None:
            return None
        return asyncio.get_running_loop().time() - self.registered_at

    @property
    def calls_active(self) -> bool:
        """Return whether a call the panel raised is still being followed."""
        return bool(self._call_tasks)

    async def connect(self) -> None:
        """Open the connection and start the reader."""
        await self._teardown(None, notify=False)
        try:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), REQUEST_TIMEOUT)
        except (OSError, TimeoutError) as exc:
            raise ViperConnectionError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.last_rx = asyncio.get_running_loop().time()
        self._handles = count(FIRST_HANDLE)
        self._open_handles.clear()
        self._reader_task = asyncio.create_task(self._read_loop(), name="comelit_vip.reader")

    async def close(self) -> None:
        """Close the connection without raising the disconnect callback."""
        await self._teardown(None, notify=False)

    async def _teardown(self, error: Exception | None, *, notify: bool) -> None:
        """Stop the reader and call tasks, close the socket, fail every waiter."""
        if self._tearing_down:
            return
        self._tearing_down = True
        try:
            # The reader calls this from inside itself; awaiting it would deadlock.
            current = asyncio.current_task()
            tasks = [task for task in (self._reader_task, *self._call_tasks) if task is not None and task is not current]
            self._reader_task = None
            self._call_tasks.clear()
            for task in tasks:
                await end_task(task)
            writer, self._writer, self._reader = self._writer, None, None
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            error = error or ViperConnectionError("session closed")
            for conn in self._connections.values():
                conn.fail(str(error))
            self._connections.clear()
            self._binary_handlers.clear()
            self._open_handles.clear()
            self._incoming_channel_handler = None
            self._ctpp_handle = None
            self._registration = None
            self.registered_at = None
            self._fail_waiters(error)
        finally:
            self._tearing_down = False
        if notify and self.on_disconnect is not None:
            try:
                self.on_disconnect(error)
            except Exception:
                self.log.exception("disconnect callback failed")

    async def _send_frame(self, handle: int, payload: bytes) -> None:
        if self._writer is None or self._writer.is_closing():
            raise ViperConnectionError("not connected")
        data = encode_frame(handle, payload)
        async with self._write_lock:
            try:
                self._writer.write(data)
                await self._writer.drain()
            except (OSError, ConnectionError) as exc:
                raise ViperConnectionError(f"write failed: {exc}") from exc

    async def send_ctp(self, packet: bytes) -> None:
        """Send a raw packet on the CTPP channel."""
        if self._ctpp_handle is None:
            raise ViperError("CTPP channel is not open")
        await self._send_frame(self._ctpp_handle, packet)

    async def _read_loop(self) -> None:
        error: Exception | None = None
        try:
            assert self._reader is not None
            while True:
                header = await self._reader.readexactly(8)
                length, handle = parse_header(header)
                payload = await self._reader.readexactly(length) if length else b""
                self.last_rx = asyncio.get_running_loop().time()
                try:
                    await self._dispatch(Frame(handle, payload))
                except Exception:
                    self.log.exception("error handling frame on handle %#06x", handle)
        except asyncio.CancelledError:
            raise  # cancelled by a teardown already in progress
        except asyncio.IncompleteReadError:
            error = ViperConnectionError("panel closed the connection")
        except (OSError, ConnectionError, ValueError) as exc:
            error = ViperConnectionError(str(exc))
        await self._teardown(error or ViperConnectionError("reader stopped"), notify=True)

    def _fail_waiters(self, exc: Exception) -> None:
        for fut in list(self._open_waiters.values()) + list(self._json_waiters.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._open_waiters.clear()
        self._json_waiters.clear()

    # ------------------------------------------------------------------ dispatch
    async def _dispatch(self, frame: Frame) -> None:
        if frame.handle == MGMT_HANDLE:
            magic, handle, name = parse_mgmt(frame.payload)
            if magic == OPENACK_MAGIC:
                fut = self._open_waiters.pop(handle, None)
                if fut is not None and not fut.done():
                    fut.set_result(handle)
            elif magic == OPEN_MAGIC:
                # The panel's handle shares our pool.
                self._open_handles.add(handle)
                await self._send_frame(MGMT_HANDLE, encode_open_ack(handle))
                if self._incoming_channel_handler is not None and name is not None:
                    self._incoming_channel_handler(name, handle)
                else:
                    self.log.debug("unexpected incoming channel %s handle=%#06x", name, handle)
            elif magic == CLOSEACK_MAGIC:
                pass
            else:
                self.log.debug("unknown mgmt frame: %s", frame.payload.hex())
            return

        if frame.handle == self._ctpp_handle:
            await self._dispatch_ctp(frame.payload)
            return

        handler = self._binary_handlers.get(frame.handle)
        if handler is not None:
            handler(frame.handle, frame.payload)
            return

        if frame.payload[:1] == b"{":
            try:
                obj = json.loads(frame.payload.decode("utf-8", "replace"))
            except ValueError:
                self.log.debug("bad JSON on handle %#06x: %r", frame.handle, frame.payload[:80])
                return
            key = (frame.handle, int(obj.get("message-id", -1)))
            fut = self._json_waiters.pop(key, None)
            if fut is not None and not fut.done():
                fut.set_result(obj)
            else:
                self.log.debug("unsolicited JSON on handle %#06x: %s", frame.handle, obj)
            return
        self.log.debug("frame on unknown handle %#06x: %s", frame.handle, frame.payload[:32].hex())

    async def _dispatch_ctp(self, payload: bytes) -> None:
        try:
            packet = parse_ctp_packet(payload)
        except ValueError as exc:
            self.log.debug("not a CTP packet (%s): %s", exc, payload.hex())
            return
        self.log.debug(
            "CTP rx flags=%#04x conn=%s seq=%d ack=%d op=%s %s->%s body=%s",
            packet.flags,
            packet.connection.hex(),
            packet.sequence,
            packet.acknowledgement,
            f"{packet.opcode:#06x}" if packet.opcode is not None else None,
            packet.source,
            packet.destination,
            packet.body.hex(),
        )
        conn = self._connections.get(packet.connection)
        if conn is not None and self._displaces(packet, conn):
            self._displace(conn)
            conn = None
        if conn is not None:
            await conn.handle_incoming(packet)
            if conn.persistent and packet.opcode == OP_REGISTER_RESPONSE and not packet.is_syn:
                # A repeated registration answer is a renewal; the app answers it with a FIN.
                self.renewals += 1
                await conn.fin()
            return
        if packet.is_syn:
            await self._accept_inbound(packet)
            return
        self.log.debug("CTP packet for unknown connection %s", packet.connection.hex())

    def _displaces(self, packet: CtpPacket, conn: CtpConnection) -> bool:
        """Return whether ``packet`` is a new inbound call on an id ``conn`` holds.

        A 6741W picks inbound ids with the top bit clear, so they do not meet
        our outbound ids; what they can meet is an earlier inbound call whose
        RELEASE was missed. The panel echoes our INVITE back, so the opcode
        alone is not enough; the call id tells a retransmit from a new call.
        """
        if not packet.is_syn or packet.opcode != OP_INVITE:
            return False
        try:
            _origin, _callee, call_id, _tag = parse_invite(packet.body)
        except ValueError:
            call_id = b""
        return conn.call_id != call_id

    def _displace(self, conn: CtpConnection) -> None:
        """Hand a connection's id to the inbound call that landed on it."""
        self.log.warning(
            "inbound call landed on connection %s, which was in use towards %s; ending that one",
            conn.peer_id.hex(),
            conn.destination,
        )
        self._connections.pop(conn.peer_id, None)
        conn.fail()
        if conn is self._registration:
            self._registration = None
        if conn.event is not None:
            # Reported here so the old call's end precedes the new ring.
            self._report_call_end(conn.event, None)
            conn.end_reported = True

    async def _accept_inbound(self, packet: CtpPacket) -> None:
        """Adopt a connection the panel opened."""
        conn = CtpConnection(
            self,
            local_id=peer_connection_id(packet.connection),
            peer_id=packet.connection,
            source=self.source or packet.destination,
            destination=packet.source,
        )
        if packet.opcode != OP_INVITE:
            self.log.info("inbound CTP connection with opcode %s from %s", packet.opcode, packet.source)
            with contextlib.suppress(ViperError, OSError, ValueError):
                await conn.handle_incoming(packet)
            return
        event = self._ring_event(packet)
        conn.call_id = event.call_id
        conn.event = event
        self._connections[packet.connection] = conn
        self.log.info("incoming call raised by %s, carried from %s", event.origin or "?", packet.source)
        # Report the ring before the acknowledgement, which can fail.
        self._report_ring(event)
        try:
            await conn.handle_incoming(packet)
        except (ViperError, OSError, ValueError) as err:
            self.log.warning("could not acknowledge the call from %s (%s)", packet.source, err)
            self._connections.pop(packet.connection, None)
            self._report_call_end(event, None)
            return
        task = asyncio.create_task(self._watch_inbound_call(conn, event), name="comelit_vip.inbound_call")
        self._call_tasks.add(task)
        task.add_done_callback(self._call_tasks.discard)

    def _ring_event(self, packet: CtpPacket) -> RingEvent:
        """Build a RingEvent from an INVITE, leaving unreadable fields empty."""
        origin = ""
        call_id = tag = b""
        try:
            origin, _callee, call_id, tag = parse_invite(packet.body)
        except ValueError:
            self.log.debug("unreadable INVITE body: %s", packet.body.hex())
        return RingEvent(
            caller=packet.source,
            callee=packet.destination,
            connection=packet.connection,
            call_id=call_id,
            received_at=datetime.now(UTC),
            body=packet.body,
            origin=origin,
            tag=tag,
        )

    def _report_ring(self, event: RingEvent) -> None:
        if self.on_ring is not None:
            try:
                self.on_ring(event)
            except Exception:
                self.log.exception("ring callback failed")

    def _report_call_end(self, event: RingEvent, cause: int | None) -> None:
        if self.on_call_end is not None:
            try:
                self.on_call_end(event, cause)
            except Exception:
                self.log.exception("call-end callback failed")

    async def _watch_inbound_call(self, conn: CtpConnection, event: RingEvent) -> None:
        """Follow an inbound call until the panel releases it, then report its end."""
        cause: int | None = None
        try:
            while True:
                packet = await conn.wait(timeout=INBOUND_CALL_TIMEOUT)
                if packet.opcode == OP_RELEASE:
                    cause = packet.release_cause
                    break
                if packet.is_fin:
                    break
            with contextlib.suppress(ViperError):
                await conn.fin()
        except ViperTimeoutError:
            self.log.debug("inbound call %s never released; dropping", conn.peer_id.hex())
        except ViperConnectionError:
            self.log.debug("inbound call %s ended with the connection", conn.peer_id.hex())
        finally:
            self.release_connection(conn)
            if not conn.end_reported:
                self._report_call_end(event, cause)

    # ------------------------------------------------------------------ channels
    async def open_channel(self, name: str, registration: bytes = b"") -> int:
        """Open a channel and return its handle."""
        handle = self._next_handle()
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._open_waiters[handle] = fut
        try:
            await self._send_frame(MGMT_HANDLE, encode_open_channel(name, handle, registration))
            await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except TimeoutError as exc:
            self._open_waiters.pop(handle, None)
            self._open_handles.discard(handle)
            raise ViperTimeoutError(f"no ack opening channel {name}") from exc
        except ViperConnectionError:
            self._open_waiters.pop(handle, None)
            self._open_handles.discard(handle)
            raise
        return handle

    def _next_handle(self) -> int:
        """Return an unused handle and mark it in use."""
        while True:
            handle = next(self._handles) & 0xFFFF
            if handle != MGMT_HANDLE and handle not in self._open_handles:
                self._open_handles.add(handle)
                return handle

    def reserve_handle(self, handle: int) -> None:
        """Keep ``handle`` out of the pool, for a channel the panel uses without opening it."""
        self._open_handles.add(handle)

    async def close_channel(self, handle: int) -> None:
        """Close a channel."""
        self._binary_handlers.pop(handle, None)
        self._open_handles.discard(handle)
        with contextlib.suppress(ViperConnectionError):
            await self._send_frame(MGMT_HANDLE, encode_close_channel(handle))

    def set_binary_handler(self, handle: int, handler: Callable[[int, bytes], None] | None) -> None:
        """Route raw frames on ``handle`` to ``handler``."""
        if handler is None:
            self._binary_handlers.pop(handle, None)
        else:
            self._binary_handlers[handle] = handler

    def set_incoming_channel_handler(self, handler: Callable[[str, int], None] | None) -> None:
        """Set the callback for channels the panel opens towards us."""
        self._incoming_channel_handler = handler

    async def request(self, handle: int, message: str, **fields: object) -> dict:
        """Send a JSON request and wait for its response."""
        mid = next(self._msg_ids)
        obj: dict[str, object] = {"message": message, "message-type": "request", "message-id": mid}
        obj.update(fields)
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._json_waiters[(handle, mid)] = fut
        await self._send_frame(handle, json.dumps(obj, separators=(",", ":")).encode())
        try:
            return await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except TimeoutError as exc:
            self._json_waiters.pop((handle, mid), None)
            raise ViperTimeoutError(f"no response to {message}") from exc

    # ------------------------------------------------------------------ high level
    async def authenticate(self, user_token: str) -> None:
        """Authenticate with an app user token."""
        handle = await self.open_channel("UAUT")
        try:
            resp = await self.request(handle, "access", **{"user-token": user_token})
        finally:
            await self.close_channel(handle)
        code = resp.get("response-code")
        if code != 200:
            raise ViperAuthError(f"authentication failed: code={code} {resp.get('response-string') or resp}", code)

    async def get_configuration(self, addressbooks: str = "all") -> PanelConfig:
        """Fetch the configuration and remember our source address."""
        handle = await self.open_channel("UCFG")
        try:
            resp = await self.request(handle, "get-configuration", addressbooks=addressbooks)
        finally:
            await self.close_channel(handle)
        if resp.get("response-code") != 200:
            raise ViperError(f"get-configuration failed: {resp}")
        config = PanelConfig.from_response(resp)
        try:
            encode_logaddr(config.source)
        except ValueError as exc:
            raise ViperError(f"the panel's address {config.source!r} is not a ViP address") from exc
        self.config = config
        self.source = config.source
        return self.config

    async def server_info(self) -> dict:
        """Return model, firmware and capabilities from the INFO channel."""
        handle = await self.open_channel("INFO")
        try:
            return await self.request(handle, "server-info")
        finally:
            await self.close_channel(handle)

    async def start_ctp(self) -> None:
        """Open the CTPP channel and register for calls."""
        if self.source is None or self.config is None:
            raise ViperError("call get_configuration() first")
        self._ctpp_handle = await self.open_channel("CTPP", encode_logaddr(self.source))
        await self.open_channel("CSPB")
        await self._register()

    async def refresh_registration(self) -> None:
        """Register again on the connection already open.

        The registration is a lease of about an hour. When it lapses the panel
        stops delivering calls but keeps the socket open and answering.
        """
        if self._ctpp_handle is None:
            raise ViperError("CTPP channel is not open")
        await self._register()
        self.refreshes += 1

    async def _register(self) -> None:
        """Run one registration handshake."""
        if self.source is None or self.config is None:
            raise ViperError("call get_configuration() first")
        previous = self._registration
        conn = self._new_connection(destination=self.config.apt_address)
        body = struct.pack(">H", OP_REGISTER) + conn.local_id + encode_logaddr(self.source) + _REGISTER_TAIL
        try:
            await conn.send(body, FLAG_SYN)
            await conn.wait(opcode=OP_REGISTER_RESPONSE)
        except Exception:
            self.release_connection(conn)
            self.registered_at = None
            raise
        # Not before the answer, or the answer itself counts as a renewal.
        conn.persistent = True
        self._registration = conn
        with contextlib.suppress(ViperError):
            await conn.fin()
        if previous is not None and previous is not conn:
            self.release_connection(previous)
        self.registered_at = asyncio.get_running_loop().time()

    def _new_connection(self, *, destination: str, source: str | None = None) -> CtpConnection:
        while True:
            local = struct.pack(">H", secrets.randbelow(0x7FFE) + 1)
            peer = peer_connection_id(local)
            if peer not in self._connections and local not in self._connections:
                break
        conn = CtpConnection(self, local, peer, source=source or self.source or "", destination=destination)
        self._connections[peer] = conn
        return conn

    def release_connection(self, conn: CtpConnection) -> None:
        """Drop a finished connection, unless its id has been handed to another."""
        if self._connections.get(conn.peer_id) is conn:
            del self._connections[conn.peer_id]

    async def open_door(self, target: str, relay: int = 1, *, timeout: float = CTP_TIMEOUT) -> int:
        """Open ``relay`` on ``target``; return the release cause, 0 on success."""
        if not 0 <= relay <= 0xFF:
            raise ValueError("relay must fit in one byte")
        if self._ctpp_handle is None:
            raise ViperError("CTPP channel is not open")
        conn = self._new_connection(destination=target)
        try:
            body = struct.pack(">H", OP_OPEN_DOOR) + encode_logaddr(target) + bytes((relay,))
            await conn.send(body, FLAG_SYN)
            release = await conn.wait(opcode=OP_RELEASE, timeout=timeout)
            await conn.fin()
        finally:
            self.release_connection(conn)
        cause = release.release_cause
        if cause != 0:
            raise ViperError(f"panel rejected open-door: cause={cause}")
        return 0

    def new_call_connection(self, target: str) -> CtpConnection:
        """Create the connection for an outgoing call."""
        return self._new_connection(destination=target)
