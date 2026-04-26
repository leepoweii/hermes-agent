"""In-memory cache of pending/ready LINE LLM responses, keyed by request_id.

State machine (per design spec):
    PENDING   → LLM still running, no answer yet
    READY     → LLM done, answer cached, waiting for postback tap
    DELIVERED → answer already sent

Storage: dict in the adapter process. Not Redis-backed — container restart
drops PENDING entries (acceptable trade-off; users see "答案已過期").

TTL: only applies to READY/DELIVERED. PENDING never times out via TTL —
it transitions on LLM completion or task cancellation.

Pattern borrowed from gateway/platforms/webhook.py (_delivery_info,
_idempotency_ttl=3600, _prune_delivery_info).
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class State(enum.Enum):
    PENDING = "pending"
    READY = "ready"
    DELIVERED = "delivered"


@dataclass
class CacheEntry:
    state: State
    payload: Any = None  # the LLM response, when READY
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class RequestCache:
    """In-memory `dict[request_id, CacheEntry]` with TTL pruning of terminal states."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._ttl = ttl_seconds

    def register_pending(self) -> str:
        rid = str(uuid.uuid4())
        self._entries[rid] = CacheEntry(state=State.PENDING)
        return rid

    def get(self, request_id: str) -> CacheEntry | None:
        return self._entries.get(request_id)

    def set_ready(self, request_id: str, payload: Any) -> None:
        entry = self._entries.get(request_id)
        if entry is None:
            return  # task was cancelled / cache was wiped — nothing to do
        entry.state = State.READY
        entry.payload = payload
        entry.updated_at = time.time()

    def mark_delivered(self, request_id: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None:
            return
        entry.state = State.DELIVERED
        entry.updated_at = time.time()

    def prune(self) -> None:
        """Remove READY/DELIVERED entries older than TTL. PENDING is never pruned."""
        cutoff = time.time() - self._ttl
        stale = [
            rid
            for rid, entry in self._entries.items()
            if entry.state in (State.READY, State.DELIVERED)
            and entry.updated_at < cutoff
        ]
        for rid in stale:
            del self._entries[rid]
