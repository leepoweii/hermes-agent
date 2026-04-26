"""LineAdapter — Hermes platform adapter for LINE Messaging API.

Pattern reference:
  - gateway/platforms/webhook.py — request cache + TTL pattern (borrowed in cache.py)
  - gateway/platforms/telegram.py — env var / allowlist pattern
  - gateway/platforms/base.py:1898-1921 — `_background_tasks` set + cancel-on-shutdown
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.line.allowlist import is_allowed
from gateway.platforms.line.cache import RequestCache, State
from gateway.platforms.line.reply import (
    LineReplyClient,
    PENDING_REPLY_TEXT,
    build_quick_reply_button_message,
)


log = logging.getLogger(__name__)


@dataclass
class LineAdapterConfig:
    channel_access_token: str
    channel_secret: str
    allowed_users: list[str] = field(default_factory=list)
    allowed_groups: list[str] = field(default_factory=list)
    allowed_rooms: list[str] = field(default_factory=list)
    slow_response_threshold_seconds: float = 50.0
    request_cache_ttl_seconds: int = 3600


class LineAdapter(BasePlatformAdapter):
    name = "line"

    def __init__(self, config: LineAdapterConfig) -> None:
        platform_cfg = PlatformConfig(
            enabled=True,
            token=config.channel_access_token,
        )
        super().__init__(platform_cfg, Platform.LINE)
        self._cfg = config
        self._reply = LineReplyClient(channel_access_token=config.channel_access_token)
        self._cache = RequestCache(ttl_seconds=config.request_cache_ttl_seconds)
        self._llm_call: Callable[[str, dict], Awaitable[str]] = self._real_llm_call
        # Note: _background_tasks already initialized by BasePlatformAdapter.__init__.
        self._test_button_sent_event: Optional[asyncio.Event] = None
        self._test_idle_event: Optional[asyncio.Event] = None

    @classmethod
    def from_config(cls, cfg: LineAdapterConfig | dict) -> "LineAdapter":
        if isinstance(cfg, dict):
            cfg = LineAdapterConfig(**cfg)
        return cls(cfg)

    # ---- Abstract method overrides ----

    async def connect(self) -> bool:
        # HTTP server registration wired in Task 10.
        return True

    async def disconnect(self) -> None:
        for t in list(self._background_tasks):
            t.cancel()
        for t in list(self._background_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # Wired in later task; LINE uses Reply API via reply_token, not chat_id.
        raise NotImplementedError("LineAdapter.send wired in later task")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        raise NotImplementedError("LineAdapter.get_chat_info wired in later task")

    # ---- Public dispatch ----

    async def dispatch_event(self, event: dict[str, Any]) -> None:
        cfg = {
            "users": self._cfg.allowed_users,
            "groups": self._cfg.allowed_groups,
            "rooms": self._cfg.allowed_rooms,
        }
        if not is_allowed(event, cfg):
            self._log_drop(event)
            return  # silent drop

        if event.get("type") == "message":
            await self._handle_message(event)
        elif event.get("type") == "postback":
            await self._handle_postback(event)
        # Other event types ignored for v2.

    # ---- Message handler ----

    async def _handle_message(self, event: dict[str, Any]) -> None:
        text = event.get("message", {}).get("text", "")
        source = event.get("source", {})
        reply_token = event.get("replyToken")
        request_id = self._cache.register_pending()

        async def _llm_then_dispatch() -> None:
            try:
                answer = await self._llm_call(text, source)
                self._cache.set_ready(request_id, answer)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("LLM call failed for request_id=%s", request_id)

        llm_task = asyncio.create_task(_llm_then_dispatch())
        self._background_tasks.add(llm_task)
        llm_task.add_done_callback(self._background_tasks.discard)

        async def _watcher() -> None:
            try:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(llm_task),
                        timeout=self._cfg.slow_response_threshold_seconds,
                    )
                    entry = self._cache.get(request_id)
                    if entry and entry.state is State.READY:
                        await self._reply.reply(
                            reply_token,
                            [{"type": "text", "text": entry.payload}],
                        )
                        self._cache.mark_delivered(request_id)
                except asyncio.TimeoutError:
                    msg = build_quick_reply_button_message(
                        text=PENDING_REPLY_TEXT,
                        button_label="📋 點此查看答案",
                        request_id=request_id,
                    )
                    await self._reply.reply(reply_token, [msg])
                    if self._test_button_sent_event:
                        self._test_button_sent_event.set()
            except Exception:
                log.exception("watcher failed for request_id=%s", request_id)
            finally:
                if self._test_idle_event:
                    self._test_idle_event.set()

        watcher_task = asyncio.create_task(_watcher())
        self._background_tasks.add(watcher_task)
        watcher_task.add_done_callback(self._background_tasks.discard)

    # ---- Postback handler (stub for Task 8) ----

    async def _handle_postback(self, event: dict[str, Any]) -> None:
        raise NotImplementedError("Implemented in Task 8")

    # ---- Helpers ----

    def _log_drop(self, event: dict[str, Any]) -> None:
        """Structured drop log so admins can discover new group/room IDs."""
        src = event.get("source", {})
        log.info(
            "line.drop unauthorised src_type=%s user=%s group=%s room=%s",
            src.get("type"),
            src.get("userId"),
            src.get("groupId"),
            src.get("roomId"),
        )

    async def _real_llm_call(self, text: str, source: dict[str, Any]) -> str:
        """Production LLM path — wired in Task 9."""
        raise NotImplementedError(
            "Wired in Task 9 via BasePlatformAdapter session helpers"
        )

    # ---- Test helpers (no-ops in production) ----

    async def wait_idle(self) -> None:
        self._test_idle_event = asyncio.Event()
        if not self._background_tasks:
            return
        await self._test_idle_event.wait()

    async def wait_button_sent(self) -> None:
        self._test_button_sent_event = asyncio.Event()
        await self._test_button_sent_event.wait()
