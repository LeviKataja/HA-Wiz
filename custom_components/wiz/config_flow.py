"""Config flow for WiZ."""

from collections.abc import Mapping
import logging
from typing import Any

from pywizlight import wizlight
from pywizlight.discovery import DiscoveredBulb
from pywizlight.exceptions import WizLightConnectionError, WizLightTimeOutError
import voluptuous as vol

from homeassistant.components import onboarding
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_DEVICE, CONF_HOST
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.util.network import is_ip_address

from .const import CONF_SIGNING_KEY, DEFAULT_NAME, DISCOVER_SCAN_TIMEOUT, DOMAIN
from .discovery import async_discover_devices
from .utils import (
    _short_mac,
    async_get_bulb_type,
    error_code,
    firmware_requires_signing,
    is_signature_rejected,
    is_socket_error,
    is_timeout,
    is_unsigned_rejected,
    is_valid_signing_key,
    name_from_bulb_type_and_mac,
    normalize_signing_key,
)

_LOGGER = logging.getLogger(__name__)

_KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class WizConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WiZ."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: dict[str, DiscoveredBulb] = {}
        self._discovered_device: DiscoveredBulb | None = None
        self._host: str | None = None
        self._mac: str | None = None
        self._name: str | None = None
        self._requires_signing = False
        self._signing_key: str | None = None

    # ------------------------------------------------------------------
    # Talking to the device
    # ------------------------------------------------------------------

    async def _async_try_probe(self, host: str, signing_key: str | None) -> str | None:
        """Read the device's identity. Returns an error key or None."""
        bulb = wizlight(host, signing_key=signing_key)
        try:
            config = await bulb.getBulbConfig()
            bulbtype = await async_get_bulb_type(bulb)
            mac = await bulb.getMac()
        except (WizLightTimeOutError, WizLightConnectionError, OSError) as err:
            if is_timeout(err):
                _LOGGER.warning(
                    "%s did not answer on UDP port 38899. Check the IP, that the "
                    "device is powered, and that nothing blocks UDP between Home "
                    "Assistant and the device",
                    host,
                )
                return "bulb_time_out"
            if is_socket_error(err):
                _LOGGER.warning(
                    "%s refused UDP port 38899 (%s). Something is at that "
                    "address, but it is not a WiZ device, or the IP is wrong",
                    host,
                    err.__cause__ or err,
                )
                return "cannot_connect"
            if is_signature_rejected(err):
                _LOGGER.warning("%s rejected the request signature: %s", host, err)
                return "invalid_signing_key" if signing_key else "signing_required"
            if is_unsigned_rejected(err) and not signing_key:
                _LOGGER.warning(
                    "%s rejected an unsigned read (%s); it probably needs the "
                    "signing key even for reads",
                    host,
                    err,
                )
                return "signing_required"
            _LOGGER.warning("%s answered with an error: %s", host, err)
            return "no_wiz_light"
        except Exception:
            _LOGGER.exception("Unexpected error talking to %s", host)
            return "unknown"
        finally:
            await bulb.async_close()

        if not mac:
            _LOGGER.warning("%s answered but did not report a MAC: %s", host, config)
            return "no_wiz_light"
        self._host = host
        self._mac = mac
        self._name = name_from_bulb_type_and_mac(bulbtype, mac)
        self._requires_signing = signing_key is not None or firmware_requires_signing(
            config
        )
        _LOGGER.debug(
            "%s is %s (%s), firmware %s, signed requests required: %s",
            host,
            mac,
            bulbtype.name,
            bulbtype.fw_version,
            self._requires_signing,
        )
        return None

    async def _async_probe(self, host: str, signing_key: str | None) -> dict[str, str]:
        """Identify the device, using the key only if the device insists on it."""
        error = await self._async_try_probe(host, None)
        if error == "signing_required" and signing_key:
            error = await self._async_try_probe(host, signing_key)
        return {"base": error} if error else {}

    async def _async_verify_signing_key(self, host: str, key: str) -> dict[str, str]:
        """Check the key with a signed read. Returns form errors."""
        bulb = wizlight(host, signing_key=key)
        try:
            await bulb.send({"method": "getPilot", "params": {}})
        except (WizLightTimeOutError, WizLightConnectionError, OSError) as err:
            if is_timeout(err):
                return {"base": "bulb_time_out"}
            if is_socket_error(err):
                _LOGGER.warning("Could not reach %s: %s", host, err.__cause__ or err)
                return {"base": "cannot_connect"}
            if is_signature_rejected(err) or is_unsigned_rejected(err):
                _LOGGER.warning("%s rejected the signing key: %s", host, err)
                return {"base": "invalid_signing_key"}
            _LOGGER.warning(
                "%s gave an unexpected answer (code %s) to a signed request: %s",
                host,
                error_code(err),
                err,
            )
            return {"base": "no_wiz_light"}
        finally:
            await bulb.async_close()
        return {}

    def _existing_signing_key(self) -> str | None:
        """Return a key from another entry; WiZ uses one key per home."""
        for entry in self._async_current_entries(include_ignore=False):
            if key := entry.data.get(CONF_SIGNING_KEY):
                return key
        return None

    # ------------------------------------------------------------------
    # Finishing
    # ------------------------------------------------------------------

    def _async_create(self) -> ConfigFlowResult:
        assert self._host is not None and self._name is not None
        data: dict[str, Any] = {CONF_HOST: self._host}
        if self._signing_key:
            data[CONF_SIGNING_KEY] = self._signing_key
        return self.async_create_entry(title=self._name, data=data)

    async def _async_finish(self) -> ConfigFlowResult:
        """Create the entry, asking for a signing key first if the device needs one."""
        assert self._host is not None
        if not self._requires_signing:
            return self._async_create()
        if not self._signing_key and (existing := self._existing_signing_key()):
            if not await self._async_verify_signing_key(self._host, existing):
                self._signing_key = existing
                return self._async_create()
        if self._signing_key:
            if errors := await self._async_verify_signing_key(
                self._host, self._signing_key
            ):
                return self._async_show_signing_key_form(errors)
            return self._async_create()
        return self._async_show_signing_key_form({})

    def _async_show_signing_key_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="signing_key",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Optional(CONF_SIGNING_KEY): _KEY_SELECTOR}),
                {CONF_SIGNING_KEY: self._signing_key or self._existing_signing_key()},
            ),
            errors=errors,
            description_placeholders={
                "name": self._name or "",
                "host": self._host or "",
            },
        )

    async def async_step_signing_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the signing key newer firmware requires."""
        if user_input is None:
            return self._async_show_signing_key_form({})
        key = normalize_signing_key(user_input.get(CONF_SIGNING_KEY))
        if key is None:
            # The user chose to go without. State will work, control won't,
            # and setup will raise a reauth to remind them.
            self._signing_key = None
            return self._async_create()
        if not is_valid_signing_key(key):
            return self._async_show_signing_key_form(
                {CONF_SIGNING_KEY: "invalid_key_format"}
            )
        assert self._host is not None
        if errors := await self._async_verify_signing_key(self._host, key):
            return self._async_show_signing_key_form(errors)
        self._signing_key = key
        return self._async_create()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle discovery via dhcp."""
        self._discovered_device = DiscoveredBulb(
            discovery_info.ip, discovery_info.macaddress
        )
        return await self._async_handle_discovery()

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, str]
    ) -> ConfigFlowResult:
        """Handle integration discovery."""
        self._discovered_device = DiscoveredBulb(
            discovery_info["ip_address"], discovery_info["mac_address"]
        )
        return await self._async_handle_discovery()

    async def _async_handle_discovery(self) -> ConfigFlowResult:
        """Handle any discovery."""
        device = self._discovered_device
        assert device is not None
        _LOGGER.debug("Discovered device: %s", device)
        await self.async_set_unique_id(device.mac_address)
        self._abort_if_unique_id_configured(updates={CONF_HOST: device.ip_address})
        await self._async_probe_discovered_or_abort()
        return await self.async_step_discovery_confirm()

    async def _async_probe_discovered_or_abort(self) -> None:
        device = self._discovered_device
        assert device is not None
        if errors := await self._async_probe(device.ip_address, None):
            _LOGGER.info(
                "Discovered WiZ device %s at %s but could not set it up: %s",
                device.mac_address,
                device.ip_address,
                errors["base"],
            )
            raise AbortFlow("cannot_connect")

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery."""
        if user_input is not None or not onboarding.async_is_onboarded(self.hass):
            # Make sure the device is still there and update the name if the
            # firmware has auto updated since discovery.
            await self._async_probe_discovered_or_abort()
            if not onboarding.async_is_onboarded(self.hass):
                self._signing_key = self._existing_signing_key()
                return self._async_create()
            return await self._async_finish()

        placeholders = {"name": self._name or "", "host": self._host or ""}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders=placeholders,
        )

    # ------------------------------------------------------------------
    # User initiated
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = (user_input.get(CONF_HOST) or "").strip()
            key = normalize_signing_key(user_input.get(CONF_SIGNING_KEY))
            if key is not None and not is_valid_signing_key(key):
                errors[CONF_SIGNING_KEY] = "invalid_key_format"
            elif not host:
                self._signing_key = key
                return await self.async_step_pick_device()
            elif not is_ip_address(host):
                errors[CONF_HOST] = "no_ip"
            elif not (errors := await self._async_probe(host, key)):
                assert self._mac is not None
                await self.async_set_unique_id(self._mac, raise_on_progress=False)
                updates: dict[str, Any] = {CONF_HOST: host}
                if key:
                    updates[CONF_SIGNING_KEY] = key
                self._abort_if_unique_id_configured(updates=updates)
                self._signing_key = key
                return await self._async_finish()

        schema = vol.Schema(
            {
                vol.Optional(CONF_HOST, default=""): str,
                vol.Optional(CONF_SIGNING_KEY): _KEY_SELECTOR,
            }
        )
        suggested = user_input or {CONF_SIGNING_KEY: self._existing_signing_key()}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the step to pick discovered device."""
        if user_input is not None:
            device = self._discovered_devices[user_input[CONF_DEVICE]]
            await self.async_set_unique_id(device.mac_address, raise_on_progress=False)
            self._abort_if_unique_id_configured(updates={CONF_HOST: device.ip_address})
            if errors := await self._async_probe(device.ip_address, self._signing_key):
                return self.async_abort(
                    reason="cannot_connect_reason",
                    description_placeholders={
                        "host": device.ip_address,
                        "reason": errors["base"],
                    },
                )
            return await self._async_finish()

        current_unique_ids = self._async_current_ids(include_ignore=False)
        current_hosts = {
            entry.data[CONF_HOST]
            for entry in self._async_current_entries(include_ignore=False)
        }
        discovered_devices = await async_discover_devices(
            self.hass, DISCOVER_SCAN_TIMEOUT, sweep=True
        )
        self._discovered_devices = {
            device.mac_address: device for device in discovered_devices
        }
        devices_name = {
            mac: f"{DEFAULT_NAME} {_short_mac(mac)} ({device.ip_address})"
            for mac, device in self._discovered_devices.items()
            if mac not in current_unique_ids and device.ip_address not in current_hosts
        }
        # Check if there is at least one device
        if not devices_name:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(devices_name)}),
        )

    # ------------------------------------------------------------------
    # Reconfigure / reauth
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the IP address and/or signing key of a configured device."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = (user_input.get(CONF_HOST) or "").strip()
            key = normalize_signing_key(user_input.get(CONF_SIGNING_KEY))
            if key is not None and not is_valid_signing_key(key):
                errors[CONF_SIGNING_KEY] = "invalid_key_format"
            elif not is_ip_address(host):
                errors[CONF_HOST] = "no_ip"
            elif not (errors := await self._async_probe(host, key)):
                assert self._mac is not None
                # raise_on_progress=False: a reauth for the same device may be
                # open, and older Home Assistant releases abort on that.
                await self.async_set_unique_id(self._mac, raise_on_progress=False)
                self._abort_if_unique_id_mismatch()
                if self._requires_signing and key:
                    errors = await self._async_verify_signing_key(host, key)
                if not errors:
                    data: dict[str, Any] = {**entry.data, CONF_HOST: host}
                    data.pop(CONF_SIGNING_KEY, None)
                    if key:
                        data[CONF_SIGNING_KEY] = key
                    return self.async_update_reload_and_abort(entry, data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_SIGNING_KEY): _KEY_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or entry.data
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a device that needs a (new) signing key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the signing key."""
        entry = self._get_reauth_entry()
        host = entry.data[CONF_HOST]
        errors: dict[str, str] = {}
        if user_input is not None:
            key = normalize_signing_key(user_input.get(CONF_SIGNING_KEY))
            if key is None:
                errors[CONF_SIGNING_KEY] = "key_required"
            elif not is_valid_signing_key(key):
                errors[CONF_SIGNING_KEY] = "invalid_key_format"
            elif not (errors := await self._async_verify_signing_key(host, key)):
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_SIGNING_KEY: key}
                )

        suggested = entry.data.get(CONF_SIGNING_KEY) or self._existing_signing_key()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_SIGNING_KEY): _KEY_SELECTOR}),
                {CONF_SIGNING_KEY: suggested},
            ),
            errors=errors,
            description_placeholders={"name": entry.title, "host": host},
        )
