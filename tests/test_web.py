"""Backup parsing for the token bootstrap."""

import gzip
import io
import tarfile

import pytest

from custom_components.comelit_vip.viper.web import PanelWebClient, PanelWebError, parse_panel_backup

TOKEN = "1234567890abcdef1234567890abcdef"
USERS = (
    f'mspUsersMap.0.1 = 4:2:1 6:4:"iPhone" 9:4:"{TOKEN}" 11:4:"someone@example.com"\nmspUsersMap.0.2 = 4:2:2 6:4:"" 9:4:""\n'
)


def _archive(
    users: str = USERS, apartments: str = 'x = "SB000042"', addressbook: str = 'mspAddressBookEntrances.0 = "SB900001"'
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, text, compress in (
            ("users.cfg", users, True),
            ("apartments.cfg", apartments, False),
            ("addressbook.cfg", addressbook, False),
        ):
            data = gzip.compress(text.encode()) if compress else text.encode()
            info = tarfile.TarInfo(f"etc/comelit/{name}")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_parse_backup():
    backup = parse_panel_backup(_archive())
    assert [u.token for u in backup.users] == [TOKEN]
    assert backup.users[0].description == "iPhone"
    assert backup.apartment_address == "SB000042"
    assert backup.entrance_address == "SB900001"


def test_disabled_slots_are_ignored():
    users = f'mspUsersMap.0.1 = 4:2:2 6:4:"Old" 9:4:"{TOKEN}"\n'
    with pytest.raises(PanelWebError):
        parse_panel_backup(_archive(users=users))


def test_rejects_garbage():
    with pytest.raises(PanelWebError):
        parse_panel_backup(b"not a tar")


def test_corrupt_gzip_is_panel_error():
    whole = gzip.compress(USERS.encode())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        data = whole[: len(whole) // 2]
        info = tarfile.TarInfo("etc/comelit/users.cfg")
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))

    with pytest.raises(PanelWebError):
        parse_panel_backup(buffer.getvalue())


def test_oversized_member_rejected():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        data = gzip.compress(b"\x00" * (16 << 20))
        info = tarfile.TarInfo("etc/comelit/users.cfg")
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))

    with pytest.raises(PanelWebError, match="implausibly large"):
        parse_panel_backup(buffer.getvalue())


def test_malformed_token_ignored():
    users = 'mspUsersMap.0.1 = 4:2:1 6:4:"iPhone" 9:4:"not-a-token" 11:4:"someone@example.com"\n'

    with pytest.raises(PanelWebError):
        parse_panel_backup(_archive(users=users))


def test_entrance_after_size_header():
    """The `.size` header line matches the prefix and holds no address."""
    book = 'mspAddressBookEntrances.size = 1\nmspAddressBookEntrances.0 = "SB900001"\n'

    assert parse_panel_backup(_archive(addressbook=book)).entrance_address == "SB900001"


async def test_backup_name_validated():
    client = PanelWebClient(object(), "127.0.0.1", "password")

    with pytest.raises(ValueError):
        await client.download_backup("../../etc/passwd")
