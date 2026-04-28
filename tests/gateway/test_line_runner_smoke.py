"""Smoke tests for LineAdapter connect/disconnect lifecycle and runner startup."""
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
