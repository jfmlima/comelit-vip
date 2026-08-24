"""RTSP server that re-serves the panel's video.

Video only exists inside a call, so the first client starts one and the last
to leave ends it. Payload type, sequence and SSRC are rewritten so a client
survives a call restarting. Binds loopback by default; a DESCRIBE starts a
call on the panel.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets
import struct
from collections.abc import Awaitable, Callable, Sequence

from .call import VideoCall
from .rtp import NAL_PPS, NAL_SPS, RtpPacket
from .tasks import end_task

_LOGGER = logging.getLogger(__name__)

RTSP_VERSION = "RTSP/1.0"
SERVER_NAME = "comelit-vip"
PAYLOAD_TYPE = 96
CLOCK_RATE = 90000
DEFAULT_PATH = "comelit"
DEFAULT_HOST = "127.0.0.1"
EXPOSED_HOST = "0.0.0.0"
IDLE_GRACE = 2.0

MAX_LINE = 4096
MAX_HEADERS = 32
MAX_HEADER_BYTES = 8192
MAX_BODY = 8192
REQUEST_TIMEOUT = 15.0
# Loopback clients are exempt: the snapshot and clip ffmpeg pull from here.
MAX_REMOTE_CLIENTS = 4
MAX_WRITE_BUFFER = 1 << 20
# Timestamp gap between one call's last packet and the next call's first.
RESTART_GAP = CLOCK_RATE // 10

CallFactory = Callable[[str], Awaitable[VideoCall]]


class RelayBusyError(Exception):
    """A call is already up towards another entrance."""


class RtspError(Exception):
    """A request that will be answered with a status and then dropped."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"{code} {reason}")
        self.code = code
        self.reason = reason


class _Client:
    """One connected RTSP client."""

    def __init__(self, writer: asyncio.StreamWriter, session_id: str) -> None:
        self.writer = writer
        self.session_id = session_id
        self.interleaved: tuple[int, int] = (0, 1)
        self.playing = False
        self.gone = False
        self.sequence = secrets.randbelow(0x10000)
        self.ssrc = secrets.randbits(32)
        self.timestamp_offset: int | None = None
        self.last_output = 0
        self.generation = -1

    @property
    def local(self) -> bool:
        """Return whether this client is on the loopback interface."""
        peer = self.writer.get_extra_info("peername")
        return bool(peer) and str(peer[0]) in ("127.0.0.1", "::1")

    @property
    def backed_up(self) -> bool:
        """Return whether this client has stopped reading what it asked for."""
        transport = self.writer.transport
        return transport is not None and transport.get_write_buffer_size() > MAX_WRITE_BUFFER

    def rtp_frame(self, packet: RtpPacket, generation: int) -> bytes:
        """Rewrite a packet for this client and wrap it for interleaved TCP.

        Each call from the panel starts its timestamps from an unrelated base,
        so they are rebased onto the client's own clock across restarts.
        """
        if self.timestamp_offset is None:
            self.generation = generation
            self.timestamp_offset = packet.timestamp
        elif generation != self.generation:
            self.generation = generation
            self.timestamp_offset = (packet.timestamp - self.last_output - RESTART_GAP) & 0xFFFFFFFF
        timestamp = (packet.timestamp - self.timestamp_offset) & 0xFFFFFFFF
        self.last_output = timestamp
        header = struct.pack(
            ">BBHII",
            0x80,
            (0x80 if packet.marker else 0x00) | PAYLOAD_TYPE,
            self.sequence & 0xFFFF,
            timestamp,
            self.ssrc,
        )
        self.sequence = (self.sequence + 1) & 0xFFFF
        rtp = header + packet.payload
        return b"$" + bytes((self.interleaved[0],)) + struct.pack(">H", len(rtp)) + rtp


class _Episode:
    """One serving episode: from the first request to the last viewer gone."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.task: asyncio.Task | None = None
        self.call: VideoCall | None = None
        self.ready = asyncio.Event()
        self.failure: Exception | None = None


class RtspRelay:
    """Serve ``rtsp://<host>:<port>/<path>`` from on-demand ViP video calls."""

    def __init__(
        self,
        call_factory: CallFactory,
        *,
        targets: Sequence[str] = ("",),
        host: str = DEFAULT_HOST,
        port: int = 8554,
        path: str = DEFAULT_PATH,
        logger: logging.Logger | None = None,
    ) -> None:
        """Serve ``targets``: the first at the bare path, the rest at ``<path>/<address>``."""
        if not targets:
            raise ValueError("a relay needs at least one target")
        self._call_factory = call_factory
        self.targets = tuple(targets)
        self.host = host
        self.port = port
        self.path = path.strip("/")
        self.log = logger or _LOGGER
        self._server: asyncio.Server | None = None
        self._clients: set[_Client] = set()
        self._sessions: set[asyncio.Task] = set()
        self._episode: _Episode | None = None
        self._idle = asyncio.Event()
        self._lock = asyncio.Lock()
        # Parameter sets per entrance, for the SDP.
        self._sps: dict[str, bytes] = {}
        self._pps: dict[str, bytes] = {}
        self._generation = 0

    @property
    def viewers(self) -> int:
        """Return how many clients are playing."""
        return sum(1 for client in self._clients if client.playing)

    # ------------------------------------------------------------------ server
    async def start(self) -> str:
        """Bind the listener and return the stream URL."""
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self.log.info("RTSP relay listening on %s:%s/%s", self.host, self.port, self.path)
        return self.url()

    def url(self, host: str | None = None, target: str | None = None) -> str:
        """Return the URL for the default target or a named one."""
        path = self.path if target is None or target == self.targets[0] else f"{self.path}/{target}"
        return f"rtsp://{host or self.host}:{self.port}/{path}"

    @property
    def serving(self) -> str | None:
        """Return the entrance a live call is towards, if there is one."""
        episode = self._live()
        return episode.target if episode is not None else None

    async def stop(self) -> None:
        """Close the listener, drop clients and end any call.

        Clients are closed before ``wait_closed``, which blocks while any
        handler still holds a transport.
        """
        if self._server is not None:
            self._server.close()
            # Reaches connections whose handler task has not started yet.
            self._server.close_clients()
        sessions = [task for task in self._sessions if task is not asyncio.current_task()]
        self._sessions.clear()
        for task in sessions:
            await end_task(task)
        for client in list(self._clients):
            with contextlib.suppress(Exception):
                client.writer.close()
        self._clients.clear()
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        await self.abandon_call()

    # ------------------------------------------------------------------ media
    #
    # One task owns each call: `_serve` dials, pumps, redials when the panel
    # hangs up, and closes. Requests only start or join an episode; `_drop`
    # only reports that a client left.
    async def _ensure_call(self, target: str | None = None) -> None:
        """Start or join the episode towards ``target``; raise if it cannot serve.

        The panel allows one call, so a live episode towards another entrance
        raises ``RelayBusyError`` rather than being joined.
        """
        target = self.targets[0] if target is None else target
        async with self._lock:
            episode = self._live()
            if episode is not None and episode.target != target:
                raise RelayBusyError(f"a call towards {episode.target} is up; {target} must wait")
            if episode is None:
                episode = _Episode(target)
                episode.task = asyncio.create_task(self._serve(episode), name="comelit_vip.rtsp_call")
                self._episode = episode
        await episode.ready.wait()
        if episode.failure is not None:
            raise episode.failure

    def _live(self) -> _Episode | None:
        """Return the episode being served, if its owner is still running."""
        episode = self._episode
        if episode is None or episode.task is None or episode.task.done():
            return None
        return episode

    async def abandon_call(self) -> None:
        """End the episode, however far along it is."""
        episode = self._episode
        if episode is not None and episode.task is not None:
            await end_task(episode.task)

    async def _serve(self, episode: _Episode) -> None:
        """Own the episode's calls, from the first dial to the last viewer gone."""
        dialled = False
        try:
            while True:
                self.log.debug("starting video call towards %s for %d client(s)", episode.target, len(self._clients))
                try:
                    call = await self._call_factory(episode.target)
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    if not dialled:
                        episode.failure = err
                    else:
                        self.log.warning("could not restart the video call towards %s: %s", episode.target, err)
                    return
                finally:
                    dialled = True
                    episode.ready.set()
                episode.call = call
                self._generation += 1
                left = await self._relay_call(call, episode.target)
                episode.call = None
                with contextlib.suppress(Exception):
                    await call.close()
                if left or self.viewers == 0:
                    return
                self.log.debug("video call ended; restarting for %d viewer(s)", self.viewers)
        finally:
            episode.ready.set()
            if episode.call is not None:
                with contextlib.suppress(Exception):
                    await episode.call.close()
                episode.call = None
            if self._episode is episode:
                self._episode = None

    async def _relay_call(self, call: VideoCall, target: str) -> bool:
        """Relay one call until it ends or the clients leave.

        Returns whether the clients left; the panel ending the call otherwise.
        """
        pump = asyncio.create_task(self._pump(call, target), name="comelit_vip.rtsp_pump")
        idle = asyncio.create_task(self._wait_idle(), name="comelit_vip.rtsp_idle")
        try:
            done, _ = await asyncio.wait({pump, idle}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancel both before awaiting either: awaiting the first can raise
            # this task's own cancellation, and the second must not outlive it.
            pump.cancel()
            idle.cancel()
            await end_task(pump)
            await end_task(idle)
        return idle in done

    async def _pump(self, call: VideoCall, target: str) -> None:
        """Move packets to the playing clients until the call ends."""
        try:
            async for packet in call.packets():
                self._remember_parameter_sets(target, packet)
                self._fan_out(packet)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("video relay failed")

    async def _wait_idle(self) -> None:
        """Return once the clients have been gone for ``IDLE_GRACE``."""
        while True:
            self._idle.clear()
            if self._clients:
                await self._idle.wait()
            await asyncio.sleep(IDLE_GRACE)
            if self.viewers == 0:
                return

    def _fan_out(self, packet: RtpPacket) -> None:
        """Write one packet to every playing client, dropping any that fail."""
        dead = []
        for client in self._clients:
            if not client.playing:
                continue
            if client.backed_up:
                self.log.warning("an RTSP client stopped reading; dropping it")
                dead.append(client)

                continue
            try:
                client.writer.write(client.rtp_frame(packet, self._generation))
            except Exception:
                dead.append(client)
        for client in dead:
            self._drop(client)

    def _remember_parameter_sets(self, target: str, packet: RtpPacket) -> None:
        payload = packet.payload
        if not payload:
            return
        kind = payload[0] & 0x1F
        if kind == NAL_SPS:
            self._sps[target] = payload
        elif kind == NAL_PPS:
            self._pps[target] = payload

    # ------------------------------------------------------------------ RTSP
    def _sdp(self, target: str) -> str:
        lines = [
            "v=0",
            "o=- 0 0 IN IP4 0.0.0.0",
            "s=Comelit ViP entrance",
            "c=IN IP4 0.0.0.0",
            "t=0 0",
            f"m=video 0 RTP/AVP {PAYLOAD_TYPE}",
            f"a=rtpmap:{PAYLOAD_TYPE} H264/{CLOCK_RATE}",
        ]
        fmtp = f"a=fmtp:{PAYLOAD_TYPE} packetization-mode=1"
        sps, pps = self._sps.get(target), self._pps.get(target)
        if sps and pps:
            fmtp += f";profile-level-id={sps[1:4].hex()};sprop-parameter-sets={_sdp_escape(sps)},{_sdp_escape(pps)}"
        lines.append(fmtp)
        lines.append("a=control:trackID=0")
        return "\r\n".join(lines) + "\r\n"

    def _drop(self, client: _Client) -> None:
        self._clients.discard(client)
        client.playing = False
        client.gone = True
        with contextlib.suppress(Exception):
            client.writer.close()
        if self.viewers == 0:
            self._idle.set()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client = _Client(writer, secrets.token_hex(4))
        if not self._admit(client):
            self.log.warning("refusing an RTSP client from %s: too many already connected", peer)
            with contextlib.suppress(Exception):
                await self._reply(writer, 503, "Service Unavailable", "0")
            writer.close()
            return
        task = asyncio.current_task()
        if task is not None:
            self._sessions.add(task)
        self._clients.add(client)
        self.log.debug("RTSP client connected: %s", peer)
        try:
            while True:
                request = await self._read_request(reader)
                if request is None:
                    break
                await self._handle_request(client, request, writer)
                if client.gone:
                    break
        except RtspError as err:
            self.log.debug("bad RTSP request from %s: %s", peer, err)
            with contextlib.suppress(Exception):
                await self._reply(writer, err.code, err.reason, "0")
        except TimeoutError, asyncio.IncompleteReadError, ConnectionResetError, OSError, ValueError:
            pass
        finally:
            self.log.debug("RTSP client gone: %s", peer)
            self._drop(client)
            if task is not None:
                self._sessions.discard(task)

    def _admit(self, client: _Client) -> bool:
        """Return whether there is room for one more client."""
        if client.local:
            return True
        return sum(1 for other in self._clients if not other.local) < MAX_REMOTE_CLIENTS

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]] | None:
        """Read one request.

        No deadline on the first line, since a playing client is silent for
        minutes; a deadline on the rest.
        """
        line = await reader.readline()
        if not line:
            return None
        if len(line) > MAX_LINE:
            raise RtspError(414, "Request-URI Too Long")
        parts = line.decode("ascii", "replace").strip().split()
        if len(parts) < 2:
            return None
        method, url = parts[0], parts[1]
        headers: dict[str, str] = {}
        async with asyncio.timeout(REQUEST_TIMEOUT):
            size = 0
            while True:
                header = await reader.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
                size += len(header)
                if len(headers) >= MAX_HEADERS or size > MAX_HEADER_BYTES:
                    raise RtspError(431, "Request Header Fields Too Large")
                name, _, value = header.decode("ascii", "replace").partition(":")
                headers[name.strip().lower()] = value.strip()
            length = _content_length(headers)
            if length:
                await reader.readexactly(length)
        return method.upper(), url, headers

    async def _reply(
        self,
        writer: asyncio.StreamWriter,
        code: int,
        reason: str,
        cseq: str,
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        lines = [f"{RTSP_VERSION} {code} {reason}", f"CSeq: {cseq}", f"Server: {SERVER_NAME}"]
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n" + body).encode())
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()

    async def _handle_request(
        self, client: _Client, request: tuple[str, str, dict[str, str]], writer: asyncio.StreamWriter
    ) -> None:
        method, url, headers = request
        cseq = headers.get("cseq", "0")
        target = self._target_of(url)
        if target is None:
            await self._reply(writer, 404, "Not Found", cseq)
            return
        if method == "OPTIONS":
            await self._reply(writer, 200, "OK", cseq, {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"})
        elif method == "DESCRIBE":
            if not await self._try_call(writer, cseq, target):
                return
            await self._reply(
                writer,
                200,
                "OK",
                cseq,
                {"Content-Type": "application/sdp", "Content-Base": url.rstrip("/") + "/"},
                self._sdp(target),
            )
        elif method == "SETUP":
            transport = headers.get("transport", "")
            if "tcp" not in transport.lower():
                await self._reply(writer, 461, "Unsupported Transport", cseq)
                return
            client.interleaved = _interleaved(transport)
            await self._reply(
                writer,
                200,
                "OK",
                cseq,
                {
                    "Transport": f"RTP/AVP/TCP;unicast;interleaved={client.interleaved[0]}-{client.interleaved[1]}",
                    "Session": client.session_id,
                },
            )
        elif method == "PLAY":
            if not await self._try_call(writer, cseq, target):
                return
            client.playing = True
            await self._reply(
                writer,
                200,
                "OK",
                cseq,
                {"Session": client.session_id, "RTP-Info": f"url={url};seq={client.sequence};rtptime=0"},
            )
        elif method in ("TEARDOWN", "GET_PARAMETER", "SET_PARAMETER"):
            await self._reply(writer, 200, "OK", cseq, {"Session": client.session_id})
            if method == "TEARDOWN":
                self._drop(client)
        else:
            await self._reply(writer, 405, "Method Not Allowed", cseq)

    def _target_of(self, url: str) -> str | None:
        """Return the entrance a request URL names, or None.

        A ``trackID=`` suffix may follow the path, since the SDP advertises
        ``a=control:trackID=0``.
        """
        if url == "*":
            return self.targets[0]
        path = _url_path(url).strip("/")
        if path != self.path and not path.startswith(f"{self.path}/"):
            return None
        rest = [part for part in path[len(self.path) :].split("/") if part and not part.startswith("trackID=")]
        if not rest:
            return self.targets[0]
        if len(rest) == 1 and rest[0] in self.targets:
            return rest[0]
        return None

    async def _try_call(self, writer: asyncio.StreamWriter, cseq: str, target: str) -> bool:
        """Start a call, answering 503 if it cannot."""
        try:
            await self._ensure_call(target)
        except RelayBusyError as err:
            self.log.info("refusing an RTSP client: %s", err)
            await self._reply(writer, 503, "Service Unavailable", cseq)
            return False
        except Exception as err:
            self.log.warning("cannot start a video call: %s", err)
            await self._reply(writer, 503, "Service Unavailable", cseq)
            return False
        return True


def _url_path(url: str) -> str:
    """Return the path of an RTSP URL, which may also arrive bare."""
    rest = url.split("://", 1)[1] if "://" in url else url
    if "://" in url:
        _, _, rest = rest.partition("/")
        rest = "/" + rest
    for cut in ("?", "#"):
        rest = rest.split(cut, 1)[0]
    return rest


def _content_length(headers: dict[str, str]) -> int:
    """Return a body length this server is willing to read."""
    raw = headers.get("content-length", "0") or "0"
    try:
        length = int(raw)
    except ValueError as err:
        raise RtspError(400, "Bad Request") from err
    if length < 0 or length > MAX_BODY:
        raise RtspError(413, "Request Entity Too Large")
    return length


def _interleaved(transport: str) -> tuple[int, int]:
    """Return the interleaved channel pair from a Transport header, or the default pair."""
    for part in transport.split(";"):
        if not part.strip().startswith("interleaved="):
            continue
        values = part.split("=", 1)[1].split("-")
        try:
            first = int(values[0])
            second = int(values[1]) if len(values) > 1 else first + 1
        except ValueError:
            break
        if 0 <= first <= 255 and 0 <= second <= 255 and first != second:
            return first, second
        break
    return 0, 1


def _sdp_escape(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
