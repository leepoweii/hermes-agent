"""End-to-end dispatch: incoming message -> LLM -> reply."""
import json

import pytest
import respx
from httpx import Response


def _msg_event(reply_token="rt", user="U1", source_type="user", source_id="U1"):
    src = {"type": source_type, "userId": user}
    if source_type == "group":
        src["groupId"] = source_id
    elif source_type == "room":
        src["roomId"] = source_id
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": src,
        "timestamp": 1,
        "message": {"id": "m", "type": "text", "text": "hello"},
    }


@pytest.mark.asyncio
@respx.mock
async def test_disallowed_source_is_silently_dropped(line_adapter_user_only):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    event = _msg_event(user="U_NOT_ALLOWED")
    await line_adapter_user_only.dispatch_event(event)
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_fast_llm_replies_directly(line_adapter_with_fast_llm):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    event = _msg_event(reply_token="rt-1", user="U1")
    await line_adapter_with_fast_llm.dispatch_event(event)
    await line_adapter_with_fast_llm.wait_idle()
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["replyToken"] == "rt-1"
    assert "quickReply" not in sent["messages"][0]


@pytest.mark.asyncio
@respx.mock
async def test_slow_llm_sends_quick_reply_button(line_adapter_with_slow_llm):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    event = _msg_event(reply_token="rt-1", user="U1")
    await line_adapter_with_slow_llm.dispatch_event(event)
    await line_adapter_with_slow_llm.wait_button_sent()
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    quick = sent["messages"][0]["quickReply"]["items"][0]["action"]
    payload = json.loads(quick["data"])
    assert payload["action"] == "show_response"
    assert "request_id" in payload


@pytest.mark.asyncio
@respx.mock
async def test_message_without_reply_token_is_dropped(line_adapter_with_fast_llm):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    event = _msg_event(reply_token="rt-1", user="U1")
    del event["replyToken"]  # simulate missing token
    await line_adapter_with_fast_llm.dispatch_event(event)
    assert not route.called
