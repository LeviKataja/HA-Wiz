"""Fixtures for the WiZ tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Coroutine, Generator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_socket
from pywizlight import bulb as pywiz_bulb
from pywizlight.push_manager import PushManager

from .fake_bulb import FakeBulb

KEY = "00112233445566778899aabbccddeeff"
WRONG_KEY = "ffeeddccbbaa99887766554433221100"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/wiz instead of the core integration."""


@pytest.fixture(autouse=True)
def allow_loopback_udp(socket_enabled: None) -> None:
    """The fake devices live on 127.0.0.x; let the code talk to them."""
    # Explicit addresses: older pytest-socket releases do not understand CIDR.
    pytest_socket.socket_allow_hosts(
        [f"127.0.0.{i}" for i in range(1, 32)], allow_unix_socket=True
    )


@pytest.fixture
def expected_lingering_timers() -> bool:
    """pywizlight re-arms its push registration timer with call_later."""
    return True


@pytest.fixture(autouse=True)
def fast_pywizlight() -> Generator[None]:
    """Shrink pywizlight's 13 second retry window so failure tests are quick."""
    with (
        patch.object(pywiz_bulb, "TIMEOUT", 1.0),
        patch.object(pywiz_bulb, "FIRST_SEND_INTERVAL", 0.1),
        patch.object(pywiz_bulb, "MAX_BACKOFF", 0.3),
        patch("custom_components.wiz.discovery.SWEEP_TIMEOUT", 0.6),
    ):
        yield


@pytest.fixture(autouse=True)
def no_broadcast_discovery() -> Generator[list]:
    """Never broadcast on the test machine's real network."""
    found: list = []

    async def _find(*args: Any, **kwargs: Any) -> list:
        return list(found)

    with patch("custom_components.wiz.discovery.find_wizlights", side_effect=_find):
        yield found


@pytest.fixture(autouse=True)
def reset_push_manager() -> Generator[None]:
    """pywizlight keeps its push listener in a process-wide singleton."""
    yield
    manager = PushManager._manager
    if manager is not None and manager.push_transport is not None:
        manager.push_transport.close()
    PushManager._manager = None


@pytest.fixture
async def make_bulb() -> AsyncGenerator[Callable[..., Coroutine[Any, Any, FakeBulb]]]:
    """Start fake devices on 127.0.0.x."""
    bulbs: list[FakeBulb] = []

    async def _make(
        ip: str = "127.0.0.2", mac: str = "a8bb50aabb01", **kwargs: Any
    ) -> FakeBulb:
        bulb = FakeBulb(ip, mac, **kwargs)
        await bulb.start()
        bulbs.append(bulb)
        return bulb

    yield _make
    for bulb in bulbs:
        bulb.close()
