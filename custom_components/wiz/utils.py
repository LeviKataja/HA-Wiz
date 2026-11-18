"""WiZ utils."""

import logging
import re
from typing import Any

from pywizlight import BulbType, PilotParser, wizlight
from pywizlight.bulblibrary import BulbClass, Features, KelvinRange
from pywizlight.exceptions import (
    WizLightConnectionError,
    WizLightMethodNotFound,
    WizLightNotKnownBulb,
    WizLightTimeOutError,
)

from .const import DEFAULT_NAME, ERROR_CODE_INVALID_PARAMS, ERROR_CODE_SECURITY

_LOGGER = logging.getLogger(__name__)

_ERROR_CODE_RE = re.compile(r"""["']code["']\s*:\s*(-?\d+)""")

# Used only when the bulb reports a colour bulb but no CCT range at all.
_FALLBACK_KELVIN = KelvinRange(min=2200, max=6500)

_FALLBACK_FEATURES: dict[BulbClass, dict[str, bool]] = {
    BulbClass.RGB: {"brightness": True, "color": True, "color_tmp": True},
    BulbClass.TW: {"brightness": True, "color": False, "color_tmp": True},
    BulbClass.DW: {"brightness": True, "color": False, "color_tmp": False},
    BulbClass.SOCKET: {"brightness": False, "color": False, "color_tmp": False},
    BulbClass.FANDIM: {
        "brightness": True,
        "color": False,
        "color_tmp": False,
        "fan": True,
        "fan_breeze_mode": True,
        "fan_reverse": True,
    },
    BulbClass.FANTW: {
        "brightness": True,
        "color": False,
        "color_tmp": True,
        "fan": True,
        "fan_breeze_mode": True,
        "fan_reverse": True,
    },
}


def _short_mac(mac: str) -> str:
    """Get the short mac address from the full mac."""
    return mac.replace(":", "").upper()[-6:]


def name_from_bulb_type_and_mac(bulb_type: BulbType, mac: str) -> str:
    """Generate a name from bulb_type and mac."""
    if bulb_type.bulb_type == BulbClass.RGB:
        if bulb_type.white_channels == 2:
            description = "RGBWW Tunable"
        else:
            description = "RGBW Tunable"
    else:
        description = bulb_type.bulb_type.value
    return f"{DEFAULT_NAME} {description} {_short_mac(mac)}"


def first_state(bulb: wizlight) -> PilotParser | None:
    """Return the state of the first (or only) head of the device."""
    states = bulb.state
    if not states:
        return None
    return states[0]


def normalize_signing_key(value: Any) -> str | None:
    """Clean up a pasted signing key. Returns None for an empty value."""
    if not isinstance(value, str):
        return None
    key = "".join(value.split()).strip("\"'").lower()
    return key or None


def is_valid_signing_key(key: str) -> bool:
    """Check that the key looks like the hex udp_signing_key the WiZ app exports."""
    if len(key) < 32 or len(key) % 2:
        return False
    try:
        bytes.fromhex(key)
    except ValueError:
        return False
    return True


def firmware_requires_signing(response: dict[str, Any] | None) -> bool:
    """Return True if a bulb response comes from firmware that wants signed requests."""
    return bool(response) and response.get("env") == "pro"


def error_code(err: BaseException) -> int | None:
    """Pull the JSON-RPC error code out of a pywizlight error, if there is one.

    pywizlight only exposes the bulb's error object through the exception text
    ("Error recieved: {'code': -32602, 'message': 'Invalid params'}").
    """
    if not isinstance(err, WizLightConnectionError):
        return None
    if (match := _ERROR_CODE_RE.search(str(err))) is None:
        return None
    return int(match.group(1))


def is_timeout(err: BaseException) -> bool:
    """Return True if a pywizlight error is really a timeout.

    pywizlight's send() catches OSError before asyncio.TimeoutError, and on
    Python 3.11+ TimeoutError is a subclass of OSError, so a device that never
    answers surfaces as WizLightConnectionError('') instead of
    WizLightTimeOutError. Core shows "cannot be connected via WiZ integration"
    for what is just a timeout because of this.
    """
    if isinstance(err, WizLightTimeOutError | TimeoutError):
        return True
    return isinstance(err, WizLightConnectionError) and isinstance(
        err.__cause__, TimeoutError
    )


def is_socket_error(err: BaseException) -> bool:
    """Return True for a socket-level failure (e.g. ICMP port unreachable)."""
    if is_timeout(err):
        return False
    if isinstance(err, WizLightConnectionError):
        return isinstance(err.__cause__, OSError)
    return isinstance(err, OSError)


def is_signature_rejected(err: BaseException) -> bool:
    """Return True if the bulb rejected a request's signature."""
    return error_code(err) == ERROR_CODE_SECURITY


def is_unsigned_rejected(err: BaseException) -> bool:
    """Return True if the bulb rejected a request as having invalid params.

    On "pro" firmware this is the answer to an unsigned setPilot. It is only
    meaningful together with firmware_requires_signing().
    """
    return error_code(err) == ERROR_CODE_INVALID_PARAMS


def _is_bulb_error_response(err: WizLightConnectionError) -> bool:
    """Return True if the error is the bulb answering with an error object.

    pywizlight raises WizLightConnectionError both for socket errors (chained
    from an OSError) and for error responses (not chained).
    """
    return not isinstance(err.__cause__, OSError)


def _classify_module_name(module_name: str) -> tuple[BulbClass, bool, bool]:
    """Classify a device from its module name, tolerating odd formats.

    Returns (bulb class, supports effects, dual head).
    """
    upper = module_name.upper()
    parts = upper.split("_")
    identifier = parts[1] if len(parts) > 1 else upper
    dual_head = "DH" in identifier
    if "RGB" in identifier:
        return BulbClass.RGB, True, dual_head
    if "DDTW" in identifier:
        return BulbClass.FANTW, True, dual_head
    if "TW" in identifier:
        return BulbClass.TW, True, dual_head
    if "SOCKET" in identifier:
        return BulbClass.SOCKET, False, dual_head
    if "FANDIM" in identifier:
        return BulbClass.FANDIM, False, dual_head
    return BulbClass.DW, ("DH" in identifier or "SH" in identifier), dual_head


async def async_get_bulb_type(bulb: wizlight) -> BulbType:
    """Return the bulb type, falling back to a best guess for unknown devices.

    Core gives up on a device when pywizlight cannot classify it, which makes it
    impossible to add. We fall back to a guess from the module name so the
    device can at least be switched and dimmed, and log what it reported.
    """
    try:
        return await bulb.get_bulbtype()
    except (WizLightNotKnownBulb, WizLightMethodNotFound) as err:
        reason: Exception = err
    except WizLightConnectionError as err:
        if (
            not _is_bulb_error_response(err)
            or is_signature_rejected(err)
            or is_unsigned_rejected(err)
        ):
            raise
        reason = err

    config = await bulb.getBulbConfig()
    result: dict[str, Any] = config.get("result") or {}
    module_name = str(result.get("moduleName") or "")
    model_result: dict[str, Any] = {}
    if bulb.modelConfig and isinstance(bulb.modelConfig.get("result"), dict):
        model_result = bulb.modelConfig["result"]

    bulb_class, effect, dual_head = _classify_module_name(module_name)
    features = Features(
        **_FALLBACK_FEATURES[bulb_class], dual_head=dual_head, effect=effect
    )

    kelvin_range: KelvinRange | None = None
    if bulb.extwhiteRange:
        kelvin_range = KelvinRange(
            min=int(min(bulb.extwhiteRange)), max=int(max(bulb.extwhiteRange))
        )
    elif features.color_tmp:
        kelvin_range = _FALLBACK_KELVIN

    white_channels = model_result.get("nowc")
    white_to_color_ratio = model_result.get("wcr")
    if "drvConf" in result and isinstance(result["drvConf"], list | tuple):
        drv_conf = list(result["drvConf"])
        if len(drv_conf) >= 2:
            white_to_color_ratio = white_to_color_ratio or drv_conf[0]
            white_channels = white_channels or drv_conf[1]

    bulb_type = BulbType(
        bulb_type=bulb_class,
        name=module_name or None,
        features=features,
        kelvin_range=kelvin_range,
        fw_version=result.get("fwVersion"),
        white_channels=white_channels,
        white_to_color_ratio=white_to_color_ratio,
        fan_speed_range=bulb.fanSpeedRange,
    )
    _LOGGER.warning(
        "pywizlight could not identify the WiZ device at %s (module %r): %s. "
        "Treating it as %s. If features are missing or wrong, open an issue "
        "with the device's diagnostics attached",
        bulb.ip,
        module_name,
        reason,
        bulb_class.value,
    )
    bulb.bulbtype = bulb_type
    return bulb_type
