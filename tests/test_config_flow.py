"""Setup flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comelit_vip import async_migrate_entry
from custom_components.comelit_vip.const import (
    CONF_EXPOSE_STREAM,
    CONF_RECORD_ON_RING,
    CONF_RECORD_PATH,
    CONF_RECORD_SECONDS,
    CONF_RTSP_HOST,
    CONF_RTSP_PORT,
    CONF_SNAPSHOT_ON_RING,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.comelit_vip.viper.session import ViperAuthError
from custom_components.comelit_vip.viper.web import PanelBackup, PanelUser, PanelWebAuthError, PanelWebError

TOKEN = "274a64" + "0" * 22 + "8ad0"
USER_INPUT = {CONF_HOST: "192.0.2.21", CONF_PASSWORD: "comelit"}


def _backup() -> PanelBackup:
    return PanelBackup(
        users=[PanelUser(slot=2, description="Home Assistant", token=TOKEN, email="someone@example.com")],
        apartment_address="SB000042",
        entrance_address="SB900001",
    )


@pytest.fixture
def happy_path():
    """Patch the panel away so the flow can run without one."""
    with (
        patch(
            "custom_components.comelit_vip.config_flow.PanelWebClient.fetch_config",
            AsyncMock(return_value=_backup()),
        ) as web,
        patch("custom_components.comelit_vip.config_flow.ViperSession.connect", AsyncMock()),
        patch("custom_components.comelit_vip.config_flow.ViperSession.authenticate", AsyncMock()) as auth,
        patch("custom_components.comelit_vip.config_flow.ViperSession.get_configuration", AsyncMock()),
        patch("custom_components.comelit_vip.config_flow.ViperSession.close", AsyncMock()),
        patch("custom_components.comelit_vip.config_flow.async_discover", AsyncMock(return_value=None)),
        patch("custom_components.comelit_vip.async_setup_entry", AsyncMock(return_value=True)),
    ):
        yield {"web": web, "auth": auth}


async def test_token_from_backup(hass, happy_path):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "pick_user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"user_slot": "2"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == TOKEN
    assert result["data"][CONF_HOST] == "192.0.2.21"
    # The installer password is not stored.
    assert CONF_PASSWORD not in result["data"]


async def test_backup_taken_fresh(hass, happy_path):
    """A user added moments ago is missing from an older backup."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    assert happy_path["web"].await_args.kwargs == {"fresh": True}


async def test_supplied_token_skips_web(hass, happy_path):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_TOKEN: TOKEN.upper()})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    happy_path["web"].assert_not_awaited()
    assert result["data"][CONF_TOKEN] == TOKEN, "stored lowercase"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (PanelWebAuthError("no"), "invalid_password"),
        (PanelWebError("none"), "no_token"),
    ],
)
async def test_web_errors_mapped(hass, happy_path, failure, expected):
    happy_path["web"].side_effect = failure
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_rejected_token(hass, happy_path):
    happy_path["auth"].side_effect = ViperAuthError("rejected")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"user_slot": "2"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_token"}


async def test_pick_user_among_several(hass, happy_path):
    happy_path["web"].return_value = PanelBackup(
        users=[
            PanelUser(slot=1, description="iPhone", token="a" * 32, email="someone@example.com"),
            PanelUser(slot=2, description="Home Assistant", token=TOKEN, email="someone@example.com"),
        ],
        apartment_address="SB000042",
        entrance_address="SB900001",
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick_user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"user_slot": "2"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == TOKEN


async def test_pick_user_even_if_one(hass, happy_path):
    """On a fresh panel the only user is the owner's phone."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick_user"


async def test_empty_host(hass, happy_path):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_HOST: "  "})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_web_client_error(hass, happy_path):
    happy_path["web"].side_effect = ClientError("no route")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# ---------------------------------------------------------------- reauthentication
async def test_reauth_form_has_no_host(hass, happy_path):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=entry.data
    )

    assert result["step_id"] == "reauth_confirm"
    assert CONF_HOST not in {str(key) for key in result["data_schema"].schema}


async def test_reauth_updates_token(hass, happy_path):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=entry.data
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "b" * 32})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "b" * 32
    assert entry.data[CONF_HOST] == "192.0.2.21"
    assert entry.unique_id == "aa:bb:cc:dd:ee:ff"


async def test_reauth_pick_user(hass, happy_path):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=entry.data
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "comelit"})
    assert result["step_id"] == "pick_user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"user_slot": "2"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert entry.data[CONF_TOKEN] == TOKEN
    assert entry.data[CONF_HOST] == "192.0.2.21"


# --------------------------------------------------------------------- identity
async def test_migrate_v1_unique_ids(hass):
    entry = _entry(version=1)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    item = registry.async_get_or_create(
        "event", DOMAIN, f"{entry.entry_id}_doorbell", config_entry=entry, suggested_object_id="doorbell"
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(item.entity_id).unique_id == "aa:bb:cc:dd:ee:ff_doorbell"
    assert entry.version == 2


async def test_migrate_v1_device(hass):
    entry = _entry(version=1)
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)})

    assert await async_migrate_entry(hass, entry)

    assert devices.async_get_device(identifiers={(DOMAIN, "aa:bb:cc:dd:ee:ff")}) is not None
    assert devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)}) is None
    assert device.id == devices.async_get_device(identifiers={(DOMAIN, "aa:bb:cc:dd:ee:ff")}).id


async def test_migrate_v1_skips_host_unique_id(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, unique_id="192.0.2.21", data={CONF_HOST: "192.0.2.21", CONF_TOKEN: "a" * 32}
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    item = registry.async_get_or_create(
        "event", DOMAIN, f"{entry.entry_id}_doorbell", config_entry=entry, suggested_object_id="doorbell"
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(item.entity_id).unique_id == f"{entry.entry_id}_doorbell"
    assert entry.version == 2


async def test_duplicate_panel_aborts(hass, happy_path):
    with patch(
        "custom_components.comelit_vip.config_flow.async_discover",
        AsyncMock(return_value=SimpleNamespace(mac="aa:bb:cc:dd:ee:ff", model="6741W")),
    ):
        existing = _entry()
        existing.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"user_slot": "2"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_coerce_ints(hass, happy_path):
    """NumberSelector returns floats."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_RTSP_PORT: 8555.0,
            CONF_EXPOSE_STREAM: True,
            CONF_RTSP_HOST: "192.0.2.9",
            CONF_SNAPSHOT_ON_RING: True,
            CONF_RECORD_ON_RING: False,
            CONF_RECORD_SECONDS: 30.0,
            CONF_RECORD_PATH: "/media/comelit_vip",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_RTSP_PORT] == 8555
    assert isinstance(entry.options[CONF_RTSP_PORT], int)
    assert isinstance(entry.options[CONF_RECORD_SECONDS], int)
    assert entry.options[CONF_EXPOSE_STREAM] is True


async def test_options_defaults_from_entry(hass, happy_path):
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_RTSP_PORT: 9000})

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["data_schema"]({})[CONF_RTSP_PORT] == 9000


def _entry(version: int = 2) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=version,
        unique_id="aa:bb:cc:dd:ee:ff",
        data={CONF_HOST: "192.0.2.21", CONF_TOKEN: "a" * 32},
    )
