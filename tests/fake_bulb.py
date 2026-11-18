"""A fake WiZ device speaking the UDP JSON protocol on 127.0.0.x:38899.

It models two firmware generations:

* legacy: answers everything unsigned.
* pro: adds "env":"pro" to every answer, accepts unsigned reads, rejects
  unsigned writes with -32602 and bad signatures with -32605, the behaviour
  pywizlight 0.6.7 documents for newer firmware.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import time
from typing import Any

WIZ_PORT = 38899
PUSH_PORT = 38900

READ_METHODS = {
    "getSystemConfig",
    "getModelConfig",
    "getUserConfig",
    "getPilot",
    "getPower",
}
WRITE_METHODS = {"setPilot", "setState"}
COLOR_KEYS = {"r", "g", "b", "c", "w"}

DEFAULT_MODEL_CONFIG = {
    "ps": 1,
    "pwmFreq": 1000,
    "pwmRange": [0, 100],
    "wcr": 30,
    "nowc": 1,
    "cctRange": [2200, 2700, 6500, 6500],
}


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


class FakeBulb(asyncio.DatagramProtocol):
    """A fake WiZ device."""

    def __init__(
        self,
        ip: str,
        mac: str,
        *,
        module_name: str = "ESP01_SHRGB1C_31",
        fw_version: str = "1.35.0",
        pro: bool = False,
        key: str | None = None,
        model_config: dict[str, Any] | None = DEFAULT_MODEL_CONFIG,
        max_skew: float = 60,
    ) -> None:
        self.ip = ip
        self.mac = mac
        self.module_name = module_name
        self.fw_version = fw_version
        self.pro = pro
        self.key = bytes.fromhex(key) if key else None
        self.model_config = copy.deepcopy(model_config)
        self.max_skew = max_skew
        self.responding = True
        self.received: list[dict[str, Any]] = []
        self.push_target: tuple[str, int] | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.pilot: dict[str, Any] = {
            "mac": mac,
            "rssi": -55,
            "src": "",
            "state": True,
            "sceneId": 0,
            "temp": 2700,
            "dimming": 50,
        }

    # -- asyncio plumbing ----------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = json.loads(data.decode())
        except json.JSONDecodeError:
            return
        self.received.append(msg)
        if not self.responding:
            return
        resp = self._handle(msg)
        if resp is None:
            return
        if self.pro:
            resp["env"] = "pro"
            resp["hmac"] = "ZmFrZQ=="
        assert self.transport is not None
        self.transport.sendto(_compact(resp).encode(), addr)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(
            lambda: self, local_addr=(self.ip, WIZ_PORT)
        )

    def close(self) -> None:
        if self.transport:
            self.transport.close()
            self.transport = None

    # -- helpers for tests ---------------------------------------------------

    def writes(self) -> list[dict[str, Any]]:
        return [m for m in self.received if m.get("method") in WRITE_METHODS]

    def push(self, **changes: Any) -> None:
        """Change state locally (e.g. from the WiZ app) and push it."""
        self._apply(changes)
        self._send_push()

    # -- protocol ------------------------------------------------------------

    def _error(self, method: str, code: int, message: str) -> dict[str, Any]:
        return {"method": method, "id": 1, "error": {"code": code, "message": message}}

    def _signature_error(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Return an error if a signed message has a bad signature."""
        method = msg.get("method", "")
        if "hmac" not in msg:
            return None
        params = msg.get("params", {})
        if self.key is None:
            return self._error(method, -32605, "Security error")
        expected = base64.b64encode(
            hmac.new(self.key, _compact(params).encode(), hashlib.sha256).digest()
        ).decode()
        sig_ts = params.get("sigTs")
        if (
            msg.get("env") != "pro"
            or msg["hmac"] != expected
            or not isinstance(sig_ts, int)
            or abs(time.time() - sig_ts) > self.max_skew
        ):
            return self._error(method, -32605, "Security error")
        return None

    def _handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method", "")
        params = dict(msg.get("params") or {})

        if self.pro:
            if error := self._signature_error(msg):
                return error
            if method in WRITE_METHODS and "hmac" not in msg:
                return self._error(method, -32602, "Invalid params")
        elif "hmac" in msg or "sigTs" in params:
            # Model older firmware as strict about unknown params on writes,
            # so the tests catch the integration signing requests to it.
            if method in WRITE_METHODS:
                return self._error(method, -32602, "Invalid params")
        params.pop("sigTs", None)

        if method == "getSystemConfig":
            return {
                "method": method,
                "id": 1,
                "result": {
                    "mac": self.mac,
                    "homeId": 4242,
                    "roomId": 99,
                    "rgn": "eu",
                    "moduleName": self.module_name,
                    "fwVersion": self.fw_version,
                    "groupId": 0,
                    "ping": 0,
                },
            }
        if method == "getModelConfig":
            if self.model_config is None:
                return self._error(method, -32601, "Method not found")
            return {"method": method, "id": 1, "result": self.model_config}
        if method == "getUserConfig":
            return self._error(method, -32601, "Method not found")
        if method == "getPower":
            return self._error(method, -32601, "Method not found")
        if method == "getPilot":
            return {"method": method, "id": 1, "result": dict(self.pilot)}
        if method in WRITE_METHODS:
            self._apply(params)
            self._send_push()
            return {"method": method, "id": 1, "result": {"success": True}}
        if method == "registration":
            if params.get("register") and params.get("phoneIp"):
                self.push_target = (params["phoneIp"], PUSH_PORT)
            return {
                "method": method,
                "id": 1,
                "result": {"mac": self.mac, "success": True},
            }
        return self._error(method, -32601, "Method not found")

    def _apply(self, params: dict[str, Any]) -> None:
        params = {k: v for k, v in params.items() if k != "devices"}
        if "temp" in params:
            for key in COLOR_KEYS:
                self.pilot.pop(key, None)
            self.pilot["sceneId"] = 0
        if COLOR_KEYS & params.keys():
            self.pilot.pop("temp", None)
            self.pilot["sceneId"] = 0
        if params.get("sceneId"):
            self.pilot.pop("temp", None)
            for key in COLOR_KEYS:
                self.pilot.pop(key, None)
        self.pilot.update(params)
        self.pilot["src"] = params.get("src", "udp")

    def _send_push(self) -> None:
        if not self.push_target or not self.transport:
            return
        push = {"method": "syncPilot", "params": dict(self.pilot)}
        if self.pro:
            push["env"] = "pro"
        self.transport.sendto(_compact(push).encode(), self.push_target)
