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


def _postback_event(request_id, reply_token="rt-pb"):
    return {
        "type": "postback",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": "U1"},
        "timestamp": 2,
        "postback": {
            "data": json.dumps({"action": "show_response", "request_id": request_id})
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_postback_pending_re_attaches_button(line_adapter_with_fast_llm):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    rid = line_adapter_with_fast_llm._cache.register_pending()
    await line_adapter_with_fast_llm.dispatch_event(_postback_event(rid))
    sent = json.loads(route.calls.last.request.content)
    msg = sent["messages"][0]
    assert "quickReply" in msg  # button must be re-attached so user can retry
    button_data = json.loads(msg["quickReply"]["items"][0]["action"]["data"])
    assert button_data["request_id"] == rid
    assert button_data["action"] == "show_response"


@pytest.mark.asyncio
@respx.mock
async def test_postback_ready_delivers_answer_and_marks_delivered(line_adapter_with_fast_llm):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    rid = line_adapter_with_fast_llm._cache.register_pending()
    line_adapter_with_fast_llm._cache.set_ready(rid, "the answer")
    await line_adapter_with_fast_llm.dispatch_event(_postback_event(rid))
    sent = json.loads(route.calls.last.request.content)
    assert sent["messages"][0]["text"] == "the answer"
    assert line_adapter_with_fast_llm._cache.get(rid).state.value == "delivered"


@pytest.mark.asyncio
@respx.mock
async def test_postback_delivered_replies_already_done(line_adapter_with_fast_llm):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    rid = line_adapter_with_fast_llm._cache.register_pending()
    line_adapter_with_fast_llm._cache.set_ready(rid, "x")
    line_adapter_with_fast_llm._cache.mark_delivered(rid)
    await line_adapter_with_fast_llm.dispatch_event(_postback_event(rid))
    sent = json.loads(route.calls.last.request.content)
    assert "Already replied" in sent["messages"][0]["text"]


@pytest.mark.asyncio
@respx.mock
async def test_postback_unknown_request_id_says_expired(line_adapter_with_fast_llm):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    await line_adapter_with_fast_llm.dispatch_event(_postback_event("never-existed"))
    sent = json.loads(route.calls.last.request.content)
    assert "expired" in sent["messages"][0]["text"].lower()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("data_value", ['"just a string"', "null", "[1,2,3]", "42"])
async def test_postback_non_dict_payload_treated_as_unknown(line_adapter_with_fast_llm, data_value):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    event = {
        "type": "postback",
        "replyToken": "rt-pb",
        "source": {"type": "user", "userId": "U1"},
        "timestamp": 2,
        "postback": {"data": data_value},
    }
    # Should not raise; treat as unknown action and silently ignore
    await line_adapter_with_fast_llm.dispatch_event(event)
    # Since action != "show_response" path returns silently, route is NOT called
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_llm_exception_replies_with_error(line_adapter_with_fast_llm):
    """When LLM raises, user gets an error message instead of silence."""
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )

    async def failing_llm(text, source, event=None):
        raise RuntimeError("oops")

    line_adapter_with_fast_llm._llm_call = failing_llm
    event = _msg_event(reply_token="rt-err", user="U1")
    await line_adapter_with_fast_llm.dispatch_event(event)
    await line_adapter_with_fast_llm.wait_idle()
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert "processing error" in sent["messages"][0]["text"].lower()


@pytest.mark.asyncio
@respx.mock
async def test_postback_error_state_delivers_error(line_adapter_with_fast_llm):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    rid = line_adapter_with_fast_llm._cache.register_pending()
    line_adapter_with_fast_llm._cache.set_error(rid, "⚠️ failed")
    await line_adapter_with_fast_llm.dispatch_event(_postback_event(rid))
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["messages"][0]["text"] == "⚠️ failed"
    assert line_adapter_with_fast_llm._cache.get(rid).state.value == "delivered"


@pytest.mark.asyncio
@respx.mock
async def test_non_text_message_type_is_ignored(line_adapter_with_fast_llm):
    """Sticker, image, audio etc. should be silently dropped — no reply sent."""
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    sticker_event = {
        "type": "message",
        "replyToken": "rt-sticker",
        "source": {"type": "user", "userId": "U1"},
        "timestamp": 1,
        "message": {"id": "m2", "type": "sticker", "packageId": "1", "stickerId": "1"},
    }
    await line_adapter_with_fast_llm.dispatch_event(sticker_event)
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_watcher_button_delivery_failure_logs_warning(line_adapter_with_slow_llm, caplog):
    """When the LINE API rejects the Quick Reply button send, a warning is logged.

    The LLM answer still lands in the cache (READY) when it arrives, but the
    user has no button to tap. The warning gives operators a correlation handle.
    """
    import logging
    respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(429, json={"message": "Too Many Requests"})
    )
    event = _msg_event(reply_token="rt-fail", user="U1")
    with caplog.at_level(logging.WARNING, logger="gateway.platforms.line"):
        await line_adapter_with_slow_llm.dispatch_event(event)
        await line_adapter_with_slow_llm.wait_idle()
    assert any("button delivery failed" in r.message for r in caplog.records), (
        "Expected a WARNING about button delivery failure — check watcher timeout path"
    )
    assert any("rt-fail"[:8] in r.message for r in caplog.records), (
        "Warning should include the reply token prefix for operator correlation"
    )


@pytest.mark.asyncio
@respx.mock
async def test_unconfigured_bot_replies_with_setup_notice(line_adapter_with_fast_llm):
    """When _message_handler is None (no LLM configured), allowed users see setup notice."""
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=Response(200, json={})
    )
    line_adapter_with_fast_llm._message_handler = None  # Simulate no LLM
    # Restore default _llm_call so the pre-check (which only triggers when
    # the default path is active) can fire.
    line_adapter_with_fast_llm._llm_call = line_adapter_with_fast_llm._real_llm_call
    event = _msg_event(reply_token="rt-1", user="U1")
    await line_adapter_with_fast_llm.dispatch_event(event)
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert "not configured" in sent["messages"][0]["text"].lower()
