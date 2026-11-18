"""WiZ integration (fork of the Home Assistant core integration)."""

import logging
from typing import Any

from pywizlight import PilotParser, wizlight
from pywizlight.exceptions import WizLightConnectionError

from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_SIGNING_KEY,
    DISCOVER_SCAN_TIMEOUT,
    DISCOVERY_INTERVAL,
    DOMAIN,
    OCCUPANCY_SOURCES,
    SIGNAL_WIZ_PIR,
    WIZ_CONNECT_EXCEPTIONS,
)
from .coordinator import WizConfigEntry, WizCoordinator, WizData
from .discovery import async_discover_devices, async_trigger_discovery
from .utils import (
    async_get_bulb_type,
    firmware_requires_signing,
    is_signature_rejected,
    is_timeout,
    is_unsigned_rejected,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.FAN,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, hass_config: ConfigType) -> bool:
    """Set up the wiz integration."""

    async def _async_discovery(*_: Any) -> None:
        async_trigger_discovery(
            hass, await async_discover_devices(hass, DISCOVER_SCAN_TIMEOUT)
        )

    hass.async_create_background_task(_async_discovery(), "wiz-discovery")
    async_track_time_interval(
        hass, _async_discovery, DISCOVERY_INTERVAL, cancel_on_shutdown=True
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: WizConfigEntry) -> bool:
    """Set up the wiz integration from a config entry."""
    ip_address = entry.data[CONF_HOST]
    signing_key: str | None = entry.data.get(CONF_SIGNING_KEY)
    _LOGGER.debug("Get bulb with IP: %s", ip_address)
    bulb = wizlight(ip_address)
    placeholders = {"name": entry.title, "ip_address": ip_address}
    try:
        # Ask unsigned first: only firmware that answers with "env":"pro"
        # gets signed requests, so a stored key never breaks older firmware.
        try:
            config = await bulb.getBulbConfig()
            requires_signing = firmware_requires_signing(config)
        except WizLightConnectionError as err:
            if not signing_key or not (
                is_unsigned_rejected(err) or is_signature_rejected(err)
            ):
                raise
            # Firmware that wants a signature even for reads.
            requires_signing = True
        if requires_signing and signing_key:
            bulb.signing_key = bytes.fromhex(signing_key)
        await async_get_bulb_type(bulb)
        scenes = await bulb.getSupportedScenes()
        await bulb.getMac()
    except WIZ_CONNECT_EXCEPTIONS as err:
        await bulb.async_close()
        if is_signature_rejected(err):
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="signature_rejected",
                translation_placeholders=placeholders,
            ) from err
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={
                "ip_address": ip_address,
                "error": "no answer (timeout)"
                if is_timeout(err)
                else str(err) or type(err).__name__,
            },
        ) from err

    if bulb.mac != entry.unique_id:
        # The ip address of the bulb has changed and its likely offline
        # and another WiZ device has taken the IP. Avoid setting up
        # since its the wrong device. As soon as the device comes back
        # online the ip will get updated and setup will proceed.
        await bulb.async_close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="unexpected_device",
            translation_placeholders={
                "mac": str(bulb.mac),
                "ip_address": ip_address,
                "expected_mac": str(entry.unique_id),
            },
        )

    coordinator = WizCoordinator(hass, entry, bulb)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await bulb.async_close()
        raise

    if requires_signing and not signing_key:
        _LOGGER.warning(
            "%s (%s) runs firmware that only accepts signed commands. Its state "
            "will update, but it cannot be controlled until you enter the "
            "signing key from the WiZ app",
            entry.title,
            ip_address,
        )
        entry.async_start_reauth(hass)

    async def _async_shutdown_on_stop(event: Event) -> None:
        await bulb.async_close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown_on_stop)
    )

    @callback
    def _async_push_update(states: list[PilotParser | None]) -> None:
        """Receive a push update."""
        _LOGGER.debug(
            "%s: Got push update: %s",
            bulb.mac,
            [state.pilotResult if state else None for state in states],
        )
        coordinator.async_handle_push()
        if any(
            state is not None and state.get_source() in OCCUPANCY_SOURCES
            for state in states
        ):
            async_dispatcher_send(hass, SIGNAL_WIZ_PIR.format(bulb.mac))

    if not await bulb.start_push(_async_push_update):
        _LOGGER.warning(
            "%s: push updates are unavailable (%s); falling back to polling "
            "every %s seconds",
            entry.title,
            bulb.diagnostics.get("push_manager", {}).get("fail_reason"),
            int(coordinator.update_interval.total_seconds())
            if coordinator.update_interval
            else "?",
        )
    bulb.set_discovery_callback(lambda bulb: async_trigger_discovery(hass, [bulb]))

    entry.runtime_data = WizData(
        coordinator=coordinator,
        bulb=bulb,
        scenes=scenes,
        requires_signing=requires_signing,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WizConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.bulb.async_close()
    return unload_ok
