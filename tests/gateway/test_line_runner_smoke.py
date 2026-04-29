"""Smoke tests for LineAdapter connect/disconnect lifecycle and runner startup."""
import logging

import pytest
from aiohttp import web

from gateway.platforms.line import LineAdapter, LineAdapterConfig


@pytest.mark.asyncio
async def test_connect_starts_standalone_server_when_no_app(monkeypatch):
    """Verify connect() with no app argument starts its own listener."""
    monkeypatch.setenv("LINE_WEBHOOK_PORT", "0")  # let OS pick free port
    cfg = LineAdapterConfig(channel_access_token="t", channel_secret="s")
    adapter = LineAdapter.from_config(cfg)
    try:
        result = await adapter.connect()
        assert result is True
        assert adapter._runner is not None
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_connect_with_shared_app_registers_routes():
    """Verify connect(app) injects routes into a caller-owned application."""
    cfg = LineAdapterConfig(channel_access_token="t", channel_secret="s")
    adapter = LineAdapter.from_config(cfg)
    shared_app = web.Application()
    result = await adapter.connect(shared_app)
    assert result is True
    assert adapter._runner is None  # no standalone runner created
    routes = [r.resource.canonical for r in shared_app.router.routes()]
    assert "/line/webhook" in routes
    assert "/line/webhook/health" in routes


@pytest.mark.asyncio
async def test_connect_warns_when_free_response_id_not_in_allowlist(caplog):
    """Operator footgun: free_response_groups contains an ID missing from
    allowed_groups → allowlist drops the message before free-response can fire.
    connect() should log a clear WARNING so operators catch the misconfiguration."""
    cfg = LineAdapterConfig(
        channel_access_token="t",
        channel_secret="s",
        allowed_groups=["Callowed"],
        free_response_groups=["Cunreachable", "Callowed"],  # Cunreachable not in allowlist
    )
    adapter = LineAdapter.from_config(cfg)
    shared_app = web.Application()
    with caplog.at_level(logging.WARNING, logger="gateway.platforms.line"):
        await adapter.connect(shared_app)
    assert any(
        "Cunreachable" in r.message and "free_response_groups" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_connect_warns_when_free_response_room_not_in_allowlist(caplog):
    """Same operator footgun for rooms: free_response_rooms with an ID missing
    from allowed_rooms must emit a WARNING. Guards against the parallel rooms
    branch in connect() being dropped during refactor."""
    cfg = LineAdapterConfig(
        channel_access_token="t",
        channel_secret="s",
        allowed_rooms=["Rallowed"],
        free_response_rooms=["Runreachable", "Rallowed"],  # Runreachable not in allowlist
    )
    adapter = LineAdapter.from_config(cfg)
    shared_app = web.Application()
    with caplog.at_level(logging.WARNING, logger="gateway.platforms.line"):
        await adapter.connect(shared_app)
    assert any(
        "Runreachable" in r.message and "free_response_rooms" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_connect_disconnect_marks_connection_state():
    """connect() should call _mark_connected() so is_connected() returns True;
    disconnect() should call _mark_disconnected() to clear it."""
    cfg = LineAdapterConfig(channel_access_token="t", channel_secret="s")
    adapter = LineAdapter.from_config(cfg)
    shared_app = web.Application()
    await adapter.connect(shared_app)
    assert adapter.is_connected is True
    await adapter.disconnect()
    assert adapter.is_connected is False
