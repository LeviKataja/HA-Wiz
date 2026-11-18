"""DataUpdateCoordinator for the WiZ integration."""

from dataclasses import dataclass
import logging

from pywizlight import wizlight

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    MAX_CONSECUTIVE_FAILURES,
    REQUEST_REFRESH_DELAY,
    UPDATE_INTERVAL,
    WIZ_EXCEPTIONS,
)
from .utils import is_signature_rejected, is_timeout

_LOGGER = logging.getLogger(__name__)


type WizConfigEntry = ConfigEntry[WizData]


@dataclass
class WizData:
    """Data for the wiz integration."""

    coordinator: "WizCoordinator"
    bulb: wizlight
    scenes: list
    requires_signing: bool = False


class WizCoordinator(DataUpdateCoordinator[float | None]):
    """Poll a WiZ device, tolerating a few dropped polls in a row."""

    config_entry: WizConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: WizConfigEntry,
        bulb: wizlight,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=UPDATE_INTERVAL,
            # We don't want an immediate refresh since the device
            # takes a moment to reflect the state change
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=REQUEST_REFRESH_DELAY, immediate=False
            ),
        )
        self._bulb = bulb
        self._consecutive_failures = 0
        self._has_data = False

    async def _async_update_data(self) -> float | None:
        """Update the WiZ device."""
        ip_address = self._bulb.ip
        try:
            await self._bulb.updateState()
            power: float | None = None
            if self._bulb.power_monitoring is not False:
                power = await self._bulb.get_power()
        except WIZ_EXCEPTIONS as ex:
            if is_signature_rejected(ex):
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="signature_rejected",
                    translation_placeholders={
                        "name": self.config_entry.title,
                        "ip_address": ip_address,
                    },
                ) from ex
            self._consecutive_failures += 1
            if self._has_data and self._consecutive_failures < MAX_CONSECUTIVE_FAILURES:
                _LOGGER.debug(
                    "%s: poll failed (%s/%s), keeping last known state: %s",
                    ip_address,
                    self._consecutive_failures,
                    MAX_CONSECUTIVE_FAILURES,
                    ex,
                )
                return self.data
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={
                    "ip_address": ip_address,
                    "error": "no answer (timeout)"
                    if is_timeout(ex)
                    else str(ex) or type(ex).__name__,
                },
            ) from ex
        if self._consecutive_failures:
            _LOGGER.debug(
                "%s: poll succeeded after %s failure(s)",
                ip_address,
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._has_data = True
        return power

    @callback
    def async_handle_push(self) -> None:
        """Handle a state push from the device."""
        self._consecutive_failures = 0
        self.async_set_updated_data(self.data)
