"""ffmpeg processes the hub starts."""

from __future__ import annotations

import asyncio

import pytest
from fake_panel import CONFIGURATION

from custom_components.comelit_vip.hub import ComelitVipHub
from custom_components.comelit_vip.viper.models import PanelConfig


@pytest.fixture
def hub(hass, tmp_path):
    """A hub with no panel behind it."""
    hub = ComelitVipHub(hass, "entry", host="192.0.2.1", token="t" * 32, port=64100, rtsp_port=8554)
    hub.config = PanelConfig.from_response(CONFIGURATION)
    hub.record_path = str(tmp_path)
    hub.relay = _FakeRelay()
    return hub


class _FakeRelay:
    viewers = 0
    serving = None

    def url(self, host=None, target=None):
        return f"rtsp://{host or '127.0.0.1'}:8554/comelit"

    async def stop(self):
        return

    async def prepare(self, target=None):
        return

    async def abandon_call(self):
        return


async def test_process_tracked(hub, tmp_path):
    hub._ffmpeg_binary = _fake_ffmpeg(tmp_path, "exit 0")
    seen: list[object] = []

    async def _watch() -> None:
        await _until(lambda: bool(hub._processes))
        seen.extend(hub._processes)

    watcher = asyncio.create_task(_watch())
    code, _out, _err = await hub._run_ffmpeg((), timeout=10, capture=False)
    await watcher

    assert code == 0
    assert seen
    assert hub._processes == set()


async def test_process_timeout(hub, tmp_path):
    hub._ffmpeg_binary = _fake_ffmpeg(tmp_path, "sleep 30")

    code, _out, err = await hub._run_ffmpeg((), timeout=0.2, capture=False)

    assert code == 1
    assert err == b"timed out"
    assert hub._processes == set()


async def test_shutdown_ends_process(hub, tmp_path):
    hub._ffmpeg_binary = _fake_ffmpeg(tmp_path, "sleep 30")
    running = asyncio.create_task(hub._run_ffmpeg((), timeout=60, capture=False))
    await _until(lambda: bool(hub._processes))
    process = next(iter(hub._processes))

    await hub.async_shutdown()

    assert process.returncode is not None
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


async def test_missing_binary(hub):
    hub._ffmpeg_binary = lambda: "/nonexistent/ffmpeg"

    code, _out, _err = await hub._run_ffmpeg(("-i", "x"), timeout=5, capture=False)

    assert code == 1


async def test_binary_fallback(hub):
    assert hub._ffmpeg_binary() == "ffmpeg"


async def test_binary_from_ffmpeg_component(hub, monkeypatch):
    pytest.importorskip("haffmpeg", reason="the ffmpeg integration is not installed here")
    from homeassistant.components.ffmpeg import DATA_FFMPEG, FFmpegManager

    monkeypatch.setitem(hub.hass.data, DATA_FFMPEG, FFmpegManager(hub.hass, "/opt/bin/ffmpeg"))

    assert hub._ffmpeg_binary() == "/opt/bin/ffmpeg"


async def test_clip_names_unique(hub, monkeypatch, freezer):
    freezer.move_to("2026-08-23 18:00:00+00:00")
    written = []

    async def _record(args, *, timeout, capture):
        written.append(args[-1])
        return 0, b"", b""

    monkeypatch.setattr(hub, "_run_ffmpeg", _record)
    monkeypatch.setattr(hub.hass.config, "is_allowed_path", lambda path: True)

    await hub.async_record_clip(1)
    await hub.async_record_clip(1)

    assert len(set(written)) == 2


async def test_disallowed_path_refused(hub, monkeypatch):
    started = []

    async def _record(args, *, timeout, capture):
        started.append(args)
        return 0, b"", b""

    monkeypatch.setattr(hub, "_run_ffmpeg", _record)
    monkeypatch.setattr(hub.hass.config, "is_allowed_path", lambda path: False)

    assert await hub.async_record_clip(1) is None
    assert started == [], "no ffmpeg run"


async def test_cancel_ends_process(hub, tmp_path):
    hub._ffmpeg_binary = _fake_ffmpeg(tmp_path, "sleep 30")
    running = asyncio.create_task(hub._run_ffmpeg((), timeout=60, capture=False))
    await _until(lambda: bool(hub._processes))
    process = next(iter(hub._processes))

    running.cancel()
    await asyncio.gather(running, return_exceptions=True)

    assert process.returncode is not None
    assert hub._processes == set()


def _fake_ffmpeg(tmp_path, body: str):
    """Return a stand-in that ignores the arguments ffmpeg would be given."""
    script = tmp_path / "fake-ffmpeg"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return lambda: str(script)


async def _until(ready, passes: int = 500) -> None:
    """Let the loop run until something becomes true."""
    for _ in range(passes):
        if ready():
            return
        await asyncio.sleep(0)
    raise AssertionError("it never happened")
