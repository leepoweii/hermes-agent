# LINE /check-pending + Answer Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix silent answer-loss when a second LLM run completes while a postback button is still unclaimed, and surface all cached answers via a `/check-pending` command.

**Architecture:** Convert `_pending_buttons` from a single-slot `Dict[str, str]` to a queue `Dict[str, deque[str]]` so multiple answers per chat can be cached. A `/check-pending` text command is intercepted before reaching the LLM and replies with a Template Button for each retrievable answer. The LLM history stays linear — answer delivery is pure adapter infrastructure.

**Tech Stack:** Python 3.10+, aiohttp, existing `RequestCache` / `_CacheEntry` / `build_postback_button_message` in `plugins/platforms/line/adapter.py`

---

## File Map

| File | Change |
|------|--------|
| `plugins/platforms/line/adapter.py` | All logic changes (single file, zero core edits) |
| `tests/gateway/test_line_plugin.py` | New test classes + update existing `TestSendRouting` |

---

## Task 1 — Extend `RequestCache` data model

**Files:**
- Modify: `plugins/platforms/line/adapter.py:277-352`
- Test: `tests/gateway/test_line_plugin.py` → add to `TestRequestCache`

### Why
`_CacheEntry` has no `question_preview`. `RequestCache` has no way to create a READY entry directly (needed for a second answer that arrives after the first slot is already READY), and no way to list all retrievable entries for a chat.

- [ ] **Step 1: Write failing tests** — append to `TestRequestCache` in `tests/gateway/test_line_plugin.py`

```python
    def test_register_pending_stores_question_preview(self):
        c = RequestCache()
        rid = c.register_pending("Uchat", question_preview="What time is it?")
        assert c.get(rid).question_preview == "What time is it?"

    def test_register_pending_default_preview_is_empty(self):
        c = RequestCache()
        rid = c.register_pending("Uchat")
        assert c.get(rid).question_preview == ""

    def test_register_ready_creates_ready_entry(self):
        c = RequestCache()
        rid = c.register_ready("Uchat", "the answer", question_preview="q?")
        entry = c.get(rid)
        assert entry.state is State.READY
        assert entry.payload == "the answer"
        assert entry.chat_id == "Uchat"
        assert entry.question_preview == "q?"

    def test_list_retrievable_for_chat_returns_ready_and_error(self):
        c = RequestCache()
        rid_ready = c.register_pending("Uchat")
        c.set_ready(rid_ready, "ans1")
        rid_error = c.register_pending("Uchat")
        c.set_error(rid_error, "boom")
        rid_pending = c.register_pending("Uchat")   # still PENDING — excluded
        rid_other = c.register_pending("Uother")    # different chat — excluded
        c.set_ready(rid_other, "other")

        results = c.list_retrievable_for_chat("Uchat")
        rids = [r for r, _ in results]
        assert rid_ready in rids
        assert rid_error in rids
        assert rid_pending not in rids
        assert rid_other not in rids

    def test_list_retrievable_for_chat_empty_when_none(self):
        c = RequestCache()
        assert c.list_retrievable_for_chat("Uchat") == []
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/pwlee/Documents/Github/hermes-agent
python -m pytest tests/gateway/test_line_plugin.py::TestRequestCache -x -q 2>&1 | tail -15
```

Expected: `AttributeError` or `TypeError` on `question_preview` / `register_ready` / `list_retrievable_for_chat`.

- [ ] **Step 3: Implement the changes** in `plugins/platforms/line/adapter.py`

**3a.** Add `from collections import deque` after line 66 (`import enum`):

```python
import collections
```

(We use `collections.deque` throughout to avoid a bare `deque` name collision.)

**3b.** Replace `_CacheEntry` (lines 276-282) with:

```python
@dataclass
class _CacheEntry:
    state: State
    payload: Any = None
    chat_id: str = ""
    question_preview: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
```

**3c.** Replace `register_pending` (lines 302-305) with:

```python
    def register_pending(self, chat_id: str, question_preview: str = "") -> str:
        rid = str(uuid.uuid4())
        self._entries[rid] = _CacheEntry(
            state=State.PENDING,
            chat_id=chat_id,
            question_preview=question_preview,
        )
        return rid
```

**3d.** Add `register_ready` and `list_retrievable_for_chat` after `register_pending`:

```python
    def register_ready(self, chat_id: str, payload: Any, question_preview: str = "") -> str:
        """Create a cache entry that starts in READY state (second answer, no button fired)."""
        rid = str(uuid.uuid4())
        self._entries[rid] = _CacheEntry(
            state=State.READY,
            chat_id=chat_id,
            question_preview=question_preview,
            payload=payload,
        )
        return rid

    def list_retrievable_for_chat(self, chat_id: str) -> List[Tuple[str, "_CacheEntry"]]:
        """Return all READY or ERROR entries for a chat, in insertion order."""
        return [
            (rid, entry)
            for rid, entry in self._entries.items()
            if entry.chat_id == chat_id
            and entry.state in (State.READY, State.ERROR)
        ]
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/gateway/test_line_plugin.py::TestRequestCache -x -q 2>&1 | tail -10
```

Expected: all `TestRequestCache` tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/platforms/line/adapter.py tests/gateway/test_line_plugin.py
git commit -m "feat(line): extend RequestCache with register_ready, list_retrievable_for_chat, question_preview"
```

---

## Task 2 — Convert `_pending_buttons` to a queue + update all call sites

**Files:**
- Modify: `plugins/platforms/line/adapter.py:705-723, 1083-1086, 1183-1199, 1212-1217`
- Test: `tests/gateway/test_line_plugin.py` → update `TestSendRouting`

### Why
`_pending_buttons: Dict[str, str]` is a single slot per chat. We replace it with `Dict[str, collections.deque]`. There are exactly 5 call sites that need updating.

- [ ] **Step 1: Write failing tests** — add to `TestSendRouting` in the test file

```python
    def test_send_second_answer_queued_not_dropped(self, adapter):
        """Second LLM answer must not be silently dropped when first slot is READY."""
        from collections import deque
        rid1 = adapter._cache.register_pending("Uchat")
        adapter._cache.set_ready(rid1, "answer 1")            # slot is now READY
        adapter._pending_buttons["Uchat"] = deque([rid1])     # simulate existing queue

        result = asyncio.run(adapter.send("Uchat", "answer 2"))

        assert result.success
        adapter._client.reply.assert_not_called()
        adapter._client.push.assert_not_called()
        # rid1 unchanged
        assert adapter._cache.get(rid1).payload == "answer 1"
        # A new rid was added to the deque for answer 2
        q = adapter._pending_buttons["Uchat"]
        assert len(q) == 2
        rid2 = q[1]
        assert adapter._cache.get(rid2).state is State.READY
        assert adapter._cache.get(rid2).payload == "answer 2"

    def test_send_pending_button_uses_deque(self, adapter):
        """First answer to a PENDING slot still routes to the cache (deque version)."""
        from collections import deque
        rid = adapter._cache.register_pending("Uchat")
        adapter._pending_buttons["Uchat"] = deque([rid])

        result = asyncio.run(adapter.send("Uchat", "the answer"))
        assert result.success
        adapter._client.reply.assert_not_called()
        adapter._client.push.assert_not_called()
        assert adapter._cache.get(rid).state is State.READY
        assert adapter._cache.get(rid).payload == "the answer"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/gateway/test_line_plugin.py::TestSendRouting -x -q 2>&1 | tail -15
```

Expected: `test_send_second_answer_queued_not_dropped` fails (answer 2 is silently dropped by no-op).

- [ ] **Step 3: Implement the changes**

**3a.** In `__init__` replace line 723:

```python
        # Pending-button queue per chat. Each entry is a deque of request_ids
        # (oldest first). A new rid is appended when a second LLM answer arrives
        # before the user taps the first button.
        self._pending_buttons: Dict[str, collections.deque] = {}
```

**3b.** Replace `send()` routing block (lines 1081-1088):

```python
        # If the chat has a pending postback queue, route the response to it.
        # Find the oldest PENDING slot; if all slots are READY, open a new one
        # so the answer isn't lost (user can retrieve via /check-pending).
        pending_deque = self._pending_buttons.get(chat_id)
        if pending_deque:
            for rid in pending_deque:
                entry = self._cache.get(rid)
                if entry and entry.state is State.PENDING:
                    self._cache.set_ready(rid, content)
                    return SendResult(success=True, message_id=rid)
            # All slots already READY — queue a new one.
            new_rid = self._cache.register_ready(
                chat_id, content,
                question_preview=self._last_question.get(chat_id, ""),
            )
            pending_deque.append(new_rid)
            return SendResult(success=True, message_id=new_rid)

        return await self._send_text_chunks(chat_id, content, force_push=False)
```

Note: `self._last_question` is added in Task 3. For now add it to `__init__` (line ~724):

```python
        self._last_question: Dict[str, str] = {}
```

**3c.** Replace `_fire_postback` button registration (lines 1183-1199):

```python
            if self._pending_buttons.get(chat_id):
                return
            rid = self._cache.register_pending(
                chat_id,
                question_preview=self._last_question.get(chat_id, ""),
            )
            self._pending_buttons.setdefault(chat_id, collections.deque()).append(rid)
            token, used = self._consume_reply_token(chat_id)
            if not used:
                q = self._pending_buttons.get(chat_id)
                if q:
                    try:
                        q.remove(rid)
                    except ValueError:
                        pass
                    if not q:
                        self._pending_buttons.pop(chat_id, None)
                return
            msg = build_postback_button_message(
                self.pending_text, self.button_label, rid
            )
            try:
                await self._client.reply(token, [msg])
                logger.info("LINE: sent slow-LLM postback button for chat %s (rid=%s)", chat_id, rid)
            except Exception as exc:
                logger.warning("LINE: postback button send failed: %s", exc)
                q = self._pending_buttons.get(chat_id)
                if q:
                    try:
                        q.remove(rid)
                    except ValueError:
                        pass
                    if not q:
                        self._pending_buttons.pop(chat_id, None)
```

**3d.** Replace `_handle_postback_event` cleanup (3 occurrences of `self._pending_buttons.pop(chat_id, None)` at lines 1012, 1018, 1026). Replace each with:

```python
                    _remove_from_pending_queue(self._pending_buttons, chat_id, request_id)
```

And add this module-level helper just above the `LineAdapter` class definition:

```python
def _remove_from_pending_queue(
    pending: Dict[str, "collections.deque"],
    chat_id: str,
    request_id: str,
) -> None:
    """Remove a specific rid from the per-chat deque; prune the key when empty."""
    q = pending.get(chat_id)
    if not q:
        return
    try:
        q.remove(request_id)
    except ValueError:
        pass
    if not q:
        pending.pop(chat_id, None)
```

**3e.** Replace `interrupt_session_activity` (lines 1212-1217):

```python
    async def interrupt_session_activity(self, session_key: str, chat_id: str) -> None:
        """Resolve all orphan PENDING postbacks so buttons don't loop."""
        await super().interrupt_session_activity(session_key, chat_id)
        pending_deque = self._pending_buttons.pop(chat_id, None)
        if pending_deque:
            for rid in pending_deque:
                self._cache.set_error(rid, self.interrupted_text)
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/gateway/test_line_plugin.py::TestSendRouting -x -q 2>&1 | tail -15
```

Expected: all `TestSendRouting` tests pass, including new deque tests.

Also run the full suite to catch regressions:

```bash
python -m pytest tests/gateway/test_line_plugin.py -q 2>&1 | tail -15
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/platforms/line/adapter.py tests/gateway/test_line_plugin.py
git commit -m "feat(line): convert _pending_buttons to deque — second answer queued, not dropped"
```

---

## Task 3 — Track last question text for preview labels

**Files:**
- Modify: `plugins/platforms/line/adapter.py:940-941` (`_handle_message_event` text branch)

### Why
`_fire_postback` needs to store a `question_preview` in the cache entry so `/check-pending` buttons have meaningful labels. We store the most recent text per `chat_id` in `self._last_question`.

- [ ] **Step 1: Write failing test** — add to `TestSendRouting`

```python
    def test_last_question_stored_on_text_message(self, adapter):
        """Text message handler must update _last_question for postback label use."""
        # Simulate a minimal inbound text event through _handle_message_event.
        event = {
            "type": "message",
            "replyToken": "rt",
            "source": {"type": "user", "userId": "Uchat"},
            "message": {"type": "text", "id": "m1", "text": "What time is it?"},
        }
        adapter.handle_message = AsyncMock()
        asyncio.run(adapter._handle_message_event(event))
        assert adapter._last_question.get("Uchat") == "What time is it?"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
python -m pytest tests/gateway/test_line_plugin.py::TestSendRouting::test_last_question_stored_on_text_message -x -q 2>&1 | tail -10
```

Expected: `AssertionError` (key not found in `_last_question`).

- [ ] **Step 3: Implement** — in `_handle_message_event`, inside the `if msg_type == "text":` branch (around line 941), add one line after `text = msg.get("text", "") or ""`:

```python
        if msg_type == "text":
            text = msg.get("text", "") or ""
            if chat_id and text:
                self._last_question[chat_id] = text[:160]
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
python -m pytest tests/gateway/test_line_plugin.py::TestSendRouting::test_last_question_stored_on_text_message -x -q 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/platforms/line/adapter.py tests/gateway/test_line_plugin.py
git commit -m "feat(line): track last question text per chat for postback preview labels"
```

---

## Task 4 — `/check-pending` command intercept

**Files:**
- Modify: `plugins/platforms/line/adapter.py` — add `_handle_check_pending`, update `_handle_message_event`
- Test: `tests/gateway/test_line_plugin.py` — add `TestCheckPending`

### Why
Users need a way to retrieve queued answers. `/check-pending` is intercepted before `handle_message`, uses the inbound reply token, and sends one Template Button per retrievable answer.

- [ ] **Step 1: Write failing tests** — add new class at end of test file

```python
# ---------------------------------------------------------------------------
# 9. /check-pending command
# ---------------------------------------------------------------------------

class TestCheckPending:

    @pytest.fixture
    def adapter(self, monkeypatch):
        monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True, extra={
            "channel_access_token": "tok",
            "channel_secret": "sec",
        })
        ad = LineAdapter(cfg)
        ad._client = MagicMock()
        ad._client.reply = AsyncMock()
        ad._client.push = AsyncMock()
        ad.handle_message = AsyncMock()
        return ad

    def _make_text_event(self, text, chat_id="Uchat", reply_token="rt-123"):
        return {
            "type": "message",
            "replyToken": reply_token,
            "source": {"type": "user", "userId": chat_id},
            "message": {"type": "text", "id": "m1", "text": text},
        }

    def test_check_pending_not_forwarded_to_llm(self, adapter):
        asyncio.run(adapter._handle_message_event(
            self._make_text_event("/check-pending")
        ))
        adapter.handle_message.assert_not_called()

    def test_check_pending_replies_no_pending_when_queue_empty(self, adapter):
        asyncio.run(adapter._handle_message_event(
            self._make_text_event("/check-pending")
        ))
        adapter._client.reply.assert_called_once()
        call_messages = adapter._client.reply.call_args.args[1]
        assert call_messages[0]["type"] == "text"
        assert "No pending" in call_messages[0]["text"]

    def test_check_pending_replies_still_working_when_only_pending(self, adapter):
        from collections import deque
        rid = adapter._cache.register_pending("Uchat")
        adapter._pending_buttons["Uchat"] = deque([rid])   # PENDING, not READY

        asyncio.run(adapter._handle_message_event(
            self._make_text_event("/check-pending")
        ))
        adapter._client.reply.assert_called_once()
        call_messages = adapter._client.reply.call_args.args[1]
        assert call_messages[0]["type"] == "text"
        # Should mention still working, not send a button
        assert "template" not in str(call_messages[0])

    def test_check_pending_sends_button_for_each_ready_answer(self, adapter):
        from collections import deque
        rid1 = adapter._cache.register_ready("Uchat", "answer 1", question_preview="Q1")
        rid2 = adapter._cache.register_ready("Uchat", "answer 2", question_preview="Q2")
        adapter._pending_buttons["Uchat"] = deque([rid1, rid2])

        asyncio.run(adapter._handle_message_event(
            self._make_text_event("/check-pending", reply_token="rt-456")
        ))

        adapter._client.reply.assert_called_once()
        token_used, messages = adapter._client.reply.call_args.args
        assert token_used == "rt-456"
        assert len(messages) == 2
        assert messages[0]["type"] == "template"
        assert messages[1]["type"] == "template"
        # Verify request_ids are embedded
        data0 = json.loads(messages[0]["template"]["actions"][0]["data"])
        data1 = json.loads(messages[1]["template"]["actions"][0]["data"])
        assert data0["request_id"] == rid1
        assert data1["request_id"] == rid2

    def test_check_pending_uses_question_preview_as_bubble_text(self, adapter):
        from collections import deque
        rid = adapter._cache.register_ready("Uchat", "answer", question_preview="What time?")
        adapter._pending_buttons["Uchat"] = deque([rid])

        asyncio.run(adapter._handle_message_event(
            self._make_text_event("/check-pending")
        ))
        call_messages = adapter._client.reply.call_args.args[1]
        assert "What time?" in call_messages[0]["template"]["text"]

    def test_check_pending_caps_at_five_buttons(self, adapter):
        from collections import deque
        rids = [adapter._cache.register_ready("Uchat", f"ans{i}") for i in range(8)]
        adapter._pending_buttons["Uchat"] = deque(rids)

        asyncio.run(adapter._handle_message_event(
            self._make_text_event("/check-pending")
        ))
        call_messages = adapter._client.reply.call_args.args[1]
        assert len(call_messages) <= 5

    def test_normal_message_still_forwarded_to_llm(self, adapter):
        asyncio.run(adapter._handle_message_event(
            self._make_text_event("hello world")
        ))
        adapter.handle_message.assert_called_once()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/gateway/test_line_plugin.py::TestCheckPending -x -q 2>&1 | tail -15
```

Expected: `AttributeError` or tests failing because `/check-pending` falls through to `handle_message`.

- [ ] **Step 3: Implement `_handle_check_pending`** — add after `_handle_postback_event` in `adapter.py`:

```python
    async def _handle_check_pending(self, chat_id: str, reply_token: str) -> None:
        """Reply with Template Buttons for all READY/ERROR cached answers."""
        if not self._client or not reply_token:
            return

        pending_deque = self._pending_buttons.get(chat_id)
        if not pending_deque:
            try:
                await self._client.reply(reply_token, [_text_message("No pending answers.")])
            except Exception as exc:
                logger.warning("LINE: /check-pending reply failed: %s", exc)
            return

        retrievable = [
            (rid, self._cache.get(rid))
            for rid in pending_deque
            if self._cache.get(rid) is not None
            and self._cache.get(rid).state in (State.READY, State.ERROR)
        ]

        if not retrievable:
            try:
                await self._client.reply(reply_token, [_text_message("Still working on it...")])
            except Exception as exc:
                logger.warning("LINE: /check-pending still-working reply failed: %s", exc)
            return

        messages = []
        for rid, entry in retrievable[:LINE_MAX_MESSAGES_PER_CALL]:
            preview = (entry.question_preview or "Pending answer")[:160]
            messages.append(
                build_postback_button_message(preview, "Get answer", rid)
            )

        try:
            await self._client.reply(reply_token, messages)
            logger.info(
                "LINE: /check-pending sent %d button(s) to %s", len(messages), chat_id
            )
        except Exception as exc:
            logger.warning("LINE: /check-pending reply failed: %s", exc)
```

- [ ] **Step 4: Wire intercept in `_handle_message_event`** — add inside the `if msg_type == "text":` block, after the `self._last_question` line:

```python
        if msg_type == "text":
            text = msg.get("text", "") or ""
            if chat_id and text:
                self._last_question[chat_id] = text[:160]
            if text.strip() == "/check-pending":
                await self._handle_check_pending(chat_id, reply_token)
                return
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/gateway/test_line_plugin.py::TestCheckPending -x -q 2>&1 | tail -15
```

Expected: all 7 `TestCheckPending` tests pass.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/gateway/test_line_plugin.py -q 2>&1 | tail -15
```

Expected: all tests pass (no regressions).

- [ ] **Step 7: Commit**

```bash
git add plugins/platforms/line/adapter.py tests/gateway/test_line_plugin.py
git commit -m "feat(line): add /check-pending command — lists all queued answers as tappable buttons"
```

---

## Task 5 — Update module docstring + PR prep

**Files:**
- Modify: `plugins/platforms/line/adapter.py:1-59` (docstring)

- [ ] **Step 1: Update the module docstring** — add a new section after the existing design highlights:

In the `Design highlights` section of the module docstring, add:

```
**Answer queue + /check-pending.** When a second LLM answer arrives while the
first postback button is still unclaimed, it is stored in a per-chat deque
rather than silently dropped. Users can type ``/check-pending`` to get a
Template Button for each queued answer (intercepted before the LLM, free via
reply token). The deque is drained by ``interrupt_session_activity`` on
session stop.
```

- [ ] **Step 2: Run full suite one final time**

```bash
python -m pytest tests/gateway/test_line_plugin.py -v 2>&1 | tail -30
```

Expected: all tests pass. Count should be original 73 + ~13 new = ~86 tests.

- [ ] **Step 3: Final commit**

```bash
git add plugins/platforms/line/adapter.py
git commit -m "docs(line): document answer queue and /check-pending in module docstring"
```

- [ ] **Step 4: Push feature branch**

```bash
git push -u origin feat/line-check-pending-queue
```

---

## Self-Review

**Spec coverage:**
- ✅ Silent answer-loss bug fixed (`send()` deque routing, Task 2)
- ✅ `/check-pending` command intercept (Task 4)
- ✅ Question preview in button labels (Task 3 + Task 4)
- ✅ `interrupt_session_activity` drains whole deque (Task 2 step 3e)
- ✅ `_handle_postback_event` removes specific rid, not whole key (Task 2 step 3d)
- ✅ Tests for all new behavior (Tasks 1–4)
- ✅ No core edits — changes are within `plugins/platforms/line/` only

**Type consistency check:**
- `register_pending(chat_id, question_preview="")` — used consistently in Tasks 1, 2, 3
- `register_ready(chat_id, payload, question_preview="")` — defined Task 1, used Task 2
- `list_retrievable_for_chat(chat_id)` → `List[Tuple[str, _CacheEntry]]` — defined Task 1, not used in adapter (only in tests), can be used in future
- `_pending_buttons: Dict[str, collections.deque]` — set Task 2, read in `send()`, `_fire_postback`, `interrupt_session_activity`, `_handle_check_pending`
- `_last_question: Dict[str, str]` — set Task 2 (`__init__`), written Task 3, read Task 2 (`send()`) and Task 2 (`_fire_postback`)
- `_remove_from_pending_queue(pending, chat_id, request_id)` — defined Task 2, called in `_handle_postback_event` (3×)

**No placeholders found.**
