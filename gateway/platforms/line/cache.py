"""In-memory cache of pending/ready LINE LLM responses, keyed by request_id.

State machine (per design spec):
    PENDING   → LLM still running, no answer yet
    READY     → LLM done, answer cached, waiting for postback tap
    DELIVERED → answer already sent

Storage: dict in the adapter process. Not Redis-backed — container restart
drops PENDING entries (acceptable trade-off; users see "答案已過期").

Two TTLs:
    - ``ttl_seconds`` (default 1h) — applies to READY/DELIVERED entries,
      keyed off ``updated_at`` (when the entry transitioned).
    - ``pending_ttl_seconds`` (default 24h) — ceiling TTL for PENDING
      entries, keyed off ``created_at`` (when registered).

Contract: callers SHOULD always reach a terminal state (READY or DELIVERED)
via ``set_ready()`` / ``mark_delivered()``. The PENDING ceiling TTL is a
defensive bound to prevent pathological leaks when a task is cancelled or
otherwise never completes — without it, a long-running container would
accumulate PENDING entries forever.

Not safe across threads — single-event-loop only.

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
                entry.state in (State.READY, State.DELIVERED)
                and entry.updated_at < terminal_cutoff
            )
            or (
                entry.state is State.PENDING
                and entry.created_at < pending_cutoff
            )
        ]
        for rid in stale:
            del self._entries[rid]
