"""Read app user tokens from the panel's installer web interface on port 8080.

The tokens are in ``/etc/comelit/users.cfg`` inside the configuration backup
the interface creates. Login needs the installer password (factory default
``comelit``).
"""

from __future__ import annotations

import gzip
import io
import logging
import re
import tarfile
import zlib
from dataclasses import dataclass

import aiohttp

DEFAULT_WEB_PORT = 8080
_BACKUP_RE = re.compile(r"""href=['"](\d+\.tar\.gz)['"]""", re.IGNORECASE)
_USER_LINE_RE = re.compile(r"^mspUsersMap\.\d+\.(\d+)\s*=\s*(.*)$")
_FIELD_RE = re.compile(r'(\d+):(?:2|4):(?:"([^"]*)"|([^\s]+))')
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_BACKUP_NAME_RE = re.compile(r"\d+\.tar\.gz")
_VIP_ADDRESS_RE = re.compile(r'"(SB\d{6})"')
_MAX_BACKUPS = 5
# A real backup is a few KB. The member bound matters on its own: gzip expands
# up to ~1000:1, on the event loop.
_MAX_ARCHIVE_BYTES = 8 << 20
_MAX_MEMBER_BYTES = 4 << 20

_LOGGER = logging.getLogger(__name__)


class PanelWebError(RuntimeError):
    """The installer interface could not provide usable credentials."""


class PanelWebAuthError(PanelWebError):
    """The installer password was rejected."""


@dataclass(slots=True, frozen=True)
class PanelUser:
    """One app user in the panel configuration."""

    slot: int
    description: str
    token: str
    email: str = ""


@dataclass(slots=True, frozen=True)
class PanelBackup:
    """The contents of a configuration backup."""

    users: list[PanelUser]
    apartment_address: str = ""
    entrance_address: str = ""


def parse_panel_backup(archive: bytes) -> PanelBackup:
    """Return the users and addresses in a configuration backup.

    The archive is a tar of config files; ``users.cfg`` is gzipped again
    inside it and is the only file that must be present.
    """
    members = _open_backup(archive)
    users = [user for user in map(_read_user, members["users.cfg"].splitlines()) if user is not None]
    if not users:
        raise PanelWebError("backup contains no active app users; pair the Comelit app first")
    return PanelBackup(
        users=users,
        apartment_address=_first_address(members["apartments.cfg"]),
        entrance_address=_entrance_address(members["addressbook.cfg"]),
    )


def _open_backup(archive: bytes) -> dict[str, str]:
    """Read the three config files; only ``users.cfg`` is required."""
    wanted = ("users.cfg", "apartments.cfg", "addressbook.cfg")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            texts = {}
            for name in wanted:
                try:
                    texts[name] = _read_member(bundle, name)
                except PanelWebError:
                    if name == "users.cfg":
                        raise
                    texts[name] = ""
    except (OSError, EOFError, zlib.error, tarfile.TarError) as exc:
        raise PanelWebError("invalid or incomplete configuration backup") from exc
    return texts


def _read_user(line: str) -> PanelUser | None:
    """Parse one ``mspUsersMap`` line; field 4 is the occupied flag, 9 the token."""
    match = _USER_LINE_RE.match(line)
    if match is None:
        return None
    fields = {
        int(f.group(1)): f.group(2) if f.group(2) is not None else f.group(3) for f in _FIELD_RE.finditer(match.group(2))
    }
    token = fields.get(9, "")
    if fields.get(4) != "1" or not _TOKEN_RE.fullmatch(token):
        return None
    return PanelUser(
        slot=int(match.group(1)),
        description=fields.get(6, ""),
        token=token.lower(),
        email=fields.get(11, ""),
    )


def _first_address(text: str) -> str:
    """Return the first ViP address anywhere in a config file."""
    found = _VIP_ADDRESS_RE.search(text)
    return found.group(1) if found else ""


def _entrance_address(addressbook: str) -> str:
    """Return the first entrance panel address in the address book.

    The book may open with a ``mspAddressBookEntrances.size`` line that holds
    no address.
    """
    for line in addressbook.splitlines():
        if not line.startswith("mspAddressBookEntrances."):
            continue
        found = _VIP_ADDRESS_RE.search(line)
        if found is not None:
            return found.group(1)
    return ""


class PanelWebClient:
    """Client for the installer web interface."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        password: str,
        *,
        port: int = DEFAULT_WEB_PORT,
        timeout: float = 20.0,
    ) -> None:
        """Set up a client for one panel."""
        if not host:
            raise ValueError("host is required")
        if not password:
            raise ValueError("installer password is required")
        self._session = session
        self._base = f"http://{host}:{port}/"
        self._password = password
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        # Kept by hand: a shared ClientSession's jar may drop cookies from an IP host.
        self._cookies: dict[str, str] = {}

    def _url(self, path: str) -> str:
        return self._base + path

    def _remember_cookies(self, resp: aiohttp.ClientResponse) -> None:
        for name, morsel in resp.cookies.items():
            self._cookies[name] = morsel.value

    async def _get(self, path: str) -> str:
        async with self._session.get(self._url(path), timeout=self._timeout, cookies=self._cookies) as resp:
            resp.raise_for_status()
            self._remember_cookies(resp)
            return await resp.text(errors="replace")

    async def _post(self, path: str, data: dict | None = None) -> str:
        async with self._session.post(self._url(path), data=data, timeout=self._timeout, cookies=self._cookies) as resp:
            resp.raise_for_status()
            self._remember_cookies(resp)
            return await resp.text(errors="replace")

    async def login(self) -> None:
        """Log in, raising :class:`PanelWebAuthError` if the password fails."""
        await self._post("do-login.html", {"l-pwd": self._password})
        page = await self._get("config-backup.html")
        if "create-backup.html" not in page:
            raise PanelWebAuthError("installer login failed")

    async def list_backups(self) -> list[str]:
        """Return the backup filenames the panel holds."""
        page = await self._get("config-backup.html")
        if page.strip() == "LOGIN_IS_REQUIRED":
            raise PanelWebAuthError("installer login is required")
        return sorted(set(_BACKUP_RE.findall(page)), key=lambda n: int(n.split(".")[0]))

    async def create_backup(self) -> str:
        """Create a backup and return its filename."""
        before = set(await self.list_backups())
        text = await self._post("create-backup.html")
        if text.strip() == "LOGIN_IS_REQUIRED":
            raise PanelWebAuthError("installer login is required")
        after = await self.list_backups()
        created = sorted(set(after) - before, key=lambda n: int(n.split(".")[0]))
        if created:
            return created[-1]
        if len(after) >= _MAX_BACKUPS:
            raise PanelWebError("backup limit reached; delete an old backup in the installer UI")
        raise PanelWebError(text.strip() or "the monitor did not create a backup")

    async def download_backup(self, filename: str) -> bytes:
        """Download a backup archive."""
        if not _BACKUP_NAME_RE.fullmatch(filename):
            raise ValueError(f"{filename!r} is not a backup this panel would have made")
        async with self._session.get(self._url(filename), timeout=self._timeout, cookies=self._cookies) as resp:
            resp.raise_for_status()
            # read(n) may return less than n.
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.content.iter_chunked(64 << 10):
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    raise PanelWebError("the backup is far larger than a configuration backup can be")
                chunks.append(chunk)
            return b"".join(chunks)

    async def fetch_config(self, *, fresh: bool = False) -> PanelBackup:
        """Log in, reuse or create a backup, and return its contents.

        ``fresh`` forces a new backup. If the panel cannot make one, the newest
        existing backup is used.
        """
        await self.login()
        backups = await self.list_backups()
        filename = ""
        if fresh or not backups:
            try:
                filename = await self.create_backup()
            except PanelWebError as err:
                if not backups:
                    raise
                _LOGGER.warning("could not take a new backup (%s); reading the newest one instead", err)
        return parse_panel_backup(await self.download_backup(filename or backups[-1]))


def _read_member(bundle: tarfile.TarFile, name: str) -> str:
    try:
        member = bundle.getmember(f"etc/comelit/{name}")
    except KeyError as exc:
        raise PanelWebError(f"backup has no {name}") from exc
    stream = bundle.extractfile(member)
    if stream is None:
        raise PanelWebError(f"backup contains no readable {name}")
    data = stream.read(_MAX_MEMBER_BYTES + 1)
    if data.startswith(b"\x1f\x8b"):
        data = _gunzip(data, name)
    if len(data) > _MAX_MEMBER_BYTES:
        raise PanelWebError(f"{name} in the backup is implausibly large")
    return data.decode("utf-8", "replace")


def _gunzip(data: bytes, name: str) -> bytes:
    """Expand one gzipped member, bounded."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            return stream.read(_MAX_MEMBER_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise PanelWebError(f"{name} in the backup could not be read") from exc
