"""HTTP endpoint tests for LineAdapter — POST /line/webhook + HMAC validation."""
import base64
import hashlib
import hmac
import json

import pytest
from aiohttp import web

from gateway.platforms.line import LineAdapter, LineAdapterConfig


def _sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


@pytest.fixture
def cfg():
    return LineAdapterConfig(
        channel_access_token="t",
        channel_secret="s",
        allowed_users=["U1"],
        allowed_groups=[],
        allowed_rooms=[],
    )


@pytest.fixture
def line_adapter(cfg):
    return LineAdapter.from_config(cfg)


@pytest.fixture
def app(line_adapter):
    app = web.Application()
    line_adapter.register_routes(app)
    return app


@pytest.mark.asyncio
async def test_post_with_valid_signature_returns_200(aiohttp_client, app):
    client = await aiohttp_client(app)
    body = json.dumps({"events": [], "destination": "U1"}).encode()
    sig = _sign("s", body)
    resp = await client.post("/line/webhook", data=body, headers={"X-Line-Signature": sig})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_post_with_invalid_signature_returns_401(aiohttp_client, app):
    client = await aiohttp_client(app)
    body = json.dumps({"events": []}).encode()
    resp = await client.post("/line/webhook", data=body, headers={"X-Line-Signature": "bad"})
    assert resp.status == 401


@pytest.mark.asyncio
async def test_post_with_missing_signature_returns_401(aiohttp_client, app):
    client = await aiohttp_client(app)
    body = json.dumps({"events": []}).encode()
    resp = await client.post("/line/webhook", data=body)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_get_returns_405(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/line/webhook")
    assert resp.status == 405
