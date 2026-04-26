import json

import pytest
import respx
from httpx import Response

from gateway.platforms.line.reply import (
    LineReplyClient,
    build_quick_reply_button_message,
    PENDING_REPLY_TEXT,
    EXPIRED_REPLY_TEXT,
    ALREADY_DELIVERED_TEXT,
)


def test_build_quick_reply_button_message_carries_request_id():
    msg = build_quick_reply_button_message(
        text="🤔 思考中...",
        button_label="📋 點此查看答案",
        request_id="rid-123",
    )
    assert msg["type"] == "text"
    assert msg["text"] == "🤔 思考中..."
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
