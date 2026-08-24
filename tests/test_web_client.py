"""The installer web client against a fake installer UI."""

from __future__ import annotations

import gzip
import io
import tarfile

import aiohttp
import pytest
from aiohttp import web

from custom_components.comelit_vip.viper.web import PanelWebAuthError, PanelWebClient, PanelWebError

TOKEN = "1234567890abcdef1234567890abcdef"
USERS = f'mspUsersMap.0.1 = 4:2:1 6:4:"Home Assistant" 9:4:"{TOKEN}" 11:4:"someone@example.com"\n'


def _archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in (
            ("users.cfg", gzip.compress(USERS.encode())),
            ("apartments.cfg", b'x = "SB000042"'),
            ("addressbook.cfg", b'mspAddressBookEntrances.0 = "SB900001"'),
        ):
            info = tarfile.TarInfo(f"etc/comelit/{name}")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeInstallerUI:
    """The three pages the client uses, and the backup files behind them."""

    def __init__(self) -> None:
        self.password = "comelit"
        self.backups: list[str] = []
        self.logged_in = False
        self.creates = 0
        self.create_refuses = False
        self.app = web.Application()
        self.app.router.add_post("/do-login.html", self._login)
        self.app.router.add_get("/config-backup.html", self._page)
        self.app.router.add_post("/create-backup.html", self._create)
        self.app.router.add_get("/{name}.tar.gz", self._download)

    async def _login(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.logged_in = form.get("l-pwd") == self.password
        response = web.Response(text="ok")
        if self.logged_in:
            response.set_cookie("session", "granted")
        return response

    async def _page(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return web.Response(text="LOGIN_IS_REQUIRED")
        links = "".join(f"<a href='{name}'>{name}</a>" for name in self.backups)
        return web.Response(text=f"<html><form action='create-backup.html'></form>{links}</html>")

    async def _create(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return web.Response(text="LOGIN_IS_REQUIRED")
        self.creates += 1
        if self.create_refuses:
            return web.Response(text="the monitor is busy")
        self.backups.append(f"{len(self.backups) + 1}.tar.gz")
        return web.Response(text="ok")

    async def _download(self, request: web.Request) -> web.Response:
        return web.Response(body=_archive(), content_type="application/octet-stream")

    def _allowed(self, request: web.Request) -> bool:
        return request.cookies.get("session") == "granted"


@pytest.fixture
async def panel_ui(socket_enabled):
    """A running installer UI. Started by hand: `aiohttp_server` comes from pytest-aiohttp, which is not a declared dependency."""
    ui = FakeInstallerUI()
    runner = web.AppRunner(ui.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    ui.host, ui.port = runner.addresses[0][:2]
    try:
        yield ui
    finally:
        await runner.cleanup()


@pytest.fixture
async def client(panel_ui):
    """A web client pointed at it."""
    async with aiohttp.ClientSession() as session:
        yield PanelWebClient(session, panel_ui.host, "comelit", port=panel_ui.port)


async def test_host_and_password_required():
    with pytest.raises(ValueError):
        PanelWebClient(object(), "", "comelit")
    with pytest.raises(ValueError):
        PanelWebClient(object(), "192.0.2.21", "")


async def test_wrong_password(client, panel_ui):
    panel_ui.password = "something else"

    with pytest.raises(PanelWebAuthError):
        await client.login()


async def test_first_backup_created(client, panel_ui):
    backup = await client.fetch_config()

    assert panel_ui.creates == 1
    assert [user.token for user in backup.users] == [TOKEN]
    assert backup.apartment_address == "SB000042"
    assert backup.entrance_address == "SB900001"


async def test_existing_backup_reused(client, panel_ui):
    panel_ui.backups = ["1.tar.gz"]

    await client.fetch_config()

    assert panel_ui.creates == 0


async def test_fresh_backup(client, panel_ui):
    panel_ui.backups = ["1.tar.gz"]

    await client.fetch_config(fresh=True)

    assert panel_ui.creates == 1


async def test_fallback_to_newest_backup(client, panel_ui, caplog):
    """The panel holds at most five backups."""
    panel_ui.backups = ["1.tar.gz", "2.tar.gz"]
    panel_ui.create_refuses = True

    backup = await client.fetch_config(fresh=True)

    assert [user.token for user in backup.users] == [TOKEN]
    assert "could not take a new backup" in caplog.text


async def test_no_backup_possible(client, panel_ui):
    panel_ui.create_refuses = True

    with pytest.raises(PanelWebError):
        await client.fetch_config()


async def test_backups_sorted_numerically(client, panel_ui):
    panel_ui.backups = ["10.tar.gz", "9.tar.gz", "2.tar.gz"]
    await client.login()

    assert await client.list_backups() == ["2.tar.gz", "9.tar.gz", "10.tar.gz"]


async def test_list_requires_login(client, panel_ui):
    with pytest.raises(PanelWebAuthError):
        await client.list_backups()
