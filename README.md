# HA-Wiz

A fork of Home Assistant's built-in [WiZ integration](https://www.home-assistant.io/integrations/wiz), packaged as a custom integration. It uses the same `wiz` domain, so it **replaces** the built-in one, and your existing WiZ devices, entity IDs and automations carry over.

> Status: tested against a simulated WiZ device inside Home Assistant 2025.2.5 and 2026.10.4. **Not yet tested against real bulbs.** If something fails, see [Reporting a problem](#reporting-a-problem).

## What is different from the built-in integration

| Problem in core (2026.11.2, `pywizlight==0.6.3`) | What this fork does |
| --- | --- |
| Newer WiZ firmware (answers with `"env":"pro"`) rejects unsigned commands with `-32602 Invalid params`. Core cannot sign requests, so those devices show their state but cannot be controlled. | Uses `pywizlight` 0.6.7, which can sign requests. Setup asks for the signing key when the device needs one. A missing or rejected key raises a re-authentication prompt instead of failing silently. |
| `pywizlight` 0.6.7 changed `bulb.state` to a list, so simply bumping the library version breaks core's code. | All entities are adapted to the new interface. |
| A device `pywizlight` cannot classify (odd module name, colour bulb with no CCT range reported) cannot be added: discovery drops it with only a debug log line, and adding by IP shows "unknown error". | Falls back to a best guess from the module name and logs a warning with what the device reported. |
| Discovery only uses UDP broadcast, which many routers, mesh systems and "multicast enhancement" settings drop. | The manual search ("Add integration", IP left empty) also queries every address on your subnet directly (networks of up to 1024 addresses). |
| `pywizlight` reports a device that never answers as a generic connection error with an empty message (Python's `TimeoutError` is an `OSError`, and `pywizlight` catches `OSError` first). Core then shows "The bulb cannot be connected via WiZ integration" for what is really a timeout. | Timeouts, refused ports, rejected signatures and real device errors each get their own message. |
| One failed status poll marks every entity unavailable, so entities flap on weak Wi-Fi. | Entities keep their last state until 3 polls in a row have failed. |

Background discovery (every 15 minutes), DHCP discovery, push updates, fans, sockets, effects, the occupancy sensor and diagnostics work as in core.

## Install

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/LeviKataja/HA-Wiz`, category **Integration**.
2. Install **WiZ (HA-Wiz)**, then restart Home Assistant.

### Manual

Copy `custom_components/wiz` into your Home Assistant config directory, so you end up with `/config/custom_components/wiz/manifest.json`. Then restart Home Assistant.

After the restart, the log shows `We found a custom integration wiz which has not been tested by Home Assistant`. That's expected: it confirms the fork is the one loaded.

On first start Home Assistant installs `pywizlight==0.6.7`. That needs internet access once.

## Adding devices

**Settings → Devices & services → Add integration → WiZ (HA-Wiz)**

- **IP address:** use this if you know it. A DHCP reservation for each bulb is strongly recommended.
- **Leave the IP empty:** this searches the network, by broadcast and by asking each address directly.
- **Signing key:** only needed for newer firmware. If the device needs one, you are asked for it.

## The signing key

Per [`pywizlight`'s documentation](https://pypi.org/project/pywizlight/0.6.7/), newer firmware only accepts commands signed with a per-home key. The WiZ app can export your home "for local integration" as a JSON file, and the key is its `udp_signing_key` value (32 hex characters). It is the same for every device in a WiZ home, so once one device has it, the fork reuses it for the others.

- The device rejects signed requests whose timestamp is more than about a minute old, so the Home Assistant clock must be correct. Home Assistant OS syncs time automatically.
- You can add a device without the key. Its state still updates, but every command fails with an error saying the key is missing, and Home Assistant shows a re-authentication prompt where you can enter it.
- To change the key or the IP later, use the device's **⋮ → Reconfigure**.
- The key is only sent to devices whose firmware asks for it. Older firmware keeps getting plain requests.

## Reporting a problem

Enable debug logging, reproduce the problem, and include the log lines plus the device's diagnostics (**⋮ → Download diagnostics**; the signing key is redacted):

```yaml
logger:
  default: warning
  logs:
    custom_components.wiz: debug
    pywizlight: debug
```

`pywizlight: debug` logs every packet sent to and received from the bulbs, which is what's needed to see what the firmware actually does.

## What this cannot fix

- **Bulbs on a different VLAN or subnet than Home Assistant.** Discovery doesn't cross subnets. Push updates need the bulbs to reach Home Assistant on UDP port 38900, and control needs Home Assistant to reach the bulbs on UDP port 38899. Adding devices by IP works if your firewall allows both.
- **Home Assistant in Docker without `network_mode: host`.** Same reasons.
- **Wi-Fi client isolation** on the network the bulbs use.
- **Slow detection when a bulb loses power.** Because 3 failed polls are tolerated, it takes about a minute before it shows as unavailable. That's the cost of not flapping.

## Going back to the built-in integration

Delete `custom_components/wiz` (or uninstall it in HACS) and restart. Config entries stay compatible. The built-in integration ignores the stored signing key, so devices that need one can no longer be controlled.

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

`tests/fake_bulb.py` simulates WiZ devices on `127.0.0.x:38899`, both legacy and signed (`"env":"pro"`) firmware, modelled on `pywizlight` 0.6.7's description of that firmware.
