"""Config flow for Midea Cloud Control."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .client import SUPPORTED, CloudError, CloudInvalidAuthError, MideaCloudClient
from .const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_CLOUD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)
from .session import normalize_account

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_CLOUD, default="NetHome Plus"): SelectSelector(
            SelectSelectorConfig(
                options=list(SUPPORTED), mode=SelectSelectorMode.DROPDOWN
            )
        ),
        vol.Optional(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_SECONDS
        ): vol.All(
            vol.Coerce(int),
            vol.Range(
                min=MIN_SCAN_INTERVAL_SECONDS, max=MAX_SCAN_INTERVAL_SECONDS
            ),
        ),
    }
)


def account_unique_id(cloud_name: str, account: str) -> str:
    """Create a stable config-entry ID without storing the account in it."""
    value = f"{cloud_name}\0{normalize_account(account)}".encode()
    return hashlib.sha256(value).hexdigest()


class MideaCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure one cloud account with multiple appliance entities."""

    VERSION = 2

    def __init__(self) -> None:
        self._credentials: dict[str, Any] = {}
        self._appliances: dict[int, dict[str, Any]] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return an options flow for appliance selection and polling."""
        return MideaCloudOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and enumerate the account's appliances."""
        errors: dict[str, str] = {}

        if user_input is not None:
            cloud_name = user_input[CONF_CLOUD]
            account = user_input[CONF_USERNAME]
            # Version-1 entries used appliance IDs as unique IDs. Detect those
            # without exposing the account in the new unique ID or logs.
            if any(
                entry.data.get(CONF_CLOUD) == cloud_name
                and normalize_account(entry.data.get(CONF_USERNAME, ""))
                == normalize_account(account)
                for entry in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")

            client = MideaCloudClient(
                account,
                user_input[CONF_PASSWORD],
                cloud_name,
                session=async_get_clientsession(self.hass),
            )
            try:
                await client.connect()
                self._appliances = await client.list_appliances()
            except CloudInvalidAuthError:
                errors["base"] = "invalid_auth"
            except CloudError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error connecting to the Midea cloud")
                errors["base"] = "unknown"
            finally:
                await client.close()

            if not errors:
                if not self._appliances:
                    errors["base"] = "no_appliances"
                else:
                    await self.async_set_unique_id(
                        account_unique_id(cloud_name, account)
                    )
                    self._abort_if_unique_id_configured()
                    self._credentials = user_input
                    return await self.async_step_appliances()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_appliances(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select all appliances that should share the account session."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = [int(value) for value in user_input[CONF_APPLIANCES]]
            if not selected:
                errors["base"] = "select_appliance"
            else:
                appliances = [
                    {
                        CONF_APPLIANCE_ID: appliance_id,
                        CONF_APPLIANCE_NAME: self._appliances[appliance_id].get("name")
                        or f"Midea {appliance_id}",
                    }
                    for appliance_id in selected
                ]
                return self.async_create_entry(
                    title=(
                        f"{self._credentials[CONF_CLOUD]} "
                        f"({len(appliances)} appliances)"
                    ),
                    data={**self._credentials, CONF_APPLIANCES: appliances},
                )

        options = [
            SelectOptionDict(
                value=str(appliance_id),
                label=f"{info.get('name') or 'Unnamed'} ({appliance_id})",
            )
            for appliance_id, info in self._appliances.items()
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_APPLIANCES,
                    default=[str(appliance_id) for appliance_id in self._appliances],
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="appliances", data_schema=schema, errors=errors
        )


class MideaCloudOptionsFlow(OptionsFlow):
    """Update appliances and polling without storing credentials again."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._appliances: dict[int, dict[str, Any]] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List current account appliances and save the selected subset."""
        errors: dict[str, str] = {}
        if not self._appliances:
            runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
            client = runtime.client if runtime is not None else MideaCloudClient(
                self._entry.data[CONF_USERNAME],
                self._entry.data[CONF_PASSWORD],
                self._entry.data[CONF_CLOUD],
                session=async_get_clientsession(self.hass),
            )
            temporary_client = runtime is None
            try:
                await client.connect()
                self._appliances = await client.list_appliances()
            except CloudError:
                errors["base"] = "cannot_connect"
            finally:
                if temporary_client:
                    await client.close()

        if user_input is not None and not errors:
            selected = [int(value) for value in user_input[CONF_APPLIANCES]]
            if not selected:
                errors["base"] = "select_appliance"
            else:
                appliances = [
                    {
                        CONF_APPLIANCE_ID: appliance_id,
                        CONF_APPLIANCE_NAME: self._appliances[appliance_id].get("name")
                        or f"Midea {appliance_id}",
                    }
                    for appliance_id in selected
                ]
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_APPLIANCES: appliances,
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    },
                )

        configured = self._entry.options.get(
            CONF_APPLIANCES, self._entry.data[CONF_APPLIANCES]
        )
        selected_ids = [
            str(item[CONF_APPLIANCE_ID])
            for item in configured
            if int(item[CONF_APPLIANCE_ID]) in self._appliances
        ]
        options = [
            SelectOptionDict(
                value=str(appliance_id),
                label=f"{info.get('name') or 'Unnamed'} ({appliance_id})",
            )
            for appliance_id, info in self._appliances.items()
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_APPLIANCES, default=selected_ids): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self._entry.options.get(
                        CONF_SCAN_INTERVAL,
                        self._entry.data.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS
                        ),
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=MIN_SCAN_INTERVAL_SECONDS,
                        max=MAX_SCAN_INTERVAL_SECONDS,
                    ),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
