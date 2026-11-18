"""Config flow tests against fake WiZ devices."""

from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wiz.const import CONF_SIGNING_KEY, DOMAIN
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICE, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import KEY, WRONG_KEY


async def _start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_manual_legacy_firmware(hass: HomeAssistant, make_bulb) -> None:
    """Older firmware: no key involved at any point."""
    bulb = await make_bulb()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "WiZ RGBW Tunable AABB01"
    assert result["data"] == {CONF_HOST: "127.0.0.2"}
    assert result["result"].unique_id == "a8bb50aabb01"
    assert not any("hmac" in msg for msg in bulb.received)


async def test_manual_pro_firmware_asks_for_key(hass: HomeAssistant, make_bulb) -> None:
    """Pro firmware: the flow asks for the key, rejects a wrong one, takes the right one."""
    await make_bulb(pro=True, key=KEY)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "signing_key"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SIGNING_KEY: "not hex"}
    )
    assert result["errors"] == {CONF_SIGNING_KEY: "invalid_key_format"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SIGNING_KEY: WRONG_KEY}
    )
    assert result["errors"] == {"base": "invalid_signing_key"}

    # Pasted with quotes, spaces and upper case, as people do.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SIGNING_KEY: f' "{KEY.upper()}" '}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: KEY}


async def test_manual_pro_firmware_key_up_front(hass: HomeAssistant, make_bulb) -> None:
    """A key given in the first step is verified and used without another step."""
    await make_bulb(pro=True, key=KEY)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SIGNING_KEY] == KEY


async def test_pro_firmware_reuses_key_from_other_device(
    hass: HomeAssistant, make_bulb
) -> None:
    """WiZ uses one key per home, so a second device picks up the first one's key."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="a8bb50ffffff",
        data={CONF_HOST: "127.0.0.30", CONF_SIGNING_KEY: KEY},
    ).add_to_hass(hass)
    await make_bulb(pro=True, key=KEY)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SIGNING_KEY] == KEY


async def test_pro_firmware_skip_key(hass: HomeAssistant, make_bulb) -> None:
    """The user may add the device without a key; it is read-only then."""
    await make_bulb(pro=True, key=KEY)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2"}
    )
    assert result["step_id"] == "signing_key"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_SIGNING_KEY not in result["data"]


async def test_unknown_module_name_still_added(hass: HomeAssistant, make_bulb) -> None:
    """Core refuses devices pywizlight cannot classify; we fall back to a guess."""
    await make_bulb(module_name="WEIRDMODULE", model_config=None)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "WiZ Dimmable White AABB01"


async def test_rgb_without_cct_range_still_added(
    hass: HomeAssistant, make_bulb
) -> None:
    """pywizlight raises for a colour bulb with no CCT range; we use a default range."""
    await make_bulb(model_config={"nowc": 2})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "WiZ RGBWW Tunable AABB01"


async def test_port_refused(hass: HomeAssistant) -> None:
    """Nothing listens on 38899 at that address (ICMP port unreachable)."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.9"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_device_not_answering(hass: HomeAssistant, make_bulb) -> None:
    """Packets vanish without an answer (offline device, dropped Wi-Fi traffic)."""
    bulb = await make_bulb()
    bulb.responding = False
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "bulb_time_out"}


async def test_not_an_ip(hass: HomeAssistant) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "wiz.local"}
    )
    assert result["errors"] == {CONF_HOST: "no_ip"}


async def test_pick_device_finds_devices_broadcast_misses(
    hass: HomeAssistant, make_bulb
) -> None:
    """The unicast sweep finds devices when broadcast discovery finds nothing."""
    await make_bulb("127.0.0.2", "a8bb50aabb01")
    await make_bulb("127.0.0.3", "a8bb50aabb02", pro=True, key=KEY)
    hosts = [f"127.0.0.{i}" for i in range(2, 12)]
    with patch(
        "custom_components.wiz.discovery.async_get_sweep_hosts", return_value=hosts
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "", CONF_SIGNING_KEY: KEY}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick_device"
    options = result["data_schema"].schema[CONF_DEVICE].container
    assert set(options) == {"a8bb50aabb01", "a8bb50aabb02"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: "a8bb50aabb02"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: "127.0.0.3", CONF_SIGNING_KEY: KEY}


async def test_pick_device_nothing_found(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.wiz.discovery.async_get_sweep_hosts",
        return_value=["127.0.0.20"],
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: ""}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_integration_discovery_pro_firmware(
    hass: HomeAssistant, make_bulb
) -> None:
    """A discovered pro device leads to the key step after confirmation."""
    await make_bulb(pro=True, key=KEY)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"ip_address": "127.0.0.2", "mac_address": "a8bb50aabb01"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "signing_key"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SIGNING_KEY: KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: KEY}


async def test_existing_device_updates_ip_and_key(
    hass: HomeAssistant, make_bulb
) -> None:
    """Adding a configured device again updates its IP and key instead of failing."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="a8bb50aabb01", data={CONF_HOST: "127.0.0.30"}
    )
    entry.add_to_hass(hass)
    await make_bulb(pro=True, key=KEY)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: KEY}
