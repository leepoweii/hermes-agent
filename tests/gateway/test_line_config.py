import pytest

from gateway.platforms.line import LineAdapterConfig


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


def test_from_env_missing_secret_raises(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "t")
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    with pytest.raises(ValueError, match="LINE_CHANNEL_SECRET"):
        LineAdapterConfig.from_env()
