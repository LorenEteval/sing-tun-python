# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration and lifecycle, compatible with CPython 3.8+."""

import json
import math
import sys
from dataclasses import asdict, dataclass, field
from copy import deepcopy
from typing import Any, Dict

from . import _native


@dataclass(frozen=True)
class Config:
    """SOCKS bridge configuration with native sing-tun option dictionaries."""

    proxy: str = field(repr=False)
    stack: str = "gvisor"
    tun_options: Dict[str, Any] = field(default_factory=dict)
    stack_options: Dict[str, Any] = field(default_factory=dict)
    log_level: str = "error"
    network_interface: str = ""
    max_sessions: int = 1024
    udp_timeout: float = 60
    connect_timeout: float = 10
    tcp_idle_timeout: float = 300

    def __post_init__(self):
        for name in ("proxy", "stack", "log_level", "network_interface"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(name + " must be a string")
        for name in ("tun_options", "stack_options"):
            value = getattr(self, name)
            if not isinstance(value, dict) or any(
                not isinstance(k, str) for k in value
            ):
                raise TypeError(name + " must be a dictionary with string keys")
            object.__setattr__(self, name, deepcopy(value))
        if isinstance(self.max_sessions, bool) or not isinstance(
            self.max_sessions, int
        ):
            raise TypeError("max_sessions must be an integer")
        for name in ("udp_timeout", "connect_timeout", "tcp_idle_timeout"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(name + " must be a finite number")
        try:
            _native.validate(self._json())
        except RuntimeError as error:
            raise ValueError(str(error)) from None

    def _json(self):
        return json.dumps(asdict(self), allow_nan=False)


def _milliseconds(timeout):
    if timeout is None:
        return -1
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
        or timeout > 86400
    ):
        raise ValueError("timeout must be None or 0 to 86400 seconds")
    return int(timeout * 1000)


class Engine:
    """One-shot engine. Constructing it validates but does not open the TUN."""

    def __init__(self, config):
        if not isinstance(config, Config):
            raise TypeError("config must be Config")
        self._impl = _native.Engine(config._json())

    def start(self):
        """Open/start the device and stack synchronously; propagate startup errors."""
        self._impl.start()
        return self

    @property
    def status(self):
        return json.loads(self._impl.snapshot())

    @property
    def ready(self):
        return self.status["state"] == "ready"

    @property
    def device_name(self):
        """Actual upstream device name after opening; empty before startup."""
        return self.status.get("device_name", "")

    def stop(self):
        """Request cooperative shutdown; safe concurrently with wait/close."""
        self._impl.stop()

    def wait(self, timeout=None):
        """Return False on timeout; raise if a terminated engine failed."""
        done = self._impl.wait(_milliseconds(timeout))
        if done and self.status["error"]:
            raise RuntimeError(self.status["error"])
        return done

    def close(self, timeout=5):
        """Stop and release the handle after cleanup, or retain it on timeout."""
        if not self._impl.close(_milliseconds(timeout)):
            raise TimeoutError(
                "native cleanup is incomplete; retry close or terminate the exact child"
            )
        if self.status["error"]:
            raise RuntimeError(self.status["error"])

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


def capabilities():
    supported = (
        sys.platform == "win32"
        or sys.platform.startswith("linux")
        or sys.platform == "darwin"
    )
    return {
        "stacks": ("gvisor", "system", "mixed") if supported else (),
        "host_managed_default": False,
        "upstream_options": ("tun_options", "stack_options"),
        "dns": "SOCKS transit by default; upstream device DNS options available",
        "icmp": "upstream local echo replies; no SOCKS remote ICMP forwarding",
        "file_descriptor": (
            "borrowed, duplicated on Unix"
            if sys.platform.startswith("linux") or sys.platform == "darwin"
            else False
        ),
        "one_active_engine_per_process": True,
    }
