"""Constants for the WiZ integration."""

from datetime import timedelta

from pywizlight.bulb import PIR_SOURCE
from pywizlight.exceptions import (
    WizLightConnectionError,
    WizLightMethodNotFound,
    WizLightNotKnownBulb,
    WizLightTimeOutError,
)

DOMAIN = "wiz"
DEFAULT_NAME = "WiZ"

CONF_SIGNING_KEY = "signing_key"

DISCOVER_SCAN_TIMEOUT = 10
DISCOVERY_INTERVAL = timedelta(minutes=15)

# Unicast sweep used by the manual "find devices" step. Broadcast discovery is
# dropped by a lot of Wi-Fi gear (mesh systems, "multicast enhancement",
# broadcast filtering), so we also ask every address on the local subnet
# directly. Networks bigger than this are skipped.
SWEEP_MAX_HOSTS = 1024
SWEEP_TIMEOUT = 4.0
SWEEP_ROUNDS = 3

UPDATE_INTERVAL = timedelta(seconds=15)
REQUEST_REFRESH_DELAY = 0.35

# WiZ devices drop UDP packets on weak Wi-Fi. Core marks everything unavailable
# on the first failed poll; we keep the last known state until this many polls
# in a row have failed.
MAX_CONSECUTIVE_FAILURES = 3

WIZ_EXCEPTIONS = (
    OSError,
    WizLightTimeOutError,
    TimeoutError,
    WizLightConnectionError,
    ConnectionRefusedError,
)
WIZ_CONNECT_EXCEPTIONS = (WizLightNotKnownBulb, WizLightMethodNotFound, *WIZ_EXCEPTIONS)

SIGNAL_WIZ_PIR = "wiz_pir_{}"
OCCUPANCY_SOURCES = frozenset({PIR_SOURCE, "wfsens"})

# JSON-RPC error codes from firmware that answers with "env":"pro".
# -32602 "Invalid params" is what an unsigned setPilot gets back.
# -32605 "Security error" means a bad signature: wrong key, or a sigTs that
# is too old (host clock off by more than about a minute).
ERROR_CODE_INVALID_PARAMS = -32602
ERROR_CODE_SECURITY = -32605
