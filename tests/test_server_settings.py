"""Tests for server-level settings — runs without Wind Terminal.

wind_mcp.server imports handlers which import core.session, which does a
module-level `from WindPy import w`. CI machines have no Wind Terminal, so
stub the WindPy module before importing the server package.
"""

import sys
import types
from unittest.mock import MagicMock

_windpy = types.ModuleType("WindPy")
_windpy.w = MagicMock()
sys.modules.setdefault("WindPy", _windpy)

import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_streamable_http_lan_access_no_dns_rebinding_protection():
    """
    FastMCP auto-enables DNS rebinding protection when constructed with a
    localhost host (default 127.0.0.1). Its middleware then rejects every
    request whose Host header is not localhost (HTTP 421), which breaks the
    documented LAN usage of `wind-mcp --http`.

    The server is constructed with host="0.0.0.0", so the SDK must leave
    transport_security disabled and LAN clients connect with their real
    Host header.
    """
    from wind_mcp.server import mcp

    assert mcp.settings.transport_security is None
