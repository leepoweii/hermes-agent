"""Verify LINE integration points exist in the gateway runner and cron scheduler.

Guards against Platform.LINE being accidentally omitted from authorization
maps (which would silently drop every incoming LINE message) and from the
cron scheduler's platform_map (which would silently ignore deliver='line').
"""
import inspect

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


def test_line_in_cron_known_delivery_platforms():
    """'line' must be in _KNOWN_DELIVERY_PLATFORMS or bare deliver='line' is silently dropped."""
    from cron.scheduler import _KNOWN_DELIVERY_PLATFORMS
    assert "line" in _KNOWN_DELIVERY_PLATFORMS, (
        "'line' missing from _KNOWN_DELIVERY_PLATFORMS — "
        "cronjob(deliver='line') using a home channel will produce no delivery"
    )


def test_line_in_cron_home_target_env_vars():
    """LINE_HOME_CHANNEL must be registered so hermes setup-configured home channels work."""
    from cron.scheduler import _HOME_TARGET_ENV_VARS
    assert "line" in _HOME_TARGET_ENV_VARS, (
        "'line' missing from _HOME_TARGET_ENV_VARS — "
        "LINE_HOME_CHANNEL is unreachable for cron home-channel delivery"
    )
    assert _HOME_TARGET_ENV_VARS["line"] == "LINE_HOME_CHANNEL"
