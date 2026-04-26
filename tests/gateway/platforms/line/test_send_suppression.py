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
    caplog.set_level(logging.INFO, logger="gateway.platforms.line.adapter")
    result = await line_adapter_with_fast_llm.send(
        chat_id="U1", content="incidental notice from base"
    )
    assert isinstance(result, SendResult)
    assert result.success is False
    assert "suppressed" in caplog.text.lower()
    assert "U1" in caplog.text


@pytest.mark.asyncio
async def test_send_image_does_not_raise(line_adapter_with_fast_llm):
    # send_image default impl in base.py forwards to self.send — must not raise.
    result = await line_adapter_with_fast_llm.send_image(
        chat_id="U1", image_url="https://example.com/x.png"
    )
    assert isinstance(result, SendResult)
    assert result.success is False


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
