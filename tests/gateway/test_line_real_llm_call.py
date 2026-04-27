"""Verify _real_llm_call wires through to the registered _message_handler.

We don't need a real LLM here — set_message_handler() lets us inject a
stub MessageHandler and verify that:
  1. _real_llm_call builds a proper MessageEvent with SessionSource
  2. The handler's return value flows back as the LLM reply text
  3. Source dict (LINE event source) is correctly mapped:
       - type=user → chat_type=dm, chat_id=userId
       - type=group → chat_type=group, chat_id=groupId
"""
import os

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.line import LineAdapter, LineAdapterConfig


def _make_adapter() -> LineAdapter:
    cfg = LineAdapterConfig(
        channel_access_token="t",
        channel_secret="s",
        allowed_users=["U1"],
        allowed_groups=["G1"],
        allowed_rooms=[],
    )
    return LineAdapter.from_config(cfg)


@pytest.mark.asyncio
async def test_real_llm_call_invokes_message_handler_with_dm_source():
    adapter = _make_adapter()
    captured: dict = {}

    async def handler(event: MessageEvent) -> str:
        captured["event"] = event
        return "agent reply"

    adapter.set_message_handler(handler)
    out = await adapter._real_llm_call("hi", {"type": "user", "userId": "U1"})

    assert out == "agent reply"
    ev = captured["event"]
    assert isinstance(ev, MessageEvent)
    assert ev.text == "hi"
    assert ev.message_type == MessageType.TEXT
    assert ev.source.platform == Platform.LINE
    assert ev.source.chat_type == "dm"
    assert ev.source.chat_id == "U1"
    assert ev.source.user_id == "U1"
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_real_llm_call_maps_group_source():
    adapter = _make_adapter()

    async def handler(event: MessageEvent) -> str:
        assert event.source.chat_type == "group"
        assert event.source.chat_id == "G1"
        assert event.source.user_id == "U1"
        return "ok"

    adapter.set_message_handler(handler)
    out = await adapter._real_llm_call(
        "ping", {"type": "group", "groupId": "G1", "userId": "U1"}
    )
    assert out == "ok"
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_real_llm_call_handles_none_response():
    adapter = _make_adapter()

    async def handler(event: MessageEvent):
        return None

    adapter.set_message_handler(handler)
    out = await adapter._real_llm_call("x", {"type": "user", "userId": "U1"})
    assert out == ""
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_real_llm_call_without_handler_raises():
    adapter = _make_adapter()
    with pytest.raises(RuntimeError, match="set_message_handler"):
        await adapter._real_llm_call("x", {"type": "user", "userId": "U1"})
    await adapter.disconnect()


# Optional smoke test gated behind env var (real LLM, costs money)
requires_llm = pytest.mark.skipif(
    not os.environ.get("HERMES_TEST_LLM"),
    reason="set HERMES_TEST_LLM=1 to run real LLM smoke test",
)


@requires_llm
@pytest.mark.asyncio
async def test_real_llm_call_returns_text_with_real_handler():
    # Caller would register a real handler from gateway.run; skipped by default.
    pytest.skip("requires full gateway wiring; covered by integration tests")
