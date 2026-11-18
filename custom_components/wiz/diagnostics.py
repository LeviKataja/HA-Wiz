"""Diagnostics support for WiZ."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_SIGNING_KEY
from .coordinator import WizConfigEntry

TO_REDACT = {"roomId", "homeId", "hmac", CONF_SIGNING_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WizConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "requires_signing": data.requires_signing,
        "signing_key_configured": bool(entry.data.get(CONF_SIGNING_KEY)),
        "data": async_redact_data(data.bulb.diagnostics, TO_REDACT),
    }
