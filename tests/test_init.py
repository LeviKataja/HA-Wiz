"""Runtime tests against fake WiZ devices."""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wiz.const import CONF_SIGNING_KEY, DOMAIN
from custom_components.wiz.diagnostics import async_get_config_entry_diagnostics
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_SUPPORTED_COLOR_MODES,
    DOMAIN as LIGHT_DOMAIN,
    ColorMode,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from .conftest import KEY, WRONG_KEY

ENTITY_ID = "light.wiz_rgbw_tunable_aabb01"


def _entry(hass: HomeAssistant, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a8bb50aabb01",
        title="WiZ RGBW Tunable AABB01",
        data={CONF_HOST: "127.0.0.2", **data},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    """Wait for something that happens over real UDP, outside hass's task tracking."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def _reauth_flows(hass: HomeAssistant) -> list:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


async def _call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
    )


async def test_legacy_setup_and_control(hass: HomeAssistant, make_bulb) -> None:
    bulb = await make_bulb()
    entry = _entry(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == 2700
    assert set(state.attributes[ATTR_SUPPORTED_COLOR_MODES]) == {
        ColorMode.COLOR_TEMP,
        ColorMode.RGBW,
    }

    await _call(hass, SERVICE_TURN_OFF)
    await _wait_for(lambda: hass.states.get(ENTITY_ID).state == STATE_OFF)
    assert bulb.writes()[-1]["params"] == {"state": False}
    assert not any("hmac" in msg for msg in bulb.received)


async def test_pro_firmware_signed_control(hass: HomeAssistant, make_bulb) -> None:
    bulb = await make_bulb(pro=True, key=KEY)
    entry = _entry(hass, **{CONF_SIGNING_KEY: KEY})
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert not _reauth_flows(hass)

    await _call(
        hass, SERVICE_TURN_ON, **{ATTR_COLOR_TEMP_KELVIN: 4000, ATTR_BRIGHTNESS: 255}
    )
    await _wait_for(
        lambda: (
            hass.states.get(ENTITY_ID).attributes.get(ATTR_COLOR_TEMP_KELVIN) == 4000
        )
    )
    write = bulb.writes()[-1]
    assert write["env"] == "pro" and "hmac" in write
    assert write["params"]["temp"] == 4000
    assert write["params"]["dimming"] == 100
    assert hass.states.get(ENTITY_ID).attributes[ATTR_BRIGHTNESS] == 255


async def test_pro_firmware_without_key(hass: HomeAssistant, make_bulb) -> None:
    """State works, control fails with an actionable error, and a reauth is raised."""
    bulb = await make_bulb(pro=True, key=KEY)
    entry = _entry(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY_ID).state == STATE_ON
    assert len(_reauth_flows(hass)) == 1

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, SERVICE_TURN_OFF)
    assert err.value.translation_key == "signing_key_required"
    assert bulb.pilot["state"] is True


async def test_stored_key_not_used_on_legacy_firmware(
    hass: HomeAssistant, make_bulb
) -> None:
    """A key is only applied to firmware that asks for signatures."""
    bulb = await make_bulb()
    entry = _entry(hass, **{CONF_SIGNING_KEY: KEY})
    await _setup(hass, entry)
    await _call(hass, SERVICE_TURN_OFF)
    await _wait_for(lambda: hass.states.get(ENTITY_ID).state == STATE_OFF)
    assert not any("hmac" in msg for msg in bulb.received)


async def test_wrong_key_starts_reauth(hass: HomeAssistant, make_bulb) -> None:
    await make_bulb(pro=True, key=KEY)
    entry = _entry(hass, **{CONF_SIGNING_KEY: WRONG_KEY})
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = _reauth_flows(hass)
    assert len(flows) == 1

    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {CONF_SIGNING_KEY: WRONG_KEY}
    )
    assert result["errors"] == {"base": "invalid_signing_key"}
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {CONF_SIGNING_KEY: KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_SIGNING_KEY] == KEY
    assert entry.state is ConfigEntryState.LOADED


async def test_stale_clock_is_reported_as_signature_problem(
    hass: HomeAssistant, make_bulb
) -> None:
    """The device rejects signatures whose timestamp is too far off."""
    await make_bulb(pro=True, key=KEY, max_skew=-1)
    entry = _entry(hass, **{CONF_SIGNING_KEY: KEY})
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1


async def test_poll_failures_tolerated(hass: HomeAssistant, make_bulb) -> None:
    """Two dropped polls keep the last state, the third makes it unavailable."""
    bulb = await make_bulb()
    entry = _entry(hass)
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    # Push updates suppress polling; make sure we are really polling.
    entry.runtime_data.bulb.last_push = -1000.0

    bulb.responding = False
    await coordinator.async_refresh()
    assert hass.states.get(ENTITY_ID).state == STATE_ON
    await coordinator.async_refresh()
    assert hass.states.get(ENTITY_ID).state == STATE_ON
    await coordinator.async_refresh()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE

    bulb.responding = True
    await coordinator.async_refresh()
    assert hass.states.get(ENTITY_ID).state == STATE_ON


async def test_command_timeout_message(hass: HomeAssistant, make_bulb) -> None:
    """A device that stops answering gives a timeout error, not a vague one."""
    bulb = await make_bulb()
    entry = _entry(hass)
    await _setup(hass, entry)
    bulb.responding = False
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, SERVICE_TURN_OFF)
    assert err.value.translation_key == "timeout"


async def test_push_updates(hass: HomeAssistant, make_bulb) -> None:
    """A change made outside Home Assistant shows up without waiting for a poll."""
    bulb = await make_bulb(pro=True, key=KEY)
    entry = _entry(hass, **{CONF_SIGNING_KEY: KEY})
    await _setup(hass, entry)
    await _wait_for(lambda: bulb.push_target is not None)
    registration = next(m for m in bulb.received if m["method"] == "registration")
    assert "hmac" in registration

    bulb.push(state=False)
    await _wait_for(lambda: hass.states.get(ENTITY_ID).state == STATE_OFF)
    bulb.push(state=True, dimming=10)
    await _wait_for(
        lambda: hass.states.get(ENTITY_ID).attributes.get(ATTR_BRIGHTNESS) == 26
    )


async def test_unknown_module_light(hass: HomeAssistant, make_bulb) -> None:
    await make_bulb(module_name="WEIRDMODULE", model_config=None)
    entry = _entry(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.BRIGHTNESS]


async def test_wrong_device_at_ip(hass: HomeAssistant, make_bulb) -> None:
    await make_bulb(mac="a8bb50000000")
    entry = _entry(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unreachable_at_startup(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_reconfigure_sets_key(hass: HomeAssistant, make_bulb) -> None:
    await make_bulb(pro=True, key=KEY)
    entry = _entry(hass)
    await _setup(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: WRONG_KEY}
    )
    assert result["errors"] == {"base": "invalid_signing_key"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data == {CONF_HOST: "127.0.0.2", CONF_SIGNING_KEY: KEY}
    assert entry.state is ConfigEntryState.LOADED


async def test_diagnostics_redact_key(hass: HomeAssistant, make_bulb) -> None:
    await make_bulb(pro=True, key=KEY)
    entry = _entry(hass, **{CONF_SIGNING_KEY: KEY})
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert KEY not in repr(diag)
    assert diag["requires_signing"] is True
    assert diag["signing_key_configured"] is True
