"""Host-managed TUN-to-SOCKS5 bridge. Import has no networking side effects."""

from ._native import __upstream_commit__, __upstream_version__, __version__
from .api import Config, Engine, capabilities

__all__ = ["Config", "Engine", "capabilities"]
