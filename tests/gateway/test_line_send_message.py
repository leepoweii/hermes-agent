"""Tests for LINE send_message tool routing."""
import json

import pytest
import respx
from httpx import Response


def test_line_in_send_message_platform_map():
    """Platform map must include 'line' so send_message tool can route to it."""
    import inspect

    from tools import send_message_tool as smt

    # The platform_map lives in _handle_send (send_message_tool delegates to it)
    src = inspect.getsource(smt._handle_send)
    assert '"line"' in src or "'line'" in src


@pytest.mark.asyncio
@respx.mock
async def test_send_line_posts_to_push_api():
    """_send_line() must POST to LINE Push API with correct headers."""
    from tools.send_message_tool import _send_line

    route = respx.post("https://api.line.me/v2/bot/message/push").mock(
        return_value=Response(200, json={})
    )

    class _FakePConfig:
        token = "test-token"

    result = await _send_line(_FakePConfig(), "U123456789", "hello from cron")
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["to"] == "U123456789"
    assert body["messages"][0]["text"] == "hello from cron"
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"
    assert result.get("success") is True


@pytest.mark.asyncio
@respx.mock
async def test_send_line_returns_error_without_token():
    """_send_line() must return an error dict when no token is configured."""
    from tools.send_message_tool import _send_line

    class _NullConfig:
        token = ""

    result = await _send_line(_NullConfig(), "U123", "hi")
    assert "error" in result
    assert "LINE" in result["error"]


@pytest.mark.asyncio
@respx.mock
async def test_adapter_send_image_https_url():
    """send_image() with HTTPS URL sends an image message via Push API."""
    from gateway.platforms.line import LineAdapter, LineAdapterConfig
    from gateway.platforms.base import SendResult

    cfg = LineAdapterConfig(
        channel_access_token="tok",
        channel_secret="sec",
        allowed_users=["U1"],
        allowed_groups=[],
        allowed_rooms=[],
    )
    adapter = LineAdapter.from_config(cfg)

    route = respx.post("https://api.line.me/v2/bot/message/push").mock(
        return_value=Response(200, json={})
    )

    result = await adapter.send_image("U1", "https://example.com/img.png", caption="hi")
    assert isinstance(result, SendResult)
    assert result.success
    body = json.loads(route.calls.last.request.content)
    assert body["to"] == "U1"
    assert body["messages"][0]["type"] == "image"
    assert body["messages"][0]["originalContentUrl"] == "https://example.com/img.png"
    # Caption arrives as a second text message
    assert body["messages"][1]["type"] == "text"
    assert body["messages"][1]["text"] == "hi"


@pytest.mark.asyncio
@respx.mock
async def test_adapter_send_image_http_url_falls_back_to_text():
    """send_image() with non-HTTPS URL degrades to a Push API text message."""
    from gateway.platforms.line import LineAdapter, LineAdapterConfig
    from gateway.platforms.base import SendResult

    cfg = LineAdapterConfig(
        channel_access_token="tok",
        channel_secret="sec",
        allowed_users=["U1"],
        allowed_groups=[],
        allowed_rooms=[],
    )
    adapter = LineAdapter.from_config(cfg)

    route = respx.post("https://api.line.me/v2/bot/message/push").mock(
        return_value=Response(200, json={})
    )

    result = await adapter.send_image("U1", "http://example.com/img.png")
    assert isinstance(result, SendResult)
    assert result.success
    body = json.loads(route.calls.last.request.content)
    # Non-HTTPS → text fallback, not image message
    assert body["messages"][0]["type"] == "text"
    assert "http://example.com/img.png" in body["messages"][0]["text"]


@pytest.mark.asyncio
async def test_adapter_send_image_file_returns_unsupported():
    """send_image_file() returns a non-fatal error — LINE has no local file upload."""
    from gateway.platforms.line import LineAdapter, LineAdapterConfig
    from gateway.platforms.base import SendResult

    cfg = LineAdapterConfig(
        channel_access_token="tok",
        channel_secret="sec",
        allowed_users=[],
        allowed_groups=[],
        allowed_rooms=[],
    )
    adapter = LineAdapter.from_config(cfg)
    result = await adapter.send_image_file("U1", "/tmp/img.png", caption="cap")
    assert isinstance(result, SendResult)
    assert not result.success
    assert result.error and "HTTPS" in result.error
