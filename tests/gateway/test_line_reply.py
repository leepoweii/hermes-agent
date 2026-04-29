"""Tests for LineReplyClient — reply, quick-reply button, and loading indicator calls."""
import json

import pytest
import respx
from httpx import Response

from gateway.platforms.line import (
    LineReplyClient,
    build_quick_reply_button_message,
    PENDING_REPLY_TEXT,
    EXPIRED_REPLY_TEXT,
    ALREADY_DELIVERED_TEXT,
)


def test_build_quick_reply_button_message_carries_request_id():
    msg = build_quick_reply_button_message(
        text=PENDING_REPLY_TEXT,
        button_label="📋 Show response",
        request_id="rid-123",
    )
    assert msg["type"] == "text"
    assert msg["text"] == PENDING_REPLY_TEXT
    items = msg["quickReply"]["items"]
    assert len(items) == 1
    action = items[0]["action"]
    assert action["type"] == "postback"
    payload = json.loads(action["data"])
    assert payload["action"] == "show_response"
    assert payload["request_id"] == "rid-123"


@pytest.mark.asyncio
@respx.mock
async def test_reply_sends_post_to_line():
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    client = LineReplyClient(channel_access_token="test-token")
    await client.reply("rt-1", [{"type": "text", "text": "hello"}])
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["replyToken"] == "rt-1"
    assert sent["messages"][0]["text"] == "hello"


def test_known_text_constants_exist():
    assert PENDING_REPLY_TEXT
    assert EXPIRED_REPLY_TEXT
    assert ALREADY_DELIVERED_TEXT


@pytest.mark.asyncio
@respx.mock
async def test_show_loading_suppressed_for_group_id():
    """show_loading() must skip the HTTP call for group/room IDs (LINE limitation)."""
    route = respx.post("https://api.line.me/v2/bot/chat/loading/start").mock(
        return_value=Response(200, json={})
    )
    client = LineReplyClient(channel_access_token="t")
    await client.show_loading("C_group_id_starts_with_C")
    await client.show_loading("R_room_id_starts_with_R")
    await client.show_loading("")
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_show_loading_fires_for_user_id():
    """show_loading() must POST for U-prefixed IDs (DM users) — guards against
    inverted prefix check that would silently disable the typing indicator."""
    route = respx.post("https://api.line.me/v2/bot/chat/loading/start").mock(
        return_value=Response(200, json={})
    )
    client = LineReplyClient(channel_access_token="t")
    await client.show_loading("U_user_id")
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["chatId"] == "U_user_id"


@pytest.mark.asyncio
@respx.mock
async def test_reply_raises_on_non_2xx():
    """LineReplyClient.reply() must surface LINE API errors via raise_for_status —
    documented contract; silently swallowing non-2xx would mask expired reply tokens."""
    import httpx
    respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(400, json={"message": "Invalid reply token"})
    )
    client = LineReplyClient(channel_access_token="t")
    with pytest.raises(httpx.HTTPStatusError):
        await client.reply("expired-token", [{"type": "text", "text": "hi"}])
