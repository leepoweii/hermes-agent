"""Verify LINE integration points exist in the gateway runner and cron scheduler.

Guards against Platform.LINE being accidentally omitted from authorization
maps (which would silently drop every incoming LINE message) and from the
cron scheduler's platform_map (which would silently ignore deliver='line').
"""
import inspect

from gateway.run import GatewayRunner


def test_line_in_authorization_maps():
    """Source-text guard: the platform_env_map / platform_allow_all_map dicts
    inside ``_is_user_authorized`` are method-local (rebuilt per call), so we
    can't import them as data. Asserting on the source literal text is a
    pragmatic regression guard that catches the most likely failure mode —
    forgetting to add ``Platform.LINE`` to one of the maps when the rest of
    the wiring is in place. A full behavioural test would require building a
    GatewayConfig + GatewayRunner + MessageEvent fixture, an order of
    magnitude more setup for the same coverage."""
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


def test_line_in_setup_wizard_gateway_platforms():
    """LINE must be in setup.py _GATEWAY_PLATFORMS or 'hermes setup gateway' won't offer it.

    The first-run setup wizard (`hermes setup gateway`) iterates this list to build
    the platform checklist. The newer `hermes gateway setup` wizard uses a separate
    `_PLATFORMS` list in gateway.py — both must include LINE, otherwise one entry
    point silently omits it. This test guards the older registry; the gateway.py
    one is exercised by the wizard's import path."""
    from hermes_cli.setup import _GATEWAY_PLATFORMS
    keys = [env_var for _name, env_var, _func in _GATEWAY_PLATFORMS]
    assert "LINE_CHANNEL_ACCESS_TOKEN" in keys, (
        "LINE missing from _GATEWAY_PLATFORMS — "
        "'hermes setup gateway' won't offer LINE in its checklist"
    )


def test_line_in_gateway_platforms_dict_registry():
    """LINE must be in gateway.py _PLATFORMS or 'hermes gateway setup' won't offer it."""
    from hermes_cli.gateway import _PLATFORMS
    keys = [p["key"] for p in _PLATFORMS]
    assert "line" in keys, (
        "LINE missing from _PLATFORMS — "
        "'hermes gateway setup' won't offer LINE in its menu"
    )


def test_line_group_room_chat_type_bypasses_user_allowlist_in_runner():
    """LINE group/room chat_type sources must be auto-authorized at the gateway
    layer because the LINE adapter's own LINE_ALLOWED_GROUPS/ROOMS check
    already validated them before dispatch.

    Regression guard: a previous version of this PR only registered
    LINE_ALLOWED_USERS in `_is_user_authorized`'s `platform_env_map`, which
    meant group/room messages were rejected at the gateway layer even when
    the LINE adapter passed them. Reported by codex review."""
    src = inspect.getsource(GatewayRunner._is_user_authorized)
    assert "Platform.LINE" in src and 'chat_type in {"group", "room"}' in src, (
        "_is_user_authorized must short-circuit LINE group/room sources — "
        "otherwise LINE_ALLOWED_GROUPS/ROOMS messages get gateway-level rejection"
    )
