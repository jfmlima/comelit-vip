"""Config and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_EXPOSE_STREAM,
    CONF_RECORD_ON_RING,
    CONF_RECORD_PATH,
    CONF_RECORD_SECONDS,
    CONF_RTSP_HOST,
    CONF_RTSP_PORT,
    CONF_SNAPSHOT_ON_RING,
    CONF_TOKEN,
    CONF_USER_SLOT,
    CONF_WEB_PORT,
    DEFAULT_PORT,
    DEFAULT_RECORD_SECONDS,
    DEFAULT_RTSP_PORT,
    DEFAULT_WEB_PASSWORD,
    DEFAULT_WEB_PORT,
    DOMAIN,
)
from .hub import default_record_path
from .viper import PanelUser, PanelWebClient, ViperSession, async_discover
from .viper.session import ViperAuthError, ViperError
from .viper.web import PanelWebAuthError, PanelWebError

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Optional(CONF_PASSWORD, default=DEFAULT_WEB_PASSWORD): TextSelector(),
        vol.Optional(CONF_TOKEN): TextSelector(),
        vol.Optional(CONF_WEB_PORT, default=DEFAULT_WEB_PORT): vol.Coerce(int),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PASSWORD, default=DEFAULT_WEB_PASSWORD): TextSelector(),
        vol.Optional(CONF_TOKEN): TextSelector(),
    }
)


def _error_for(err: Exception) -> str:
    """Return the form error key for a failure."""
    if isinstance(err, PanelWebAuthError):
        return "invalid_password"
    if isinstance(err, PanelWebError):
        return "no_token"
    if isinstance(err, ViperAuthError):
        return "invalid_token"
    return "cannot_connect"


class ComelitVipConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for a Comelit ViP panel."""

    VERSION = 2

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None
        self._pending: dict[str, Any] = {}
        self._users: list[PanelUser] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            token = (user_input.get(CONF_TOKEN) or "").strip().lower()
            password = user_input.get(CONF_PASSWORD) or ""
            web_port = int(user_input.get(CONF_WEB_PORT, DEFAULT_WEB_PORT))
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))

            self._pending = {CONF_HOST: host, CONF_PORT: port, CONF_WEB_PORT: web_port}
            if not host:
                errors["base"] = "cannot_connect"
                return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)
            try:
                if not token:
                    self._users = await self._users_from_web(host, password, web_port)
                    return await self.async_step_pick_user()
                await self._verify(host, port, token)
            except (PanelWebError, ViperError, OSError, ClientError, ValueError) as err:
                errors["base"] = _error_for(err)
            else:
                return await self._finish(token)
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def _finish(self, token: str) -> ConfigFlowResult:
        """Store the token on a new entry, or on the entry being reauthenticated."""
        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(self._reauth_entry, data={**self._reauth_entry.data, CONF_TOKEN: token})
        data = {**self._pending, CONF_TOKEN: token}
        host = data[CONF_HOST]
        info = await async_discover(host)
        await self.async_set_unique_id(info.mac if info else host)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        return self.async_create_entry(title=info.model if info else "Comelit intercom", data=data)

    async def async_step_pick_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which app user to authenticate as, even when there is only one."""
        errors: dict[str, str] = {}
        if user_input is not None:
            slot = int(user_input[CONF_USER_SLOT])
            token = next(user.token for user in self._users if user.slot == slot)
            try:
                await self._verify(self._pending[CONF_HOST], self._pending[CONF_PORT], token)
            except (ViperError, OSError, ClientError) as err:
                errors["base"] = _error_for(err)
            else:
                return await self._finish(token)
        return self.async_show_form(
            step_id="pick_user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USER_SLOT): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=str(user.slot),
                                    label=f"{user.slot}: {user.description or user.email or 'unnamed'}",
                                )
                                for user in self._users
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start again after a rejected token."""
        self._reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new token; the address is not editable here."""
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        errors: dict[str, str] = {}
        host = entry.data[CONF_HOST]
        port = entry.data.get(CONF_PORT, DEFAULT_PORT)
        web_port = entry.data.get(CONF_WEB_PORT, DEFAULT_WEB_PORT)
        self._pending = {CONF_HOST: host, CONF_PORT: port, CONF_WEB_PORT: web_port}
        if user_input is not None:
            token = (user_input.get(CONF_TOKEN) or "").strip().lower()
            password = user_input.get(CONF_PASSWORD) or ""
            try:
                if not token:
                    self._users = await self._users_from_web(host, password, web_port)
                    return await self.async_step_pick_user()
                await self._verify(host, port, token)
            except (PanelWebError, ViperError, OSError, ClientError, ValueError) as err:
                errors["base"] = _error_for(err)
            else:
                return await self._finish(token)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"host": host},
            errors=errors,
        )

    async def _users_from_web(self, host: str, password: str, web_port: int) -> list[PanelUser]:
        """Read the app users from a fresh backup."""
        client = PanelWebClient(async_get_clientsession(self.hass), host, password, port=web_port)
        backup = await client.fetch_config(fresh=True)
        return backup.users

    async def _verify(self, host: str, port: int, token: str) -> None:
        """Check that the token authenticates."""
        session = ViperSession(host, port)
        await session.connect()
        try:
            await session.authenticate(token)
            await session.get_configuration("none")
        finally:
            await session.close()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ComelitVipOptionsFlow:
        """Return the options flow."""
        return ComelitVipOptionsFlow()


class ComelitVipOptionsFlow(OptionsFlow):
    """Streaming and recording options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            user_input[CONF_RECORD_SECONDS] = int(user_input[CONF_RECORD_SECONDS])
            user_input[CONF_RTSP_PORT] = int(user_input[CONF_RTSP_PORT])
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(CONF_RTSP_PORT, default=options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): NumberSelector(
                    NumberSelectorConfig(min=1024, max=65535, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_EXPOSE_STREAM, default=options.get(CONF_EXPOSE_STREAM, False)): BooleanSelector(),
                vol.Optional(CONF_RTSP_HOST, default=options.get(CONF_RTSP_HOST, "")): TextSelector(),
                vol.Optional(CONF_SNAPSHOT_ON_RING, default=options.get(CONF_SNAPSHOT_ON_RING, False)): BooleanSelector(),
                vol.Optional(CONF_RECORD_ON_RING, default=options.get(CONF_RECORD_ON_RING, False)): BooleanSelector(),
                vol.Optional(
                    CONF_RECORD_SECONDS, default=options.get(CONF_RECORD_SECONDS, DEFAULT_RECORD_SECONDS)
                ): NumberSelector(NumberSelectorConfig(min=5, max=120, step=1, mode=NumberSelectorMode.BOX)),
                vol.Optional(
                    CONF_RECORD_PATH, default=options.get(CONF_RECORD_PATH) or default_record_path(self.hass)
                ): TextSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
