import asyncio

import pytest_asyncio

from gateway.platforms.line.adapter import LineAdapter, LineAdapterConfig


@pytest_asyncio.fixture
async def line_adapter_user_only():
    cfg = LineAdapterConfig(
        channel_access_token="t",
        channel_secret="s",
        allowed_users=["U1"],
        allowed_groups=[],
        allowed_rooms=[],
        slow_response_threshold_seconds=50,
        request_cache_ttl_seconds=3600,
    )
    adapter = LineAdapter.from_config(cfg)

    async def stub_llm(text, source, event=None):
        return "ok"

    adapter._llm_call = stub_llm
    yield adapter
    await adapter.disconnect()


@pytest_asyncio.fixture
async def line_adapter_with_fast_llm():
    cfg = LineAdapterConfig(
        channel_access_token="t",
        channel_secret="s",
        allowed_users=["U1"],
        allowed_groups=[],
        allowed_rooms=[],
        slow_response_threshold_seconds=50,
        request_cache_ttl_seconds=3600,
    )
    adapter = LineAdapter.from_config(cfg)

    async def fast_llm(text, source, event=None):
        return f"fast: {text}"

    adapter._llm_call = fast_llm
    yield adapter
    await adapter.disconnect()


@pytest_asyncio.fixture
async def line_adapter_with_slow_llm():
    cfg = LineAdapterConfig(
        channel_access_token="t",
        channel_secret="s",
        allowed_users=["U1"],
        allowed_groups=[],
        allowed_rooms=[],
        slow_response_threshold_seconds=0.1,
        request_cache_ttl_seconds=3600,
    )
    adapter = LineAdapter.from_config(cfg)

    async def slow_llm(text, source, event=None):
        await asyncio.sleep(0.5)
        return f"slow: {text}"

    adapter._llm_call = slow_llm
    yield adapter
    await adapter.disconnect()
