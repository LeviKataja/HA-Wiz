"""The wiz integration discovery."""

import asyncio
from dataclasses import asdict
import ipaddress
import json
import logging
from typing import Any

from pywizlight.discovery import DiscoveredBulb, find_wizlights

from homeassistant import config_entries
from homeassistant.components import network
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import discovery_flow

from .const import DOMAIN, SWEEP_MAX_HOSTS, SWEEP_ROUNDS, SWEEP_TIMEOUT

_LOGGER = logging.getLogger(__name__)

WIZ_PORT = 38899
_SWEEP_MESSAGE = b'{"method":"getSystemConfig","params":{}}'
# Pace the sweep so we do not dump hundreds of packets on the Wi-Fi at once.
_SWEEP_BATCH = 32
_SWEEP_BATCH_PAUSE = 0.05


async def async_discover_devices(
    hass: HomeAssistant, timeout: int, *, sweep: bool = False
) -> list[DiscoveredBulb]:
    """Discover wiz devices by broadcast, and optionally by a unicast sweep."""
    broadcast_addrs = await network.async_get_ipv4_broadcast_addresses(hass)
    targets = [str(address) for address in broadcast_addrs]
    tasks: list[Any] = [find_wizlights(timeout, address) for address in targets]
    if sweep:
        tasks.append(async_sweep_devices(await async_get_sweep_hosts(hass)))

    combined_discoveries: dict[str, DiscoveredBulb] = {}
    for idx, discovered in enumerate(
        await asyncio.gather(*tasks, return_exceptions=True)
    ):
        source = targets[idx] if idx < len(targets) else "unicast sweep"
        if isinstance(discovered, Exception):
            _LOGGER.debug("Scanning %s failed with error: %s", source, discovered)
            continue
        if isinstance(discovered, BaseException):
            raise discovered from None
        _LOGGER.debug("%s found %s device(s)", source, len(discovered))
        for device in discovered:
            assert isinstance(device, DiscoveredBulb)
            combined_discoveries[device.ip_address] = device

    return list(combined_discoveries.values())


async def async_get_sweep_hosts(hass: HomeAssistant) -> list[str]:
    """Return every host address on the IPv4 networks Home Assistant uses."""
    hosts: list[str] = []
    seen: set[ipaddress.IPv4Network] = set()
    for adapter in await network.async_get_adapters(hass):
        if not adapter["enabled"]:
            continue
        for ip_info in adapter["ipv4"]:
            own_ip = ip_info["address"]
            try:
                net = ipaddress.IPv4Network(
                    f"{own_ip}/{ip_info['network_prefix']}", strict=False
                )
            except ValueError:
                continue
            if net in seen or net.is_loopback or net.is_link_local:
                continue
            seen.add(net)
            if net.num_addresses > SWEEP_MAX_HOSTS + 2:
                _LOGGER.debug(
                    "Not sweeping %s (%s, %s addresses is too many)",
                    net,
                    adapter["name"],
                    net.num_addresses,
                )
                continue
            hosts.extend(str(host) for host in net.hosts() if str(host) != own_ip)
    return hosts


class _SweepProtocol(asyncio.DatagramProtocol):
    """Collect getSystemConfig answers from WiZ devices."""

    def __init__(self, found: dict[str, DiscoveredBulb]) -> None:
        self._found = found

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            resp = json.loads(data.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(resp, dict):
            return
        result = resp.get("result")
        if not isinstance(result, dict):
            return
        mac = result.get("mac")
        if isinstance(mac, str) and mac:
            if addr[0] not in self._found:
                _LOGGER.debug("Sweep found WiZ device %s at %s", mac, addr[0])
            self._found[addr[0]] = DiscoveredBulb(addr[0], mac)

    def error_received(self, exc: Exception) -> None:
        # Unreachable hosts are expected during a sweep.
        pass


async def async_sweep_devices(
    hosts: list[str],
    timeout: float | None = None,
    rounds: int = SWEEP_ROUNDS,
    port: int = WIZ_PORT,
) -> list[DiscoveredBulb]:
    """Ask every host directly whether it is a WiZ device.

    Works where broadcast discovery does not, because nothing on the path
    has to forward a broadcast. getSystemConfig is a read, which newer
    firmware still answers without a signature.
    """
    if not hosts:
        return []
    if timeout is None:
        timeout = SWEEP_TIMEOUT
    loop = asyncio.get_running_loop()
    found: dict[str, DiscoveredBulb] = {}
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _SweepProtocol(found), local_addr=("0.0.0.0", 0)
    )
    try:
        for _ in range(rounds):
            pending = [host for host in hosts if host not in found]
            for idx, host in enumerate(pending, start=1):
                transport.sendto(_SWEEP_MESSAGE, (host, port))
                if idx % _SWEEP_BATCH == 0:
                    await asyncio.sleep(_SWEEP_BATCH_PAUSE)
            await asyncio.sleep(timeout / rounds)
    finally:
        transport.close()
    return list(found.values())


@callback
def async_trigger_discovery(
    hass: HomeAssistant,
    discovered_devices: list[DiscoveredBulb],
) -> None:
    """Trigger config flows for discovered devices."""
    for device in discovered_devices:
        discovery_flow.async_create_flow(
            hass,
            DOMAIN,
            context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
            data=asdict(device),
        )
