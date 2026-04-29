"""Tests for LineAdapterConfig (env var loading) and global integration checks."""
import pytest

from gateway.platforms.line import LineAdapterConfig


def test_check_line_requirements_returns_true():
    """check_line_requirements always returns True — httpx and aiohttp are core deps."""
    from gateway.platforms.line import check_line_requirements
    assert check_line_requirements() is True


def test_platform_hints_includes_line():
    """Agent system prompt must know it's on LINE to avoid markdown/formatting issues."""
    from agent.prompt_builder import PLATFORM_HINTS
    assert "line" in PLATFORM_HINTS
    hint = PLATFORM_HINTS["line"]
    assert "LINE" in hint
    assert isinstance(hint, str)
    assert len(hint) > 20


def test_from_env_parses_csv_allowlists(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "t")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "s")
    monkeypatch.setenv("LINE_ALLOWED_USERS", "U1,U2")
    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "C1")
    monkeypatch.setenv("LINE_ALLOWED_ROOMS", "")
    cfg = LineAdapterConfig.from_env()
    assert cfg.allowed_users == ["U1", "U2"]
    assert cfg.allowed_groups == ["C1"]
    assert cfg.allowed_rooms == []


def test_from_env_strips_whitespace(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "t")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "s")
    monkeypatch.setenv("LINE_ALLOWED_USERS", " U1 , U2 , ")
    cfg = LineAdapterConfig.from_env()
    assert cfg.allowed_users == ["U1", "U2"]


def test_from_env_missing_token_raises(monkeypatch):
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "s")
    with pytest.raises(ValueError, match="LINE_CHANNEL_ACCESS_TOKEN"):
        LineAdapterConfig.from_env()


def test_from_env_missing_secret_yields_outbound_only(monkeypatch):
    """Outbound-only mode: LINE_CHANNEL_ACCESS_TOKEN alone is enough for
    Push API + cron deliveries; webhook receiver returns 401 on every
    inbound (verify_signature rejects when secret is empty). Codex review
    #5 P3."""
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "t")
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    cfg = LineAdapterConfig.from_env()
    assert cfg.channel_access_token == "t"
    assert cfg.channel_secret == ""


def test_from_env_reads_optional_overrides(monkeypatch):
    """All optional env overrides should round-trip through from_env()."""
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "t")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "s")
    monkeypatch.setenv("LINE_SLOW_RESPONSE_THRESHOLD", "30")
    monkeypatch.setenv("LINE_CACHE_TTL", "7200")
    monkeypatch.setenv("LINE_REQUIRE_MENTION", "true")
    monkeypatch.setenv("LINE_BOT_DISPLAY_NAME", "Samantha")
    monkeypatch.setenv("LINE_FREE_RESPONSE_GROUPS", "Caaa,Cbbb")
    monkeypatch.setenv("LINE_FREE_RESPONSE_ROOMS", "R111")

    cfg = LineAdapterConfig.from_env()

    assert cfg.slow_response_threshold_seconds == 30.0
    assert cfg.request_cache_ttl_seconds == 7200
    assert cfg.require_mention is True
    assert cfg.bot_display_name == "Samantha"
    assert cfg.free_response_groups == ["Caaa", "Cbbb"]
    assert cfg.free_response_rooms == ["R111"]


def test_from_env_defaults_when_overrides_missing(monkeypatch):
    """from_env() applies sensible defaults when optional overrides are unset."""
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "t")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "s")
    for key in ("LINE_SLOW_RESPONSE_THRESHOLD", "LINE_CACHE_TTL",
                "LINE_REQUIRE_MENTION", "LINE_BOT_DISPLAY_NAME",
                "LINE_FREE_RESPONSE_GROUPS", "LINE_FREE_RESPONSE_ROOMS"):
        monkeypatch.delenv(key, raising=False)

    cfg = LineAdapterConfig.from_env()

    assert cfg.slow_response_threshold_seconds == 45.0  # documented LINE token margin
    assert cfg.request_cache_ttl_seconds == 3600
    assert cfg.require_mention is False
    assert cfg.bot_display_name == ""
    assert cfg.free_response_groups == []
    assert cfg.free_response_rooms == []
