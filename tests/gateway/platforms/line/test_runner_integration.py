"""Verify LINE auth lookup keys exist in the gateway runner maps.

Regression test for codex finding #1: Platform.LINE was missing from
GatewayRunner._is_user_authorized's platform_env_map and
platform_allow_all_map, causing every LINE message to fail authorization.
"""
import inspect

from gateway.config import Platform
from gateway.run import GatewayRunner


def test_line_in_authorization_maps():
    src = inspect.getsource(GatewayRunner._is_user_authorized)
    assert "Platform.LINE" in src, (
        "Platform.LINE missing from GatewayRunner._is_user_authorized — "
        "every LINE message will fail authorization"
    )
    assert "LINE_ALLOWED_USERS" in src
    assert "LINE_ALLOW_ALL_USERS" in src
