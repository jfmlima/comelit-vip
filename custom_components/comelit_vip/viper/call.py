"""Outgoing video call towards an entrance panel.

Setup sequence on the CTPP channel:

    -> 0x0001 INVITE towards the entrance panel
    <- panel answers, we adopt its sequence
    -> open UDPM
    -> 0x0003 capabilities
    <- 0x0003 then 0x000C, call accepted
    -> open two RTPC channels, audio and video
    -> 0x0011 audio media request
    <- 0x0011 naming the handle it wants our audio on
    -> 0x0011 video media request with the settings block

Media arrives as RTP on the media handles, inside the same TCP connection.
Closing sends 0x000E.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import struct
from collections.abc import AsyncIterator

from .ctp import (
    FLAG_DATA,
    FLAG_SYN,
    OP_CAPABILITIES,
    OP_MEDIA_REQUEST,
    OP_RELEASE,
    OP_SETUP_ACK,
    encode_logaddr,
)
from .rtp import RtpPacket, parse_rtp_packet
from .session import CtpConnection, ViperError, ViperSession, ViperTimeoutError
from .tasks import end_task

_LOGGER = logging.getLogger(__name__)

_CAPABILITIES = bytes.fromhex("00 03 49 00 27 00 00 00".replace(" ", ""))
_AUDIO_OFFER = bytes.fromhex("00 11 18 02 00 00 00 00".replace(" ", ""))
_VIDEO_OFFER = bytes.fromhex("00 11 14 32 00 00 00 00".replace(" ", ""))

# The panel rounds the request to a mode it has; a 6741W offers 320x240 and 640x240.
SD_RESOLUTION = (320, 240)
HD_RESOLUTION = (800, 480)
MAX_RESOLUTION = (800, 480)
HD_BITRATE = 1000
VIDEO_FPS = 16
MAX_PAYLOAD = 0xFFFF

VIDEO_QUEUE_SIZE = 512
# A 6741W sends about 36 s of video per media request, about 60 s per call if
# the request is repeated, and then nothing, with no RELEASE.
MEDIA_REFRESH = 15.0
MEDIA_START_TIMEOUT = 10.0
MEDIA_SILENCE = 2.0
_MEDIA_POLL = 0.2


class VideoCall:
    """A receive-only video call; open it, then iterate :meth:`packets`."""

    def __init__(
        self,
        session: ViperSession,
        target: str,
        *,
        hd: bool = False,
        bitrate: int | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.session = session
        self.target = target
        self.log = logger or _LOGGER
        self.resolution = HD_RESOLUTION if hd else SD_RESOLUTION
        self.bitrate = (HD_BITRATE if hd else 0) if bitrate is None else bitrate
        self.conn: CtpConnection | None = None
        self.udp_handle: int | None = None
        self.audio_handle: int | None = None
        self.video_handle: int | None = None
        self.media_handle: int | None = None
        self.remote_media_handle: int | None = None
        self._video_q: asyncio.Queue[RtpPacket | None] = asyncio.Queue(maxsize=VIDEO_QUEUE_SIZE)
        self._video_handles: set[int] = set()
        self._closed = False
        self._watcher: asyncio.Task | None = None
        self._media_watcher: asyncio.Task | None = None
        self._refresher: asyncio.Task | None = None
        self._last_media: float | None = None
        # What the panel still has to be told, if a close was cut short.
        self._release_conn: CtpConnection | None = None
        self._pending_handles: list[int] = []

    # ------------------------------------------------------------------ setup
    async def open(self, timeout: float = 20.0) -> None:
        """Run the call setup; raise if the panel refuses or does not answer."""
        if self.conn is not None:
            return
        source = self.session.source
        if source is None:
            raise ViperError("session has no source address; call get_configuration()")

        self.session.set_incoming_channel_handler(self._on_incoming_channel)
        conn = self.session.new_call_connection(self.target)
        self.conn = conn
        try:
            call_id = secrets.token_bytes(4)
            conn.call_id = call_id  # the panel echoes the INVITE back
            invite = (
                struct.pack(">H", 0x0001)
                + encode_logaddr(source)
                + encode_logaddr(self.target)
                + b"\x01\x20"
                + call_id
                + encode_logaddr(source)
                + b"II"
            )
            await conn.send(invite, FLAG_SYN)
            initial = await conn.wait(timeout=timeout)
            conn.acknowledgement = initial.sequence

            self.udp_handle = await self.session.open_channel("UDPM")
            await conn.send(_CAPABILITIES, FLAG_DATA)
            await conn.wait(opcode=OP_CAPABILITIES, timeout=timeout)
            await conn.wait(opcode=OP_SETUP_ACK, timeout=timeout)

            self.audio_handle = await self.session.open_channel("RTPC")
            self.video_handle = await self.session.open_channel("RTPC")
            # The panel sends video on the handle after the one offered.
            self.media_handle = (self.video_handle + 1) & 0xFFFF
            self.session.reserve_handle(self.media_handle)
            self._video_handles = {self.video_handle, self.media_handle}

            await conn.send(_AUDIO_OFFER + struct.pack("<H", self.audio_handle) + b"\x00\x00", FLAG_DATA)
            with contextlib.suppress(ViperTimeoutError):
                await conn.wait(opcode=OP_MEDIA_REQUEST, timeout=timeout)

            await self._request_video()

            for handle in (self.audio_handle, self.video_handle, self.media_handle):
                self.session.set_binary_handler(handle, self._on_media)
            self._watcher = asyncio.create_task(self._watch_release(), name="comelit_vip.call_watch")
            self._media_watcher = asyncio.create_task(self._watch_media(), name="comelit_vip.call_media")
            self._refresher = asyncio.create_task(self._refresh_video(), name="comelit_vip.call_refresh")
        except BaseException:
            await self.close()
            raise

    def _on_incoming_channel(self, name: str, handle: int) -> None:
        """Route a media channel the panel opens towards us."""
        if name != "RTPC":
            return
        self.remote_media_handle = handle
        self._video_handles.add(handle)
        self.session.set_binary_handler(handle, self._on_media)

    def _on_media(self, handle: int, payload: bytes) -> None:
        if handle not in self._video_handles:
            return
        try:
            packet = parse_rtp_packet(payload)
        except ValueError:
            return
        self._last_media = asyncio.get_running_loop().time()
        if self._video_q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._video_q.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._video_q.put_nowait(packet)

    async def _watch_release(self) -> None:
        """Finish when the panel releases the call."""
        conn = self.conn
        if conn is None:
            return
        try:
            while True:
                packet = await conn.wait(timeout=3600)
                if packet.opcode == OP_RELEASE or packet.is_fin:
                    self.log.debug("panel released the video call")
                    break
        except ViperTimeoutError, ViperError:
            return
        except asyncio.CancelledError:
            raise
        finally:
            self._signal_end()

    async def _request_video(self) -> None:
        if self.conn is None or self.video_handle is None:
            return
        await self.conn.send(
            _VIDEO_OFFER + struct.pack("<H", self.video_handle) + _video_settings(self.resolution, self.bitrate),
            FLAG_DATA,
        )

    async def _refresh_video(self) -> None:
        """Repeat the video request before the panel's media lease runs out."""
        while True:
            await asyncio.sleep(MEDIA_REFRESH)
            try:
                await self._request_video()
            except ViperError:
                return

    async def _watch_media(self) -> None:
        """Finish when the panel stops sending video, which it does without a RELEASE."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            await asyncio.sleep(_MEDIA_POLL)
            now = loop.time()
            if self._last_media is None:
                if now - started > MEDIA_START_TIMEOUT:
                    break
            elif now - self._last_media > MEDIA_SILENCE:
                break
        self.log.debug("no video from %s; treating the call as over", self.target)
        self._signal_end()

    # ------------------------------------------------------------------ media
    async def packets(self) -> AsyncIterator[RtpPacket]:
        """Yield video packets until the call ends."""
        while True:
            packet = await self._video_q.get()
            if packet is None:
                return
            yield packet

    def _signal_end(self) -> None:
        """Queue the end-of-stream sentinel, making room for it if the queue is full."""
        if self._video_q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._video_q.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._video_q.put_nowait(None)

    # ------------------------------------------------------------------ teardown
    async def close(self) -> None:
        """Release the call and close its channels.

        Local state goes first. The sends to the panel come after, and a close
        cut short by a cancellation finishes them on the next call.
        """
        if not self._closed:
            self._closed = True
            self.session.set_incoming_channel_handler(None)
            conn, self.conn = self.conn, None
            handles = [
                handle
                for handle in (
                    self.audio_handle,
                    self.video_handle,
                    self.media_handle,
                    self.remote_media_handle,
                    self.udp_handle,
                )
                if handle is not None
            ]
            self.audio_handle = self.video_handle = self.media_handle = None
            self.remote_media_handle = self.udp_handle = None
            for handle in handles:
                self.session.set_binary_handler(handle, None)
            if conn is not None:
                self.session.release_connection(conn)
            self._release_conn = conn
            self._pending_handles = handles
            self._signal_end()
        for task in (self._watcher, self._media_watcher, self._refresher):
            if task is not None:
                await end_task(task)
        self._watcher = self._media_watcher = self._refresher = None
        if self._release_conn is not None:
            with contextlib.suppress(ViperError, OSError, ConnectionError):
                await self._release_conn.send(struct.pack(">HB", OP_RELEASE, 0), FLAG_DATA)
            self._release_conn = None
        while self._pending_handles:
            with contextlib.suppress(ViperError, OSError, ConnectionError):
                await self.session.close_channel(self._pending_handles[0])
            self._pending_handles.pop(0)

    async def __aenter__(self) -> VideoCall:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def _video_settings(resolution: tuple[int, int], bitrate: int) -> bytes:
    """Pack the settings block of a video media offer: the largest accepted picture, then the requested one."""
    width, height = resolution
    limit_width, limit_height = MAX_RESOLUTION
    return struct.pack("<HIHHHHBB", MAX_PAYLOAD, bitrate & 0xFFFFFFFF, limit_width, limit_height, width, height, VIDEO_FPS, 0)
