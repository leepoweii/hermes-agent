import asyncio
import time

import pytest

from gateway.platforms.line.cache import (
    RequestCache,
    State,
    CacheEntry,
)


def test_register_pending_returns_uuid():
    cache = RequestCache(ttl_seconds=3600)
    rid = cache.register_pending()
    assert isinstance(rid, str)
    entry = cache.get(rid)
    assert entry is not None
    assert entry.state is State.PENDING


def test_set_ready_transitions_state():
    cache = RequestCache(ttl_seconds=3600)
    rid = cache.register_pending()
    cache.set_ready(rid, "the answer")
    entry = cache.get(rid)
    assert entry.state is State.READY
    assert entry.payload == "the answer"


def test_set_ready_unknown_id_is_noop():
    cache = RequestCache(ttl_seconds=3600)
    cache.set_ready("not-a-real-id", "ignored")  # must not raise


def test_mark_delivered_transitions_state():
    cache = RequestCache(ttl_seconds=3600)
    rid = cache.register_pending()
    cache.set_ready(rid, "x")
    cache.mark_delivered(rid)
    entry = cache.get(rid)
    assert entry.state is State.DELIVERED


def test_prune_removes_only_old_ready_and_delivered(monkeypatch):
    cache = RequestCache(ttl_seconds=10)
    rid_pending = cache.register_pending()
    rid_old_ready = cache.register_pending()
    cache.set_ready(rid_old_ready, "stale")
    rid_old_delivered = cache.register_pending()
    cache.set_ready(rid_old_delivered, "x")
    cache.mark_delivered(rid_old_delivered)

    # Backdate the two terminal entries
    now = time.time()
    cache._entries[rid_old_ready].updated_at = now - 11
    cache._entries[rid_old_delivered].updated_at = now - 11

    cache.prune()

    assert cache.get(rid_pending) is not None  # PENDING never pruned
    assert cache.get(rid_old_ready) is None
    assert cache.get(rid_old_delivered) is None


def test_prune_keeps_recent_entries():
    cache = RequestCache(ttl_seconds=3600)
    rid = cache.register_pending()
    cache.set_ready(rid, "x")
    cache.prune()
    assert cache.get(rid) is not None
