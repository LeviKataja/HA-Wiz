"""WiZ integration entities."""

from abc import abstractmethod
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, Concatenate

from pywizlight import PilotParser
from pywizlight.bulblibrary import BulbType
from pywizlight.exceptions import (
    WizLightConnectionError,
    WizLightMethodNotFound,
    WizLightTimeOutError,
)

from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity, ToggleEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SIGNING_KEY, DOMAIN
from .coordinator import WizCoordinator, WizData
from .utils import first_state, is_signature_rejected, is_timeout, is_unsigned_rejected


class WizEntity(CoordinatorEntity[WizCoordinator], Entity):
    """Representation of WiZ entity."""

    _attr_has_entity_name = True

    def __init__(self, wiz_data: WizData, name: str) -> None:
        """Initialize a WiZ entity."""
        super().__init__(wiz_data.coordinator)
        self._wiz_data = wiz_data
        self._device = wiz_data.bulb
        bulb_type: BulbType = self._device.bulbtype
        self._attr_unique_id = self._device.mac
        model: str | None = None
        hw_version: str | None = None
        if bulb_type.name:
            hw_data = bulb_type.name.split("_")
            board = hw_data.pop(0)
            if hw_data:
                model = hw_data.pop(0)
            hw_version = f"{board} {hw_data[0]}" if hw_data else board
        # Built in one go: DeviceInfo is a TypedDict on older Home Assistant
        # and a dataclass on newer releases, and keyword construction works
        # with both.
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, self._device.mac)},
            name=name,
            manufacturer="WiZ",
            sw_version=bulb_type.fw_version,
            model=model,
            hw_version=hw_version,
        )

    @property
    def _state(self) -> PilotParser | None:
        """Return the current state of the (first head of the) device."""
        return first_state(self._device)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._async_update_attrs()
        super()._handle_coordinator_update()

    @callback
    @abstractmethod
    def _async_update_attrs(self) -> None:
        """Handle updating _attr values."""


def wiz_exception_handler[_WizEntityT: WizEntity, **_P](
    func: Callable[Concatenate[_WizEntityT, _P], Coroutine[Any, Any, None]],
) -> Callable[Concatenate[_WizEntityT, _P], Coroutine[Any, Any, None]]:
    """Turn device errors into errors the user can act on."""

    @wraps(func)
    async def wrapper(self: _WizEntityT, *args: _P.args, **kwargs: _P.kwargs) -> None:
        entry = self.coordinator.config_entry
        placeholders = {"name": entry.title, "ip_address": entry.data[CONF_HOST]}
        try:
            await func(self, *args, **kwargs)
        except (WizLightTimeOutError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="timeout",
                translation_placeholders=placeholders,
            ) from err
        except WizLightMethodNotFound as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_supported"
            ) from err
        except WizLightConnectionError as err:
            if is_timeout(err):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="timeout",
                    translation_placeholders=placeholders,
                ) from err
            if is_signature_rejected(err):
                entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="signature_rejected",
                    translation_placeholders=placeholders,
                ) from err
            if (
                is_unsigned_rejected(err)
                and self._wiz_data.requires_signing
                and not entry.data.get(CONF_SIGNING_KEY)
            ):
                entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="signing_key_required",
                    translation_placeholders=placeholders,
                ) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={**placeholders, "error": str(err)},
            ) from err

    return wrapper


class WizToggleEntity(WizEntity, ToggleEntity):
    """Representation of WiZ toggle entity."""

    @callback
    def _async_update_attrs(self) -> None:
        """Handle updating _attr values."""
        self._attr_is_on = self._device.status

    @wiz_exception_handler
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Instruct the device to turn off."""
        await self._device.turn_off()
        await self.coordinator.async_request_refresh()
