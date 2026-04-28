"""LineAdapter — Hermes platform adapter for LINE Messaging API.

Handles webhook signature validation, source allowlisting, a PENDING/READY/DELIVERED
request-cache for slow LLM responses, and Reply/Push API dispatch.
"""
from __future__ import annotations

import asyncio
import base64
import enum
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx
from aiohttp import web

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


def check_line_requirements() -> bool:
    """Return True when LINE adapter dependencies (httpx, aiohttp) are importable.

    Both are Hermes core dependencies, so this always returns True in practice.
    Satisfies the adapter factory contract checked by gateway/run.py.
    """
    try:
        import httpx  # noqa: F401
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_LOADING_URL = "https://api.line.me/v2/bot/chat/loading/start"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

PENDING_REPLY_TEXT = (
    os.environ.get("LINE_PENDING_TEXT")
    or "🤔 Still thinking, please wait. If no reply arrives, resend your message."
)
EXPIRED_REPLY_TEXT = (
    os.environ.get("LINE_EXPIRED_TEXT")
    or "Response expired — please ask again."
)
ALREADY_DELIVERED_TEXT = (
    os.environ.get("LINE_DELIVERED_TEXT")
    or "Already replied ✅"
)
SHOW_RESPONSE_BUTTON_LABEL = (
    os.environ.get("LINE_BUTTON_LABEL")
    or "📋 Show response"
)


# ---------------------------------------------------------------------------
# Webhook signature + payload parsing
# ---------------------------------------------------------------------------
# Spec: https://developers.line.biz/en/reference/messaging-api/#signature-validation


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """Constant-time compare LINE's X-Line-Signature header against an HMAC-SHA256
    of the raw body using the channel secret.
    """
    if not signature:
        return False
    expected = base64.b64encode(
        hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def parse_events(body: bytes) -> list[dict[str, Any]]:
    """Parse a LINE webhook body into the raw events list. Returns [] on no events.
    Does NOT validate types — leaves that to the dispatcher.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    return payload.get("events", []) or []


# ---------------------------------------------------------------------------
# Source allowlist
# ---------------------------------------------------------------------------
# Decision order (per spec):
#   1. Allowlist check first — silent drop if not allowed
#   2. Configuration check second — placeholder reply if no LLM yet
#   3. Normal flow — process the message


def is_allowed(event: dict[str, Any], cfg: dict[str, list[str]]) -> bool:
    """Return True if the event's source is in the appropriate allowlist.

    cfg expected shape:
        {"users": ["U..."], "groups": ["C..."], "rooms": ["R..."]}

    If LINE_ALLOW_ALL_USERS env var is truthy, returns True regardless of
    allowlist contents (debug-only escape hatch — mirrors the pattern used by
    other Hermes platform adapters such as DISCORD_ALLOW_ALL_USERS).
    """
    if os.getenv("LINE_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes"):
        return True
    source = event.get("source") or {}
    src_type = source.get("type")
    if src_type == "user":
        return source.get("userId") in cfg.get("users", [])
    if src_type == "group":
        return source.get("groupId") in cfg.get("groups", [])
    if src_type == "room":
        return source.get("roomId") in cfg.get("rooms", [])
    return False


# ---------------------------------------------------------------------------
# Cache state machine
# ---------------------------------------------------------------------------
# In-memory cache of pending/ready LINE LLM responses, keyed by request_id.
#
# State machine (per design spec):
#     PENDING   → LLM still running, no answer yet
#     READY     → LLM done, answer cached, waiting for postback tap
#     DELIVERED → answer already sent
#     ERROR     → LLM raised; cached error text waiting to be shown
#
# Storage: dict in the adapter process. Not Redis-backed — container restart
# drops PENDING entries (acceptable trade-off; users see the expiry reply text).
#
# Two TTLs:
#     - ``ttl_seconds`` (default 1h) — applies to READY/DELIVERED entries,
#       keyed off ``updated_at`` (when the entry transitioned).
#     - ``pending_ttl_seconds`` (default 24h) — ceiling TTL for PENDING
#       entries, keyed off ``created_at`` (when registered).
#
# Pattern borrowed from gateway/platforms/webhook.py (_delivery_info,
# _idempotency_ttl=3600, _prune_delivery_info).


class State(enum.Enum):
    PENDING = "pending"
    READY = "ready"
    DELIVERED = "delivered"
    ERROR = "error"


@dataclass
class CacheEntry:
    state: State
    payload: Any = None  # the LLM response, when READY
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class RequestCache:
    """In-memory `dict[request_id, CacheEntry]` with TTL pruning.

    Two TTLs are enforced by ``prune()``:
        - READY/DELIVERED entries older than ``ttl_seconds`` (by ``updated_at``)
        - PENDING entries older than ``pending_ttl_seconds`` (by ``created_at``)

    Not safe across threads — single-event-loop only.
    """

    def __init__(
        self,
        ttl_seconds: int = 3600,
        pending_ttl_seconds: int = 86400,
    ) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._ttl = ttl_seconds
        self._pending_ttl = pending_ttl_seconds

    def register_pending(self) -> str:
        rid = str(uuid.uuid4())
        self._entries[rid] = CacheEntry(state=State.PENDING)
        return rid

    def get(self, request_id: str) -> Optional[CacheEntry]:
        return self._entries.get(request_id)

    def set_ready(self, request_id: str, payload: Any) -> None:
        entry = self._entries.get(request_id)
        if entry is None:
            return  # task was cancelled / cache was wiped — nothing to do
        entry.state = State.READY
        entry.payload = payload
        entry.updated_at = time.time()

    def set_error(self, request_id: str, error_msg: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None:
            return
        entry.state = State.ERROR
        entry.payload = error_msg
        entry.updated_at = time.time()

    def mark_delivered(self, request_id: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None:
            return
        entry.state = State.DELIVERED
        entry.updated_at = time.time()

    def prune(self) -> None:
        """Remove stale entries.

        - READY/DELIVERED: pruned when ``updated_at`` is older than ``ttl_seconds``.
        - PENDING: pruned when ``created_at`` is older than ``pending_ttl_seconds``
          (ceiling TTL — defends against tasks that never reach a terminal state).
        """
        now = time.time()
        terminal_cutoff = now - self._ttl
        pending_cutoff = now - self._pending_ttl
        stale = [
            rid
            for rid, entry in self._entries.items()
            if (
                entry.state in (State.READY, State.DELIVERED, State.ERROR)
                and entry.updated_at < terminal_cutoff
            )
            or (
                entry.state is State.PENDING
                and entry.created_at < pending_cutoff
            )
        ]
        for rid in stale:
            del self._entries[rid]


# ---------------------------------------------------------------------------
# Reply API client + Quick Reply helpers
# ---------------------------------------------------------------------------
# Quick Reply payload shape per LINE docs:
# https://developers.line.biz/en/reference/messaging-api/#quick-reply


def build_quick_reply_button_message(
    text: str, button_label: str, request_id: str
) -> dict[str, Any]:
    """Build a LINE text message with a single postback Quick Reply button.

    The button payload encodes JSON so the postback handler can route by action.
    """
    return {
        "type": "text",
        "text": text,
        "quickReply": {
            "items": [
                {
                    "type": "action",
                    "action": {
                        "type": "postback",
                        "label": button_label,
                        "data": json.dumps(
                            {"action": "show_response", "request_id": request_id}
                        ),
                        "displayText": button_label,
                    },
                }
            ]
        },
    }


class LineReplyClient:
    """Thin async wrapper around the LINE Reply API."""

    def __init__(self, channel_access_token: str, timeout: float = 10.0) -> None:
        self._headers = {
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    async def reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        """POST /v2/bot/message/reply. Raises on non-2xx (caller decides how to log)."""
        body = {"replyToken": reply_token, "messages": messages}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(LINE_REPLY_URL, headers=self._headers, json=body)
            r.raise_for_status()

    async def show_loading(self, chat_id: str, seconds: int = 30) -> None:
        """Show typing animation in 1-on-1 chats. Up to 60s. Group/room not supported.

        Best-effort — silently ignores failures (loading is UX nice-to-have, not critical).
        Spec: https://developers.line.biz/en/reference/messaging-api/#display-a-loading-animation
        """
        if not chat_id or not chat_id.startswith("U"):
            return  # only valid for 1-on-1 chats (user IDs start with U)
        body = {"chatId": chat_id, "loadingSeconds": max(5, min(60, seconds))}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(LINE_LOADING_URL, headers=self._headers, json=body)
        except Exception:
            pass  # loading indicator failure is non-fatal


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class LineAdapterConfig:
    channel_access_token: str
    channel_secret: str
    allowed_users: list[str] = field(default_factory=list)
    allowed_groups: list[str] = field(default_factory=list)
    allowed_rooms: list[str] = field(default_factory=list)
    slow_response_threshold_seconds: float = 50.0
    request_cache_ttl_seconds: int = 3600

    @classmethod
    def from_env(cls) -> LineAdapterConfig:
        def _required(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise ValueError(f"{name} must be set")
            return v

        def _csv(name: str) -> list[str]:
            raw = os.environ.get(name, "").strip()
            return [item.strip() for item in raw.split(",") if item.strip()]

        return cls(
            channel_access_token=_required("LINE_CHANNEL_ACCESS_TOKEN"),
            channel_secret=_required("LINE_CHANNEL_SECRET"),
            allowed_users=_csv("LINE_ALLOWED_USERS"),
            allowed_groups=_csv("LINE_ALLOWED_GROUPS"),
            allowed_rooms=_csv("LINE_ALLOWED_ROOMS"),
            slow_response_threshold_seconds=float(
                os.environ.get("LINE_SLOW_RESPONSE_THRESHOLD", "50")
            ),
            request_cache_ttl_seconds=int(
                os.environ.get("LINE_CACHE_TTL", "3600")
            ),
        )


class LineAdapter(BasePlatformAdapter):
    name = "line"
    MAX_MESSAGE_LENGTH = 5000  # LINE hard limit per message segment

    def __init__(self, config: LineAdapterConfig) -> None:
        platform_cfg = PlatformConfig(
            enabled=True,
            token=config.channel_access_token,
        )
        super().__init__(platform_cfg, Platform.LINE)
        self._cfg = config
        self._reply = LineReplyClient(channel_access_token=config.channel_access_token)
        self._cache = RequestCache(ttl_seconds=config.request_cache_ttl_seconds)
        self._dedup = MessageDeduplicator()
        self._llm_call: Callable[..., Awaitable[str]] = self._real_llm_call
        # Note: _background_tasks already initialized by BasePlatformAdapter.__init__.
        # Test sync events — created lazily on first dispatch (needs running loop).
        self._test_button_sent_event: Optional[asyncio.Event] = None
        self._test_idle_event: Optional[asyncio.Event] = None
        self._runner: Optional[web.AppRunner] = None

    def _ensure_test_events(self) -> None:
        if self._test_idle_event is None:
            self._test_idle_event = asyncio.Event()
        if self._test_button_sent_event is None:
            self._test_button_sent_event = asyncio.Event()

    @classmethod
    def from_config(cls, cfg: LineAdapterConfig) -> LineAdapter:
        return cls(cfg)

    # ---- Abstract method overrides ----

    def register_routes(self, app: web.Application) -> None:
        """Register LINE webhook routes on a shared/owned aiohttp app.

        Called from connect() once the gateway runner provides an app, or
        directly by tests / external owners of the aiohttp.web.Application.
        """
        app.router.add_post("/line/webhook", self._http_handler)
        app.router.add_get("/line/webhook/health", self._handle_health)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /line/webhook/health — liveness probe for load balancers and k8s."""
        return web.json_response({"status": "ok", "platform": "line"})

    async def _http_handler(self, request: web.Request) -> web.Response:
        body = await request.read()
        signature = request.headers.get("X-Line-Signature", "")
        if not verify_signature(body, signature, self._cfg.channel_secret):
            return web.Response(status=401, text="invalid signature")
        self._cache.prune()
        events = parse_events(body)
        # Return 200 immediately so LINE doesn't retry and show_loading fires ASAP.
        for ev in events:
            event_id = ev.get("webhookEventId", "")
            if event_id and self._dedup.is_duplicate(event_id):
                logger.info("line: ignoring duplicate webhookEventId=%s", event_id)
                continue
            task = asyncio.create_task(self.dispatch_event(ev))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return web.Response(status=200, text="ok")

    async def connect(self, app: Optional[web.Application] = None) -> bool:  # type: ignore[override]
        # Optional `app` extends the abstract signature so the gateway runner
        # can inject its shared aiohttp.web.Application (mirrors WebhookAdapter).
        # Called with no args by GatewayRunner, so the override is safe.
        if app is None:
            app = web.Application()
            self.register_routes(app)
            port = int(os.environ.get("LINE_WEBHOOK_PORT", "8645"))
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            self._runner = runner
            logger.info("LINE webhook listening on :%d/line/webhook", port)
        else:
            self.register_routes(app)
        logger.warning(
            "LINE adapter suppresses self.send() to avoid Push API costs. "
            "Dangerous-command approval prompts cannot reach the user — sessions "
            "will hang on any approval-gated tool call. "
            "Mitigate by pre-approving trusted commands with '/approve always', "
            "or ensure the agent does not trigger dangerous-command gates."
        )
        return True

    async def disconnect(self) -> None:
        for t in list(self._background_tasks):
            t.cancel()
        for t in list(self._background_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            finally:
                self._runner = None

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """Send typing indicator. LINE only supports this for 1-on-1 chats (source type 'user')."""
        if chat_id.startswith("U"):
            await self._reply.show_loading(chat_id, seconds=30)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image via LINE Push API.

        LINE requires HTTPS image URLs and does not support inline captions on
        image messages. When a caption is provided it is sent as a follow-up
        text message. Falls back to sending the URL as plain text when the URL
        is not HTTPS (LINE rejects non-HTTPS originalContentUrl).
        """
        if not image_url.startswith("https://"):
            # LINE rejects non-HTTPS URLs — degrade to text link
            text = f"{caption}\n{image_url}" if caption else image_url
            return await self._push_text(chat_id, text)

        token = self._cfg.channel_access_token
        if not token:
            return SendResult(success=False, error="LINE: channel access token not set")

        messages: list[dict] = [
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            }
        ]
        if caption:
            messages.extend(self._chunk_text(caption)[:4])  # 1 image + max 4 text = 5 per LINE limit

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    LINE_PUSH_URL,
                    json={"to": chat_id, "messages": messages},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
            return SendResult(success=True)
        except Exception as exc:
            logger.warning("line: send_image push failed chat_id=%s: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Not supported — LINE requires publicly accessible HTTPS image URLs.

        Local file upload is not available through the Messaging API. Returns
        a non-fatal error so callers (base class dispatch loop) can fall back
        to text and continue rather than crashing.
        """
        logger.warning(
            "line: send_image_file not supported (LINE requires HTTPS URLs); "
            "chat_id=%s path=%s",
            chat_id,
            image_path,
        )
        return SendResult(
            success=False,
            error="LINE adapter does not support local file upload — HTTPS URL required",
        )

    def _chunk_text(self, text: str) -> list[dict[str, Any]]:
        """Split text into LINE message segment dicts (max 5000 chars, max 5 per call).

        The 5-message cap matches LINE's per-call limit for both Reply and Push APIs.
        Responses longer than 25,000 chars are silently truncated at 5 segments.
        Empty text returns a single placeholder so LINE never receives an empty messages array.
        """
        if not text:
            return [{"type": "text", "text": "(no response)"}]
        chunks = [
            text[i:i + self.MAX_MESSAGE_LENGTH]
            for i in range(0, len(text), self.MAX_MESSAGE_LENGTH)
        ][:5]
        return [{"type": "text", "text": chunk} for chunk in chunks]

    async def _push_text(self, chat_id: str, text: str) -> SendResult:
        """Send a plain text message via LINE Push API."""
        token = self._cfg.channel_access_token
        if not token:
            return SendResult(success=False, error="LINE: channel access token not set")
        messages = self._chunk_text(text)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    LINE_PUSH_URL,
                    json={"to": chat_id, "messages": messages},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
            return SendResult(success=True)
        except Exception as exc:
            logger.warning("line: push_text failed chat_id=%s: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Suppress incidental base-class send() calls (compaction, approval prompts, etc.).

        LINE replies go through the Reply API in _handle_message/_handle_postback.
        Framework-internal sends would require Push API (costs money) — log and no-op.
        """
        preview = (content or "")[:80].replace("\n", " ")
        logger.info(
            "line: suppressed self.send chat_id=%s preview=%r",
            chat_id,
            preview,
        )
        return SendResult(
            success=False,
            error="line_adapter_suppresses_push_sends",
        )

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Minimal stub — LINE Get Profile/Group APIs not wired.

        Detect chat type by LINE id prefix:
          U… → user (dm), C… → group, R… → room.
        """
        if chat_id.startswith("U"):
            ctype = "dm"
        elif chat_id.startswith("C"):
            ctype = "group"
        elif chat_id.startswith("R"):
            ctype = "room"
        else:
            ctype = "unknown"
        return {"name": chat_id, "type": ctype, "chat_id": chat_id}

    # ---- Public dispatch ----

    async def dispatch_event(self, event: dict[str, Any]) -> None:
        self._ensure_test_events()
        assert self._test_idle_event is not None
        assert self._test_button_sent_event is not None
        self._test_idle_event.clear()
        self._test_button_sent_event.clear()
        cfg = {
            "users": self._cfg.allowed_users,
            "groups": self._cfg.allowed_groups,
            "rooms": self._cfg.allowed_rooms,
        }
        if not is_allowed(event, cfg):
            self._log_drop(event)
            return  # silent drop

        # Pre-check: bot enabled but no LLM provider configured (Phase 1 /
        # pre-`hermes setup`). Reply with a friendly setup notice instead
        # of letting the dispatcher raise RuntimeError downstream. Applies
        # to both message and postback events. Allowlist already enforced
        # above, so unauthorized users still silent-drop. Only triggers
        # when the default _real_llm_call path is active — tests that
        # override _llm_call with a stub bypass this check.
        if self._message_handler is None and self._llm_call == self._real_llm_call:
            reply_token = event.get("replyToken")
            if reply_token:
                await self._reply.reply(
                    reply_token,
                    [{"type": "text", "text": "Bot is not configured yet — please contact the administrator."}],
                )
            self._test_idle_event.set()
            return

        if event.get("type") == "message":
            await self._handle_message(event)
        elif event.get("type") == "postback":
            await self._handle_postback(event)
        # Other event types (follow, join, leave, etc.) are not handled.

    # ---- Message handler ----

    async def _handle_message(self, event: dict[str, Any]) -> None:
        msg = event.get("message", {})
        if msg.get("type") != "text":
            logger.info("line: ignoring non-text message type=%s", msg.get("type"))
            return
        text = msg.get("text", "")
        source = event.get("source", {})
        reply_token = event.get("replyToken")
        if not reply_token:
            logger.info(
                "line: dropping message event without replyToken: source=%s",
                source,
            )
            return
        logger.info(
            "line: received message src_type=%s user=%s text=%r",
            source.get("type"),
            source.get("userId"),
            text[:80],
        )
        # Show typing indicator in 1-on-1 chats (LINE limitation: groups don't support it).
        # Best-effort, fire-and-forget.
        if source.get("type") == "user":
            user_id = source.get("userId")
            if user_id:
                asyncio.create_task(self._reply.show_loading(user_id, seconds=30))
        request_id = self._cache.register_pending()

        async def _llm_then_dispatch() -> None:
            try:
                answer = await self._llm_call(text, source, event)
                self._cache.set_ready(request_id, answer)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("LLM call failed for request_id=%s", request_id)
                self._cache.set_error(
                    request_id, f"⚠️ Processing error: {type(e).__name__}"
                )

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
                            self._chunk_text(entry.payload),
                        )
                        self._cache.mark_delivered(request_id)
                    elif entry and entry.state is State.ERROR:
                        await self._reply.reply(
                            reply_token,
                            self._chunk_text(entry.payload),
                        )
                        self._cache.mark_delivered(request_id)
                    else:
                        logger.error(
                            "line: cache entry missing or wrong state for request_id=%s "
                            "(pruned or cancelled?) — reply token likely expired",
                            request_id,
                        )
                except asyncio.TimeoutError:
                    msg = build_quick_reply_button_message(
                        text=PENDING_REPLY_TEXT,
                        button_label=SHOW_RESPONSE_BUTTON_LABEL,
                        request_id=request_id,
                    )
                    await self._reply.reply(reply_token, [msg])
                    self._test_button_sent_event.set()
            except Exception:
                logger.exception("watcher failed for request_id=%s", request_id)
            finally:
                self._test_idle_event.set()

        watcher_task = asyncio.create_task(_watcher())
        self._background_tasks.add(watcher_task)
        watcher_task.add_done_callback(self._background_tasks.discard)

    # ---- Postback handler ----

    async def _handle_postback(self, event: dict[str, Any]) -> None:
        reply_token = event.get("replyToken")
        if not reply_token:
            logger.info("line: postback without replyToken: source=%s", event.get("source"))
            return
        try:
            payload = json.loads(event.get("postback", {}).get("data", "{}"))
            if not isinstance(payload, dict):
                payload = {}
        except json.JSONDecodeError:
            payload = {}
        if payload.get("action") != "show_response":
            return  # not ours, ignore
        request_id = payload.get("request_id")
        entry = self._cache.get(request_id) if request_id else None

        if entry is None:
            await self._reply.reply(reply_token, [{"type": "text", "text": EXPIRED_REPLY_TEXT}])
            return

        if entry.state is State.PENDING:
            msg = build_quick_reply_button_message(
                text=PENDING_REPLY_TEXT,
                button_label=SHOW_RESPONSE_BUTTON_LABEL,
                request_id=request_id,
            )
            await self._reply.reply(reply_token, [msg])
            return

        if entry.state is State.READY:
            await self._reply.reply(reply_token, self._chunk_text(entry.payload))
            self._cache.mark_delivered(request_id)
            return

        if entry.state is State.DELIVERED:
            await self._reply.reply(reply_token, [{"type": "text", "text": ALREADY_DELIVERED_TEXT}])
            return

        if entry.state is State.ERROR:
            await self._reply.reply(reply_token, self._chunk_text(entry.payload))
            self._cache.mark_delivered(request_id)
            return

    # ---- Helpers ----

    def _log_drop(self, event: dict[str, Any]) -> None:
        """Structured drop log so admins can discover new group/room IDs."""
        src = event.get("source", {})
        logger.info(
            "line.drop unauthorised src_type=%s user=%s group=%s room=%s",
            src.get("type"),
            src.get("userId"),
            src.get("groupId"),
            src.get("roomId"),
        )

    async def _real_llm_call(
        self,
        text: str,
        source: dict[str, Any],
        event: Optional[dict[str, Any]] = None,
    ) -> str:
        """Production LLM path.

        Bypasses BasePlatformAdapter.handle_message() session machinery
        (which would try to deliver via self.send → Push API). Instead we
        invoke the registered _message_handler directly to obtain the
        agent's reply text, then return it for the LINE Reply API path
        in _handle_message to deliver via reply_token.

        Approval-prompt suppression: not needed here because we don't
        enter the session lifecycle that issues approval prompts. Tool
        approval (if a tool requires it during _message_handler) is
        handled by the underlying agent in always-allow mode for LINE
        deployments — operators should set HERMES_AUTO_APPROVE_TOOLS=1
        or equivalent in env when running with LINE.
        """
        if self._message_handler is None:
            raise RuntimeError(
                "LineAdapter._real_llm_call invoked before set_message_handler()"
            )

        src_type = source.get("type")
        if src_type == "user":
            chat_type = "dm"
        elif src_type == "group":
            chat_type = "group"
        elif src_type == "room":
            chat_type = "room"
        else:
            chat_type = "unknown"
        # LINE source: userId always present; groupId/roomId for group/room
        chat_id = (
            source.get("groupId")
            or source.get("roomId")
            or source.get("userId")
            or "unknown"
        )
        user_id = source.get("userId")

        session_source = SessionSource(
            platform=Platform.LINE,
            chat_id=str(chat_id),
            chat_type=chat_type,
            user_id=str(user_id) if user_id else None,
        )
        message_id = None
        if event is not None:
            message_id = event.get("message", {}).get("id")
        msg_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=session_source,
            message_id=message_id,
            raw_message={"line_event": event} if event is not None else {"line_source": source},
        )
        response = await self._message_handler(msg_event)
        return response or ""

    # ---- Test helpers (no-ops in production) ----

    async def wait_idle(self) -> None:
        self._ensure_test_events()
        assert self._test_idle_event is not None
        await self._test_idle_event.wait()

    async def wait_button_sent(self) -> None:
        self._ensure_test_events()
        assert self._test_button_sent_event is not None
        await self._test_button_sent_event.wait()
