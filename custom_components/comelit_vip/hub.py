"""The runtime object behind a config entry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from homeassistant.core import HomeAssistant, callback

from .const import (
    AUTH_FAILURES_BEFORE_REAUTH,
    BUS_EVENT,
    CONNECTION_MAX_AGE,
    DEFAULT_RECORD_DIRNAME,
    DEFAULT_RECORD_SECONDS,
    DEFAULT_WEB_PORT,
    DOMAIN,
    EVENT_CALL_ENDED,
    EVENT_INTERNAL_CALL,
    EVENT_RING,
    KEEPALIVE_INTERVAL,
    MANUFACTURER,
    RECONNECT_BACKOFF,
    REGISTRATION_REFRESH,
    STABLE_CONNECTION,
    WATCHDOG_INTERVAL,
)
from .viper import DeviceInfo, PanelConfig, RingEvent, VideoCall, ViperSession, async_discover
from .viper.ctp import TAG_ENTRANCE, TAG_FLOOR_CALL
from .viper.rtsp import DEFAULT_HOST, EXPOSED_HOST, RtspRelay
from .viper.session import ViperAuthError, ViperError
from .viper.tasks import end_task

_LOGGER = logging.getLogger(__name__)

# The first key frame arrives two to three seconds after the stream opens.
SNAPSHOT_TIMEOUT = 45.0
RECORD_GRACE = 30.0
TERMINATE_GRACE = 5.0


def default_record_path(hass: HomeAssistant) -> str:
    """Return the default clip folder, inside Home Assistant's media folder."""
    media = hass.config.media_dirs.get("local") or hass.config.path("media")
    return str(Path(media) / DEFAULT_RECORD_DIRNAME)


class ComelitVipHub:
    """Owns the session, the RTSP relay and the reconnect logic."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        unique_id: str | None = None,
        host: str,
        token: str,
        port: int,
        rtsp_port: int,
        rtsp_host: str | None = None,
        expose_stream: bool = False,
        web_port: int = DEFAULT_WEB_PORT,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.unique_id = unique_id or entry_id
        self.host = host
        self.token = token
        self.port = port
        self.web_port = web_port
        self.rtsp_port = rtsp_port
        self.rtsp_host = rtsp_host
        self.expose_stream = expose_stream
        self.session = ViperSession(host, port, logger=logging.getLogger(f"{__package__}.viper"))
        self.relay: RtspRelay | None = None
        self.config: PanelConfig | None = None
        self.device: DeviceInfo | None = None
        self.last_ring: datetime | None = None
        self.snapshots: dict[str, bytes] = {}
        self.snapshots_at: dict[str, datetime] = {}
        self.snapshot_on_ring = False
        self.record_on_ring = False
        self.record_seconds = DEFAULT_RECORD_SECONDS
        self.record_path = default_record_path(hass)
        self.local_ip: str | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._clip_number = 0
        self._attempt = 0
        self._auth_failures = 0
        self._captures_owed: dict[bytes, str] = {}
        self._connected_at: float | None = None
        self._listeners: list[Callable[[str, dict], None]] = []
        self._supervisor: asyncio.Task | None = None
        self._changed = asyncio.Event()
        self._immediate = False
        self._replacing = False
        self._stopping = False

    @property
    def available(self) -> bool:
        """Return whether connected and registered.

        The panel keeps answering probes on a connection it has stopped
        delivering calls over, so connected alone is not enough. A connection
        being replaced on purpose counts as up until the replacement fails.
        """
        return (self.session.connected and self.session.registered) or self._replacing

    # ------------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        """Connect, register for calls, and start the relay."""
        await self._connect()
        entrances = [address for _name, address in self.config.entrances] if self.config is not None else []
        if not entrances:
            _LOGGER.info("no entrance panel in the configuration, so there is no video to relay")
        else:
            relay = RtspRelay(
                self._start_call,
                targets=entrances,
                host=EXPOSED_HOST if self.expose_stream else DEFAULT_HOST,
                port=self.rtsp_port,
                logger=logging.getLogger(f"{__package__}.rtsp"),
            )
            try:
                await relay.start()
            except OSError as err:
                _LOGGER.error("no video: the RTSP relay could not listen on port %s (%s)", self.rtsp_port, err)
            else:
                self.relay = relay
        self._supervisor = asyncio.create_task(self._supervise(), name="comelit_vip.link")

    async def async_shutdown(self) -> None:
        """Stop everything this entry owns."""
        self._stopping = True
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            await end_task(supervisor)
        await self._stop_processes()
        if self.relay is not None:
            await self.relay.stop()
            self.relay = None
        await self.session.close()

    async def _connect(self) -> None:
        """Authenticate, read the configuration, register for calls."""
        self.session.on_ring = self._on_ring
        self.session.on_call_end = self._on_call_end
        self.session.on_disconnect = self._on_disconnect
        await self.session.connect()
        discovery = asyncio.create_task(async_discover(self.host)) if self.device is None else None
        try:
            await self.session.authenticate(self.token)
            self.config = await self.session.get_configuration("all")
            await self.session.start_ctp()
        except BaseException:
            if discovery is not None:
                await end_task(discovery)
            await self.session.close()
            raise
        if discovery is not None:
            self.device = await discovery
        self.local_ip = self.session.local_address
        self._connected_at = asyncio.get_running_loop().time()
        self._auth_failures = 0
        self._replacing = False
        self._notify("connection", {"available": self.available})
        _LOGGER.info(
            "connected to %s (%s) as %s",
            self.host,
            self.device.model if self.device else "unknown model",
            self.config.source if self.config else "?",
        )

    @callback
    def _on_disconnect(self, error: Exception | None) -> None:
        """Handle the session reporting a connection that died on its own."""
        if self._stopping:
            return
        _LOGGER.warning("connection to %s lost: %s", self.host, error)
        self._lower_link()

    @callback
    def _lower_link(self, *, immediate: bool = False, quiet: bool = False) -> None:
        """Take the one transition to a down link and wake the supervisor.

        A quiet transition is a planned replacement: the entities hear of it
        only if the new connection fails.
        """
        self._replacing = quiet
        if not quiet:
            self._notify("connection", {"available": False})
        # A connection that dies at once usually means another client holds
        # this user; keep the backoff climbing.
        if self._connected_at is not None:
            lifetime = asyncio.get_running_loop().time() - self._connected_at
            if lifetime >= STABLE_CONNECTION:
                self._attempt = 0
            else:
                _LOGGER.debug("connection lasted only %.1fs; keeping the longer backoff", lifetime)
        self._connected_at = None
        self._immediate = immediate
        self._changed.set()

    @callback
    def _report_down(self) -> None:
        """Tell the entities about a replacement that did not come up."""
        if self._replacing:
            self._replacing = False
            self._notify("connection", {"available": False})

    async def _supervise(self) -> None:
        """Keep the link up. This is the only task that builds a connection."""
        while not self._stopping:
            try:
                if self.session.connected:
                    await self._wait_for_change(WATCHDOG_INTERVAL)
                    if self._stopping:
                        return
                    await self._watchdog_pass()
                elif not await self._raise_link():
                    return
            except Exception:
                _LOGGER.exception("the link supervisor failed a pass")

    async def _wait_for_change(self, timeout: float) -> None:
        """Sleep until the link changes or the timeout runs out."""
        self._changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)

    async def _raise_link(self) -> bool:
        """Bring the link up once, after the backoff owed.

        Returns False when reauthentication was started and the supervisor
        should stop.
        """
        # Advance the backoff before the first await, so an exception below
        # cannot leave it where it was.
        if self._immediate:
            delay = 0.0
            self._immediate = False
            self._attempt = 0
        else:
            delay = RECONNECT_BACKOFF[min(self._attempt, len(RECONNECT_BACKOFF) - 1)]
            self._attempt += 1
            _LOGGER.debug("reconnecting to %s in %ss (attempt %d)", self.host, delay, self._attempt)
        # The session does not own the video call; the relay does.
        if self.relay is not None:
            with contextlib.suppress(Exception):
                await self.relay.abandon_call()
        if delay:
            await asyncio.sleep(delay)
        if self._stopping:
            return True
        try:
            await self._connect()
        except ViperAuthError as err:
            self._report_down()
            # The panel's response code for a slot another client holds, as
            # against a bad token, is not known. Setup still reauths at once.
            self._auth_failures += 1
            if self._auth_failures < AUTH_FAILURES_BEFORE_REAUTH:
                _LOGGER.warning(
                    "%s refused the token (response code %s), %d of %d before a new one is asked for",
                    self.host,
                    err.code,
                    self._auth_failures,
                    AUTH_FAILURES_BEFORE_REAUTH,
                )
                return True
            _LOGGER.error(
                "token rejected by %s %d times (response code %s); asking for a new one",
                self.host,
                self._auth_failures,
                err.code,
            )
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            if entry is not None:
                entry.async_start_reauth(self.hass)
            return False
        except (ViperError, OSError) as err:
            self._report_down()
            _LOGGER.debug("reconnect attempt %d failed: %s", self._attempt, err)
            return True
        if not self.available:
            # The socket died during the handshake.
            _LOGGER.warning("the connection to %s did not survive being set up", self.host)
            await self.session.close()
            self._connected_at = None
            self._notify("connection", {"available": False})
        return True

    async def _watchdog_pass(self) -> None:
        """Renew, probe, recycle: one pass."""
        if not self.session.connected:
            return
        now = asyncio.get_running_loop().time()
        if not await self._keep_registered():
            return
        if not await self._probe_if_quiet(now):
            return
        await self._recycle_if_stale(now)

    async def _keep_registered(self) -> bool:
        """Renew the lease when it is old. Returns whether the link survived."""
        age = self.session.registration_age
        if age is None:
            _LOGGER.warning("connected to %s but not registered; reconnecting", self.host)
            await self._drop_connection()
            return False
        if age < REGISTRATION_REFRESH:
            return True
        try:
            await self.session.refresh_registration()
        except (ViperError, OSError) as err:
            _LOGGER.warning("could not renew the registration with %s (%s); reconnecting", self.host, err)
            await self._drop_connection()
            return False
        _LOGGER.debug("renewed the registration with %s after %.0fs", self.host, age)
        return True

    async def _probe_if_quiet(self, now: float) -> bool:
        """Probe the panel if it has been silent for too long."""
        if now - self.session.last_rx < KEEPALIVE_INTERVAL:
            return True
        try:
            await self.session.server_info()
        except (ViperError, OSError) as err:
            _LOGGER.warning("%s stopped answering (%s); reconnecting", self.host, err)
            await self._drop_connection()
            return False
        return True

    async def _recycle_if_stale(self, now: float) -> None:
        """Replace a connection old enough for its lease to be in doubt.

        It is not confirmed that a renewal moves the panel's lease; a fresh
        connection does.
        """
        if self._connected_at is None or now - self._connected_at < CONNECTION_MAX_AGE:
            return
        if self._calls_in_progress():
            _LOGGER.debug("connection to %s is due for recycling but a call is up", self.host)
            return
        _LOGGER.info("taking a fresh registration lease from %s", self.host)
        await self.session.close()
        self._lower_link(immediate=True, quiet=True)

    def _calls_in_progress(self) -> bool:
        """Return whether anything would notice the connection going away."""
        return self.session.calls_active or (self.relay is not None and self.relay.viewers > 0)

    async def _drop_connection(self) -> None:
        """Close the connection and report it down. The caller logs why."""
        await self.session.close()
        if not self._stopping:
            self._lower_link()

    # ------------------------------------------------------------------ events
    def add_listener(self, listener: Callable[[str, dict], None]) -> Callable[[], None]:
        """Subscribe an entity callback. Returns the unsubscribe function."""
        self._listeners.append(listener)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return _remove

    def _notify(self, kind: str, data: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, data)
            except Exception:
                _LOGGER.exception("listener failed for %s", kind)

    @callback
    def _on_ring(self, event: RingEvent) -> None:
        """Report an incoming call."""
        entrance = self._is_entrance_call(event)
        if entrance:
            self.last_ring = event.received_at
        data = {
            "type": EVENT_RING if entrance else EVENT_INTERNAL_CALL,
            "caller": event.caller,
            "callee": event.callee,
            "origin": event.origin,
            "timestamp": event.received_at.isoformat(),
        }
        if event.tag:
            data["kind"] = event.tag.decode("ascii", "replace")
        if entrance and self.config is not None:
            data["entrance"] = self.config.entrance_name(event.origin)
        if event.body:
            data["detail"] = event.body.hex()
        self._notify("event", data)
        self.hass.bus.async_fire(BUS_EVENT, {"entry_id": self.entry_id, **data})
        # The panel refuses an outbound call during a ring (RELEASE cause 8).
        if entrance and (self.snapshot_on_ring or self.record_on_ring):
            self._captures_owed[event.connection] = event.origin

    def _is_entrance_call(self, event: RingEvent) -> bool:
        """Return whether this call is from an entrance panel.

        A floor call names the entrance panel as its origin too, so only the
        tag tells them apart; the address book is the fallback for an unknown
        tag.
        """
        if event.tag == TAG_FLOOR_CALL:
            return False
        if event.tag == TAG_ENTRANCE:
            return True
        _LOGGER.debug("call with an unknown kind tag %r; falling back to the address book", event.tag)
        return self.config is not None and self.config.is_entrance(event.origin)

    @callback
    def _on_call_end(self, event: RingEvent, cause: int | None) -> None:
        """Report the end of a call."""
        data = {
            "type": EVENT_CALL_ENDED,
            "caller": event.caller,
            "origin": event.origin,
            "cause": cause,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._notify("event", data)
        self.hass.bus.async_fire(BUS_EVENT, {"entry_id": self.entry_id, **data})
        target = self._captures_owed.pop(event.connection, None)
        if target is None:
            return
        if self.snapshot_on_ring:
            self.hass.async_create_task(self._snapshot_after_ring(target), "comelit_vip.snapshot")
        if self.record_on_ring:
            self.hass.async_create_task(self.async_record_clip(target=target), "comelit_vip.record")

    async def _snapshot_after_ring(self, target: str) -> None:
        """Take the picture owed after a ring; nobody is waiting, so log rather than raise."""
        try:
            await self.async_capture_snapshot(target)
        except ViperError as err:
            _LOGGER.error("could not take a picture of %s after the ring: %s", target, err)

    # ------------------------------------------------------------------ actions
    async def async_open(self, address: str, output_index: int) -> None:
        """Fire a door release or an actuator."""
        if not self.available:
            raise ViperError("not connected to the intercom")
        try:
            await self.session.open_door(address, output_index)
        except ValueError as err:
            raise ViperError(str(err)) from err

    async def _start_call(self, target: str) -> VideoCall:
        """Start a call towards one entrance, for the relay."""
        if self.config is None or not self.config.is_entrance(target):
            raise ViperError(f"{target} is not an entrance panel in the configuration")
        call = VideoCall(self.session, target, logger=logging.getLogger(f"{__package__}.call"))
        await call.open()
        return call

    def stream_url(self, host: str | None = None, target: str | None = None) -> str | None:
        """Return the relay URL for the first entrance or a named one."""
        if self.relay is None:
            return None
        if host is None and not self.expose_stream:
            return self.relay.url(DEFAULT_HOST, target)
        return self.relay.url(host or self.rtsp_host or self.local_ip or DEFAULT_HOST, target)

    async def _dial(self, target: str | None) -> tuple[str, str]:
        """Start the call for a capture; return the entrance and the relay URL.

        Raises ViperError, in words, for whatever stops the call: no entrance,
        a call towards another entrance, or the panel refusing.
        """
        if self.config is None:
            raise ViperError("the configuration has not been read yet")
        target = target or self.config.entrance
        if target is None:
            raise ViperError("the configuration names no entrance panel")
        busy = self.relay.serving if self.relay is not None else None
        if busy is not None and busy != target:
            raise ViperError(f"a call towards {busy} is up and the panel allows one")
        url = self.stream_url("127.0.0.1", target)
        if url is None:
            raise ViperError("the video relay is not running")
        if self.relay is not None:
            await self.relay.prepare(target)
        return target, url

    async def async_capture_snapshot(self, target: str | None = None) -> None:
        """Take a still from an entrance camera and keep it."""
        target, url = await self._dial(target)
        code, stdout, stderr = await self._run_ffmpeg(
            ("-rtsp_transport", "tcp", "-i", url, "-frames:v", "1", "-q:v", "3", "-f", "image2", "-"),
            timeout=SNAPSHOT_TIMEOUT,
            capture=True,
        )
        if code != 0 or not stdout:
            _LOGGER.error("could not take a snapshot: %s", stderr.decode(errors="replace").strip())
            raise ViperError("ffmpeg could not read a frame from the relay; see the log")
        self.snapshots[target] = stdout
        self.snapshots_at[target] = datetime.now(UTC)
        self._notify("snapshot", {"entrance": target, "bytes": len(stdout)})

    async def async_record_clip(self, seconds: int | None = None, *, target: str | None = None) -> str | None:
        """Record a clip from the relay and return its path."""
        try:
            target, url = await self._dial(target)
        except ViperError as err:
            _LOGGER.error("could not record a clip: %s", err)
            return None
        duration = seconds or self.record_seconds
        directory = Path(self.record_path)
        # is_allowed_path does blocking I/O.
        if not await self.hass.async_add_executor_job(self.hass.config.is_allowed_path, str(directory)):
            _LOGGER.error(
                "refusing to write clips to %s: add it to allowlist_external_dirs or use a media directory", directory
            )
            return None
        self._clip_number = (self._clip_number + 1) % 10000
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        target = directory / f"ring_{stamp}_{self._clip_number:04d}.mp4"
        try:
            await self.hass.async_add_executor_job(lambda: directory.mkdir(parents=True, exist_ok=True))
        except OSError as err:
            _LOGGER.error("cannot create %s: %s", directory, err)
            return None
        code, _stdout, stderr = await self._run_ffmpeg(
            ("-rtsp_transport", "tcp", "-i", url, "-t", str(duration), "-c", "copy", "-y", str(target)),
            timeout=duration + RECORD_GRACE,
            capture=False,
        )
        if code != 0:
            _LOGGER.error("recording failed: %s", stderr.decode(errors="replace").strip())
            return None
        _LOGGER.info("saved ring clip %s", target)
        self._notify("recording", {"path": str(target)})
        return str(target)

    # ------------------------------------------------------------------ ffmpeg
    async def _run_ffmpeg(self, args: Sequence[str], *, timeout: float, capture: bool) -> tuple[int, bytes, bytes]:
        """Run ffmpeg with a timeout, and make sure it is dead before returning."""
        binary = self._ffmpeg_binary()
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "-loglevel",
                "error",
                *args,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as err:
            _LOGGER.error("cannot run %s: %s", binary, err)
            return 1, b"", str(err).encode()
        self._processes.add(process)
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            _LOGGER.error("%s took longer than %.0fs; stopping it", binary, timeout)
            await _end(process)
            return 1, b"", b"timed out"
        except asyncio.CancelledError:
            await _end(process)
            raise
        finally:
            self._processes.discard(process)
        return process.returncode or 0, stdout or b"", stderr or b""

    def _ffmpeg_binary(self) -> str:
        """Return the ffmpeg binary: the ffmpeg integration's if it is set up, else the one on PATH."""
        try:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager

            return get_ffmpeg_manager(self.hass).binary
        except ImportError, KeyError, ValueError:
            _LOGGER.debug("the ffmpeg integration is not set up; falling back to ffmpeg on the path")
            return "ffmpeg"

    async def _stop_processes(self) -> None:
        """Stop every ffmpeg this entry started."""
        processes, self._processes = list(self._processes), set()
        for process in processes:
            await _end(process)

    # ------------------------------------------------------------------ helpers
    @property
    def device_info(self) -> dict:
        """Return the device registry entry shared by every entity."""
        info = {
            "identifiers": {(DOMAIN, self.unique_id)},
            "manufacturer": MANUFACTURER,
            "name": "Comelit intercom",
            "configuration_url": f"http://{self.host}:{self.web_port}/",
        }
        if self.device is not None:
            info["model"] = self.device.model
            info["sw_version"] = self.device.firmware
            info["serial_number"] = self.device.vip_address
        return info


async def _end(process: asyncio.subprocess.Process) -> None:
    """Terminate a process, kill it if it does not exit, and reap it."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.terminate()
    try:
        async with asyncio.timeout(TERMINATE_GRACE):
            await process.wait()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
