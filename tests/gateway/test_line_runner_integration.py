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


def test_line_in_cron_platform_map():
    """Cron delivery to LINE must be registered or cronjob(deliver='line') silently fails."""
    from cron import scheduler
    src = inspect.getsource(scheduler._deliver_result)
    assert '"line"' in src or "'line'" in src, (
        "'line' key missing from cron scheduler platform_map — "
        "cronjob(deliver='line') will silently fail"
    )
