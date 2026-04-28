"""Verify LineAdapter suppresses incidental base-class self.send() calls
instead of raising NotImplementedError.

Background: BasePlatformAdapter self-calls send/send_image/etc. for
framework-internal status messages (compaction notices, approval
prompts, rate-limit notices). LINE only allows replies via reply_token
(Reply API); Push API costs money. Decision: silent-log + return
non-success SendResult so base callers don't crash.
"""
import logging

import pytest

from gateway.platforms.base import SendResult


@pytest.mark.asyncio
async def test_send_does_not_raise_and_logs(line_adapter_with_fast_llm, caplog):
    caplog.set_level(logging.INFO, logger="gateway.platforms.line")
    result = await line_adapter_with_fast_llm.send(
        chat_id="U1", content="incidental notice from base"
    )
    assert isinstance(result, SendResult)
    assert result.success is False
    assert "suppressed" in caplog.text.lower()
    assert "U1" in caplog.text


@pytest.mark.asyncio
async def test_send_image_does_not_raise(line_adapter_with_fast_llm):
    # send_image is implemented (Push API) and must not raise on network error.
    # It returns a non-success SendResult rather than propagating the exception.
    import respx
    from httpx import Response

    with respx.mock:
        respx.post("https://api.line.me/v2/bot/message/push").mock(
            return_value=Response(200, json={})
        )
        result = await line_adapter_with_fast_llm.send_image(
            chat_id="U1", image_url="https://example.com/x.png"
        )
    assert isinstance(result, SendResult)
    assert result.success is True


@pytest.mark.asyncio
async def test_send_voice_does_not_raise(line_adapter_with_fast_llm):
    result = await line_adapter_with_fast_llm.send_voice(
        chat_id="U1", audio_path="/tmp/nonexistent.ogg"
    )
    assert isinstance(result, SendResult)
    assert result.success is False


@pytest.mark.asyncio
async def test_send_document_does_not_raise(line_adapter_with_fast_llm):
    result = await line_adapter_with_fast_llm.send_document(
        chat_id="U1", file_path="/tmp/nonexistent.pdf"
    )
    assert isinstance(result, SendResult)
    assert result.success is False


@pytest.mark.asyncio
async def test_get_chat_info_returns_minimal_dict(line_adapter_with_fast_llm):
    info = await line_adapter_with_fast_llm.get_chat_info("U1")
    assert isinstance(info, dict)
    assert "name" in info
    assert "type" in info


@pytest.mark.asyncio
async def test_send_handles_none_content(line_adapter_with_fast_llm):
    # Defensive: don't crash on empty/None payloads.
    result = await line_adapter_with_fast_llm.send(chat_id="U1", content="")
    assert result.success is False


# ---- _chunk_text unit tests ----

def test_chunk_text_short_returns_single_segment(line_adapter_with_fast_llm):
    segs = line_adapter_with_fast_llm._chunk_text("hello")
    assert len(segs) == 1
    assert segs[0] == {"type": "text", "text": "hello"}


def test_chunk_text_empty_returns_placeholder(line_adapter_with_fast_llm):
    segs = line_adapter_with_fast_llm._chunk_text("")
    assert len(segs) == 1
    assert segs[0]["type"] == "text"
    assert segs[0]["text"]  # must be non-empty so LINE doesn't reject the payload


def test_chunk_text_exactly_max_length(line_adapter_with_fast_llm):
    text = "a" * 5000
    segs = line_adapter_with_fast_llm._chunk_text(text)
    assert len(segs) == 1
    assert segs[0]["text"] == text


def test_chunk_text_splits_at_max_length(line_adapter_with_fast_llm):
    text = "a" * 5001
    segs = line_adapter_with_fast_llm._chunk_text(text)
    assert len(segs) == 2
    assert segs[0]["text"] == "a" * 5000
    assert segs[1]["text"] == "a"


def test_chunk_text_caps_at_five_segments(line_adapter_with_fast_llm):
    text = "a" * (5000 * 6)  # would be 6 segments without cap
    segs = line_adapter_with_fast_llm._chunk_text(text)
    assert len(segs) == 5
