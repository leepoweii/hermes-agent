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
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

# aiohttp lives in the [messaging] optional extra; guard the top-level import
# so the module is importable when only core deps are installed. The factory
# in gateway/run.py uses check_line_requirements() to gate adapter creation.
try:
    import aiohttp  # noqa: F401
    from aiohttp import web
    _AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    _AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    coerce_plaintext_gateway_command,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


def check_line_requirements() -> bool:
    """Check if LINE adapter dependencies (httpx, aiohttp) are available."""
    return _AIOHTTP_AVAILABLE  # httpx is a core dep — only aiohttp can be missing


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


def _scrub_token(text: str) -> str:
    """Best-effort scrubbing of Bearer tokens before they hit logs / SendResult.

    httpx exception strings don't currently include Authorization headers, but
    that contract isn't documented and could change. Defensive guard: replace
    any 'Bearer <token>' substring with a marker.
    """
    return re.sub(r"Bearer\s+\S+", "Bearer <redacted>", text)


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """Constant-time compare LINE's X-Line-Signature header against an HMAC-SHA256
    of the raw body using the channel secret.

    Returns False (rejecting the webhook) when ``channel_secret`` is empty —
    operators running in outbound-only mode (no LINE_CHANNEL_SECRET) get
    401 on every inbound webhook, cleanly disabling incoming traffic.
    """
    if not signature or not channel_secret:
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
    if not isinstance(source, dict):
        return False
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

    Not thread-safe — relies on cooperative single-event-loop scheduling.
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

    def get(self, request_id: str) -> CacheEntry | None:
        return self._entries.get(request_id)

    def set_ready(self, request_id: str, payload: Any) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state is not State.PENDING:
            return  # task cancelled, cache wiped, or already transitioned
        entry.state = State.READY
        entry.payload = payload
        entry.updated_at = time.time()

    def set_error(self, request_id: str, error_msg: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state is not State.PENDING:
            return
        entry.state = State.ERROR
        entry.payload = error_msg
        entry.updated_at = time.time()

    def mark_delivered(self, request_id: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state not in (State.READY, State.ERROR):
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

    async def show_loading(self, chat_id: str, seconds: int = 60) -> None:
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
    # LINE's reply token is documented as valid for ~60 seconds. Defaulting to
    # 45s leaves a 15s safety margin for the Quick Reply button to land before
    # the token expires. Override via LINE_SLOW_RESPONSE_THRESHOLD if the LINE
    # token window changes or you need a tighter dead-zone trade-off.
    slow_response_threshold_seconds: float = 45.0
    request_cache_ttl_seconds: int = 3600
    # Group mention gating: if True, group/room chats only respond when the
    # bot display name is @-mentioned (e.g. "@小茉"). DMs are never gated.
    require_mention: bool = False
    bot_display_name: str = ""  # e.g. "小茉"
    # Per-source escape hatches: groups/rooms listed here always trigger the
    # bot regardless of require_mention. Mirrors Telegram's free_response_chats
    # — useful for "dedicated bot groups" where every message is bot-bound.
    free_response_groups: list[str] = field(default_factory=list)
    free_response_rooms: list[str] = field(default_factory=list)

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

        def _bool(name: str) -> bool:
            return os.environ.get(name, "").lower() in ("true", "1", "yes")

        return cls(
            channel_access_token=_required("LINE_CHANNEL_ACCESS_TOKEN"),
            # Channel secret is only required for the inbound webhook
            # receiver (HMAC verification). Outbound-only setups (Push API
            # via send_message_tool, cron deliveries) work with token alone.
            # When empty, verify_signature() rejects every webhook with 401
            # which cleanly disables incoming traffic without breaking sends.
            channel_secret=os.environ.get("LINE_CHANNEL_SECRET", ""),
            allowed_users=_csv("LINE_ALLOWED_USERS"),
            allowed_groups=_csv("LINE_ALLOWED_GROUPS"),
            allowed_rooms=_csv("LINE_ALLOWED_ROOMS"),
            slow_response_threshold_seconds=float(
                os.environ.get("LINE_SLOW_RESPONSE_THRESHOLD", "45")
            ),
            request_cache_ttl_seconds=int(
                os.environ.get("LINE_CACHE_TTL", "3600")
            ),
            require_mention=_bool("LINE_REQUIRE_MENTION"),
            bot_display_name=os.environ.get("LINE_BOT_DISPLAY_NAME", "").strip(),
            free_response_groups=_csv("LINE_FREE_RESPONSE_GROUPS"),
            free_response_rooms=_csv("LINE_FREE_RESPONSE_ROOMS"),
        )


class LineAdapter(BasePlatformAdapter):
    """LINE Messaging API adapter.

    Design note: this adapter intentionally does NOT call
    ``BasePlatformAdapter.handle_message()`` or ``self.build_source()``.
    The base-class session pipeline delivers via ``self.send()`` which
    would consume LINE Push API quota; we route inbound text through
    ``_real_llm_call`` -> ``_message_handler`` directly and reply with
    the Reply API token instead. ``SessionSource`` is constructed
    inline in ``_real_llm_call`` for the same reason. See the
    ``_real_llm_call`` docstring for the full rationale and trade-offs.
    """

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
        # Resolved at connect() time via GET /v2/bot/info. Falls back to
        # LINE_BOT_DISPLAY_NAME env var override if set (useful for tests/offline).
        self._bot_display_name: str = config.bot_display_name
        # Note: _background_tasks already initialized by BasePlatformAdapter.__init__.
        # Test sync events — created lazily on first dispatch (needs running loop).
        self._test_button_sent_event: asyncio.Event | None = None
        self._test_idle_event: asyncio.Event | None = None
        self._runner: web.AppRunner | None = None
        # Per-chat in-flight request tracking — prevents two concurrent agent
        # runs for the same LINE chat (codex/gemini review #2 P1: without
        # this, rapid follow-up messages spawn parallel _llm_call() tasks
        # that race on tool-approval state and conversation history).
        self._active_chats: dict[str, str] = {}

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
        # Delivery is at-most-once: a process restart between dedup-mark and dispatch
        # causes the event to be silently dropped on the retry.
        for ev in events:
            event_id = ev.get("webhookEventId", "")
            if event_id and self._dedup.is_duplicate(event_id):
                logger.info("line: ignoring duplicate webhookEventId=%s", event_id)
                continue
            task = asyncio.create_task(self.dispatch_event(ev))
            self._background_tasks.add(task)
            task.add_done_callback(self._dispatch_done_callback)
        return web.Response(status=200, text="ok")

    def _dispatch_done_callback(self, task: asyncio.Task) -> None:
        """Discard finished dispatch tasks and log any unhandled exceptions."""
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.exception("line: dispatch_event crashed", exc_info=task.exception())

    async def _fetch_bot_info(self) -> None:
        """Fetch bot display name from LINE API and cache it.

        Only runs when require_mention=True and no manual override is set.
        Failure is non-fatal — mention gate falls back to no-name (blocks all
        group messages) and logs a warning so the operator knows to set
        LINE_BOT_DISPLAY_NAME manually.
        """
        if not self._cfg.require_mention or self._bot_display_name:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.line.me/v2/bot/info",
                    headers={"Authorization": f"Bearer {self._cfg.channel_access_token}"},
                )
                resp.raise_for_status()
                data = resp.json()
                name = data.get("displayName") or ""
                if not name:
                    logger.warning(
                        "LINE /v2/bot/info returned empty displayName — set "
                        "LINE_BOT_DISPLAY_NAME manually for group mention gating to work."
                    )
                    return
                self._bot_display_name = name
                logger.info("LINE bot display name resolved: %r", self._bot_display_name)
        except Exception:
            logger.warning(
                "Failed to fetch LINE bot info — set LINE_BOT_DISPLAY_NAME manually "
                "if you want group mention gating to work.",
                exc_info=True,
            )

    async def connect(self, app: web.Application | None = None) -> bool:  # type: ignore[override]
        # Optional `app` extends the abstract signature so the gateway runner
        # could inject a shared aiohttp.web.Application. Called with no args
        # by GatewayRunner today (we own our own listener); the parameter is
        # forward-compatible for future shared-app integration.

        # Acquire a per-credential platform lock so two Hermes instances
        # cannot claim the same LINE channel simultaneously — without this
        # both instances would silently consume each other's reply tokens
        # and dedup state, mirroring the Telegram/Discord precedent.
        if not self._acquire_platform_lock(
            'line-channel-token',
            self._cfg.channel_access_token,
            'LINE channel access token',
        ):
            return False
        # Lock acquired — make sure ANY failure between here and the end of
        # connect() releases it, otherwise an in-process retry (e.g. after a
        # port-already-bound exception) would falsely report the channel
        # token as already claimed (codex review #4 P2).
        try:
            # Resolve bot info BEFORE accepting webhooks so the mention gate
            # never has a cold-start window with bot_display_name unresolved.
            await self._fetch_bot_info()
            # Warn on the most common operator footgun: a group/room is in
            # free_response_* but missing from the allowlist, so the allowlist
            # check silently drops the message before free-response can fire.
            unreachable_groups = set(self._cfg.free_response_groups) - set(self._cfg.allowed_groups)
            unreachable_rooms = set(self._cfg.free_response_rooms) - set(self._cfg.allowed_rooms)
            if unreachable_groups:
                logger.warning(
                    "line: free_response_groups contains IDs not in LINE_ALLOWED_GROUPS — "
                    "messages will be silently dropped at the allowlist: %s",
                    sorted(unreachable_groups),
                )
            if unreachable_rooms:
                logger.warning(
                    "line: free_response_rooms contains IDs not in LINE_ALLOWED_ROOMS — "
                    "messages will be silently dropped at the allowlist: %s",
                    sorted(unreachable_rooms),
                )
            if app is None:
                app = web.Application()
                self.register_routes(app)
                port = int(os.environ.get("LINE_WEBHOOK_PORT", "8645"))
                runner = web.AppRunner(app)
                await runner.setup()
                try:
                    site = web.TCPSite(runner, "0.0.0.0", port)
                    await site.start()
                except Exception:
                    # site.start() raises on port-in-use etc.; clean up the
                    # half-initialised runner so we don't leak the AppRunner.
                    await runner.cleanup()
                    raise
                self._runner = runner
                logger.info("LINE webhook listening on :%d/line/webhook", port)
            else:
                self.register_routes(app)
        except Exception:
            # Release the platform lock so subsequent retries in the same
            # process don't see "channel token already in use".
            self._release_platform_lock()
            raise
        logger.warning(
            "LINE adapter suppresses self.send() to avoid Push API costs. "
            "Dangerous-command approval prompts cannot reach the user — sessions "
            "will hang on any approval-gated tool call. "
            "Mitigate by pre-approving trusted commands with '/approve always', "
            "or ensure the agent does not trigger dangerous-command gates."
        )
        self._mark_connected()
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
        self._release_platform_lock()
        self._mark_disconnected()

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Send typing indicator. LINE only supports this for 1-on-1 chats (source type 'user')."""
        if not chat_id or not chat_id.startswith("U"):
            return
        await self._reply.show_loading(chat_id, seconds=60)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
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

        messages: list[dict[str, Any]] = [
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
            logger.warning("line: send_image push failed chat_id=%s: %s", chat_id, _scrub_token(str(exc)))
            return SendResult(success=False, error=_scrub_token(str(exc)))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
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

    @classmethod
    def _build_reply_messages(cls, text: str) -> list[dict[str, Any]]:
        """Build a LINE messages array from agent output, extracting any
        Markdown/HTML image URLs into native image bubbles before chunking
        the remaining text.

        Classmethod so ``tools/send_message_tool._send_line()`` can call it
        without instantiating an adapter — both the in-chat reply path and
        the cron/send_message Push path share the same media handling
        (codex review #4 P2).

        Without this, agent responses like ``![chart](https://...)`` render
        as raw text on the user's screen instead of inline images. LINE
        accepts up to 5 message objects per Reply/Push call; we keep the
        budget by capping image bubbles first (LINE rejects non-HTTPS URLs,
        so non-https images stay as text)."""
        # extract_images() yields HTTPS URLs only when they look like real
        # image assets (.png/.jpg/.webp/known CDNs); other links pass
        # through into the cleaned text — exactly what we want.
        images, cleaned = BasePlatformAdapter.extract_images(text)
        messages: list[dict[str, Any]] = []
        # LINE allows max 5 message objects per call. Reserve at least
        # 1 slot for text if we have any cleaned content.
        max_image_msgs = 4 if cleaned.strip() else 5
        for url, _alt in images[:max_image_msgs]:
            if url.startswith("https://"):
                messages.append({
                    "type": "image",
                    "originalContentUrl": url,
                    "previewImageUrl": url,
                })
        text_budget = 5 - len(messages)
        if cleaned.strip() and text_budget > 0:
            messages.extend(cls._chunk_text(cleaned)[:text_budget])
        # Defensive: never return empty (LINE rejects empty messages array).
        if not messages:
            messages = cls._chunk_text(text)[:5]
        return messages

    @staticmethod
    def _chunk_text(text: str) -> list[dict[str, Any]]:
        """Split text into LINE message segment dicts (max 5000 chars, max 5 per call).

        The 5-message cap matches LINE's per-call limit for both Reply and Push APIs.
        Responses longer than 25,000 chars are truncated; the last segment gets a
        "… (truncated)" suffix so users know the answer was cut off.
        Empty text returns a single placeholder so LINE never receives an empty messages array.
        """
        if not text:
            return [{"type": "text", "text": "(no response)"}]
        max_len = LineAdapter.MAX_MESSAGE_LENGTH
        all_chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
        truncated = len(all_chunks) > 5
        chunks = all_chunks[:5]
        if truncated:
            suffix = "\n… (truncated)"
            last = chunks[-1]
            # Always append suffix; trim last chunk if needed to stay within max_len.
            if len(last) + len(suffix) > max_len:
                last = last[:max_len - len(suffix)]
            chunks[-1] = last + suffix
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
            logger.warning("line: push_text failed chat_id=%s: %s", chat_id, _scrub_token(str(exc)))
            return SendResult(success=False, error=_scrub_token(str(exc)))

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
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
        self._test_idle_event.clear()
        self._test_button_sent_event.clear()
        cfg = {
            "users": self._cfg.allowed_users,
            "groups": self._cfg.allowed_groups,
            "rooms": self._cfg.allowed_rooms,
        }
        if not is_allowed(event, cfg):
            self._log_drop(event)
            self._test_idle_event.set()
            return  # silent drop

        # Pre-check: bot enabled but no LLM provider configured (Phase 1 /
        # pre-`hermes setup`). Reply with a friendly setup notice instead
        # of letting the dispatcher raise RuntimeError downstream. Applies
        # to both message and postback events. Allowlist already enforced
        # above, so unauthorized users still silent-drop. Only triggers
        # when the default _real_llm_call path is active — tests that
        # override _llm_call with a stub bypass this check.
        # NOTE: bound-method equality (`==`) compares __self__ + __func__,
        # which is exactly what we want here — `is` would always be False
        # because each `self.method` attribute access creates a fresh
        # bound-method object.
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
            # _handle_message starts async tasks; _test_idle_event is set by
            # the watcher's finally (or by early-return paths in _handle_message).
            await self._handle_message(event)
        elif event.get("type") == "postback":
            # _handle_postback is fully awaited — set the event when it completes.
            # try/finally guarantees test sync even if a reply API call raises.
            try:
                await self._handle_postback(event)
            finally:
                self._test_idle_event.set()
        else:
            # Unhandled event types (follow, join, leave, beacon, etc.).
            self._test_idle_event.set()

    # ---- Message handler ----

    async def _handle_message(self, event: dict[str, Any]) -> None:
        msg = event.get("message", {})
        if msg.get("type") != "text":
            logger.info("line: ignoring non-text message type=%s", msg.get("type"))
            self._test_idle_event.set()
            return
        text = msg.get("text", "")
        source = event.get("source", {})
        reply_token = event.get("replyToken")
        if not reply_token:
            logger.info(
                "line: dropping message event without replyToken: source=%s",
                source,
            )
            self._test_idle_event.set()
            return
        logger.info(
            "line: received message src_type=%s user=%s text=%r",
            source.get("type"),
            source.get("userId"),
            text[:80],
        )

        # Group mention gate — only respond when @-mentioned in group/room chats.
        # DMs (src_type == "user") are never gated.
        # Note: substring match on text (LINE delivers structured mention metadata
        # in event.message.mention.mentionees[]; matching that would be more robust
        # but requires a richer event-shape contract — substring is good enough for v1).
        src_type = source.get("type")
        # free_response_groups/rooms bypass the mention gate entirely — designate
        # "dedicated bot" group/room IDs here and the bot answers every message.
        free_response = (
            (src_type == "group"
             and source.get("groupId") in self._cfg.free_response_groups)
            or (src_type == "room"
                and source.get("roomId") in self._cfg.free_response_rooms)
        )
        if src_type in ("group", "room") and self._cfg.require_mention and not free_response:
            if not self._bot_display_name:
                # Fail-closed: gate is configured but bot name unresolved
                # (auto-fetch failed and no manual override). Block all
                # group/room messages so a misconfigured deployment doesn't
                # silently respond to everyone — operator must set
                # LINE_BOT_DISPLAY_NAME or fix the access token.
                logger.warning(
                    "line: require_mention=True but bot_display_name is empty — dropping group/room message"
                )
                self._test_idle_event.set()
                return
            trigger = f"@{self._bot_display_name}"
            if trigger not in text:
                logger.info(
                    "line: group message not addressed to bot — silent drop (trigger=%r)",
                    trigger,
                )
                self._test_idle_event.set()
                return
            # Strip every occurrence of the mention; collapse runs of whitespace
            # left behind so the LLM sees a clean question.
            text = re.sub(re.escape(trigger), "", text)
            text = re.sub(r"\s+", " ", text).strip()
            # Mention-only message ("@小茉" with no body) — drop instead of
            # dispatching an empty prompt to the LLM.
            if not text:
                logger.info(
                    "line: mention-only message — silent drop (trigger=%r)",
                    trigger,
                )
                self._test_idle_event.set()
                return

        # Same-chat serialization: if a previous turn is still in flight for
        # this chat, don't spawn a second concurrent agent. Acknowledge with
        # a Quick Reply button referencing the IN-FLIGHT request_id so the
        # user can fetch the previous answer when ready instead of racing
        # tool calls (codex/gemini review #2 P1).
        chat_key = (
            source.get("groupId")
            or source.get("roomId")
            or source.get("userId")
            or ""
        )
        in_flight_request_id = self._active_chats.get(chat_key) if chat_key else None
        if in_flight_request_id is not None:
            entry = self._cache.get(in_flight_request_id)
            if entry is not None and entry.state is State.PENDING:
                # Codex review #3 P1: bypass-allowed control commands (e.g.
                # /stop, /new, /approve, /status) must always reach the
                # gateway runner so an active LINE session is never trapped
                # behind the busy gate. Mirror the should_bypass_active_session
                # check that BasePlatformAdapter.handle_message uses.
                stripped = text.strip()
                bypass_cmd = False
                if stripped.startswith("/"):
                    from hermes_cli.commands import should_bypass_active_session
                    cmd_word = stripped.split(maxsplit=1)[0][1:]
                    bypass_cmd = should_bypass_active_session(cmd_word)
                if not bypass_cmd:
                    # Codex review #6 P2: this branch DROPS the follow-up text
                    # (no queue, no second agent run) to prevent races. Be
                    # honest about that in the user-facing message so they
                    # know to resend after the answer lands instead of
                    # assuming we silently captured "ignore that" / extra
                    # context. The dropped text stays in the user's LINE
                    # scrollback for easy retry.
                    busy_text = (
                        "🤔 Still answering your previous message — your latest "
                        "follow-up was not queued. Please resend it after the "
                        "response arrives. (Tap the button to fetch the previous "
                        "answer when ready.)"
                    )
                    quick_reply_msg = build_quick_reply_button_message(
                        text=busy_text,
                        button_label=SHOW_RESPONSE_BUTTON_LABEL,
                        request_id=in_flight_request_id,
                    )
                    try:
                        await self._reply.reply(reply_token, [quick_reply_msg])
                    except Exception:
                        logger.exception(
                            "line: failed to send 'still working' Quick Reply for chat=%s",
                            chat_key,
                        )
                    self._test_idle_event.set()
                    return

        # Show typing indicator in 1-on-1 chats (LINE limitation: groups don't support it).
        # Loading-indicator API auto-stops when the bot replies, so we max it
        # out at LINE's documented 60-second cap to keep the indicator visible
        # across the full slow-LLM Quick Reply window (45s threshold + margin).
        # Tracked in _background_tasks so disconnect() can drain it cleanly.
        if src_type == "user":
            user_id = source.get("userId")
            if user_id:
                loading_task = asyncio.create_task(self._reply.show_loading(user_id, seconds=60))
                self._background_tasks.add(loading_task)
                loading_task.add_done_callback(self._background_tasks.discard)
        request_id = self._cache.register_pending()
        if chat_key:
            self._active_chats[chat_key] = request_id

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
            finally:
                # Release the chat slot only if we still own it (defensive
                # against re-entrancy from /reset etc.)
                if chat_key and self._active_chats.get(chat_key) == request_id:
                    self._active_chats.pop(chat_key, None)

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
                    if entry and entry.state in (State.READY, State.ERROR):
                        # READY = LLM answer (route through media-extracting
                        # builder so ``![alt](https://...)`` lands as a native
                        # image bubble, not raw text); ERROR = formatted error
                        # string (no media).
                        if entry.state is State.READY:
                            messages = self._build_reply_messages(entry.payload)
                        else:
                            messages = self._chunk_text(entry.payload)
                        await self._reply.reply(reply_token, messages)
                        self._cache.mark_delivered(request_id)
                    else:
                        logger.error(
                            "line: cache entry missing or wrong state for request_id=%s "
                            "(pruned or cancelled?) — reply token likely expired",
                            request_id,
                        )
                except asyncio.TimeoutError:
                    quick_reply_msg = build_quick_reply_button_message(
                        text=PENDING_REPLY_TEXT,
                        button_label=SHOW_RESPONSE_BUTTON_LABEL,
                        request_id=request_id,
                    )
                    try:
                        await self._reply.reply(reply_token, [quick_reply_msg])
                        self._test_button_sent_event.set()
                    except Exception as exc:
                        # Reply token expired or LINE API error — the button was
                        # never delivered. The LLM answer will still be stored in
                        # the cache (READY) when it arrives, but the user has no
                        # button to tap. The answer will be lost at TTL expiry.
                        # Operators should pre-approve slow tools or reduce
                        # slow_response_threshold to leave a wider delivery window.
                        logger.warning(
                            "line: button delivery failed for request_id=%s "
                            "(token_prefix=%s) — user will not see 'Show response' button: %s",
                            request_id,
                            reply_token[:8] if reply_token else "?",
                            _scrub_token(str(exc)),
                        )
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
            await self._reply.reply(
                reply_token, self._build_reply_messages(entry.payload),
            )
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
        event: dict[str, Any] | None = None,
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
        handled by the underlying agent — operators must pre-approve
        trusted tools with /approve always in a LINE conversation, or
        the session will hang waiting for an approval prompt that can
        never reach the user.
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
        # Allow plain-text slash commands like ``/new`` and ``/reset`` to be
        # routed as gateway commands instead of being passed to the LLM
        # verbatim — mirrors the call in BasePlatformAdapter.handle_message.
        coerce_plaintext_gateway_command(msg_event)
        response = await self._message_handler(msg_event)
        return response or ""

    # ---- Test helpers (no-ops in production) ----

    async def wait_idle(self) -> None:
        self._ensure_test_events()
        await self._test_idle_event.wait()  # type: ignore[union-attr]

    async def wait_button_sent(self) -> None:
        self._ensure_test_events()
        await self._test_button_sent_event.wait()  # type: ignore[union-attr]
