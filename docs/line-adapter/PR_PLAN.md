# LINE Adapter — Upstream PR Plan

**Branch:** `feat/line-adapter` @ `leepoweii/hermes-agent`
**Total commits ahead of `NousResearch/hermes-agent` main:** 24
**Tests:** 65 passing + 1 skipped
**Status:** Real LINE round-trip verified on 2026-04-27

---

## Out of scope (NOT our concern)

- **Outline MCP connection failures** observed during local testing (`Connect call failed ('::1', 3000)`) are config-side issues on the operator's machine — no LINE-adapter code touches Outline. Don't mention in PR.

---

## What to PR upstream

All 24 commits on `feat/line-adapter` are in scope for upstream — they're all generic LINE platform support, no raise-a-bull specifics. The branch can be PR'd as a single PR (cleaner) or split into two (more reviewable).

### Option A: Single PR (recommended for first attempt)

Submit the whole branch with the title:

> **feat: LINE Messaging API platform adapter**

Body uses the template from the overnight execution report.

### Option B: Split into 2 PRs

If reviewers prefer smaller chunks:

**PR #1 — Core adapter (15 commits):**
- `cd6baca9` Adapter skeleton + smoke test
- `5921f794` Add line-bot-sdk v3 dependency
- `0bd41fc5` + `15540fc6` HMAC validation + event parsing
- `cef2c704` + `2b0348e9` Source allowlist (user/group/room)
- `52c931fc` + `198ec860` Request cache with PENDING/READY/DELIVERED + ceiling TTL
- `50deaaf2` Reply client + quick reply postback button builder
- `90fcd13d` + `ea4fb7e3` Dispatch — fast/slow LLM paths
- `f0410407` + `9bf51c29` Postback handler
- `0387bd2c` + `8b620881` Wire to `_message_handler` + suppress incidental sends
- `6592bf3f` HTTP webhook endpoint + signature middleware
- `27be9e24` from_env loader + runner factory wire-up

**PR #2 — Polish + integration (9 commits):**
- `69766d9b` Setup wizard LINE section
- `553cbb80` hermes-agent skill platform list + docs/messaging/line.md
- `dc5a8260` Codex-found criticals — auth map + empty-allowlist docs + LLM error state
- `87aa5f38` setup.py wording fixes
- `07398e65` Pre-check for unconfigured bot
- `2fabed4d` **Critical**: add LINE to `PLATFORMS` registry (fixes runtime KeyError)
- `2eaa6ad6` Typing indicator (loading animation) for 1-on-1

---

## Files touched (full inventory)

### New module — pure new code
- `gateway/platforms/line/__init__.py`
- `gateway/platforms/line/adapter.py`
- `gateway/platforms/line/allowlist.py`
- `gateway/platforms/line/cache.py`
- `gateway/platforms/line/reply.py`
- `gateway/platforms/line/webhook.py`

### Existing files modified
- `gateway/config.py` — added `Platform.LINE` enum + auto-enable in `_apply_env_overrides`
- `gateway/run.py` — `_create_adapter` factory branch + `_is_user_authorized` maps (`platform_env_map` + `platform_allow_all_map`)
- `gateway/platforms/__init__.py` — registered `LineAdapter`
- `hermes_cli/platforms.py` — added LINE to `PLATFORMS` registry (with `default_toolset="hermes-line"`)
- `hermes_cli/setup.py` — added `_setup_line()` wizard function + registered in `_GATEWAY_PLATFORMS` + included in `any_messaging` post-check
- `pyproject.toml` — added `line-bot-sdk>=3.11,<4` to messaging extra; `respx` + `pytest-aiohttp` to dev extra
- `skills/autonomous-ai-agents/hermes-agent/SKILL.md` — LINE added to platform list

### New files
- `docs/messaging/line.md` — setup guide
- `tests/gateway/platforms/line/` — full test suite (`__init__.py`, `conftest.py`, `test_adapter_dispatch.py`, `test_allowlist.py`, `test_cache.py`, `test_config.py`, `test_http_endpoint.py`, `test_real_llm_call.py`, `test_reply.py`, `test_runner_integration.py`, `test_runner_smoke.py`, `test_send_suppression.py`, `test_smoke.py`, `test_webhook.py`)

---

## Architectural decisions to call out in PR description

1. **Bypasses `BasePlatformAdapter.handle_message()`** — calls registered `_message_handler` directly. Reason: `handle_message` auto-delivers via `self.send()`, which would force LINE Push API (paid). Trade-offs documented in `docs/messaging/line.md`:
   - Loses adapter-level concurrency queue + `/stop /approve /deny /new /reset` slash commands
   - Loses multimodal post-processing (image URLs / MEDIA: tags appear as raw text)
   - Approval prompts can't reach user — operator MUST set `HERMES_AUTO_APPROVE_TOOLS=1` (adapter logs WARNING if unset)

2. **Reply API only — no Push API.** 60-second window. On slow LLM (>50s) sends Quick Reply postback button; user taps to receive cached answer. Zero per-message cost.

3. **Cache state machine in-memory dict** (`PENDING`/`READY`/`DELIVERED`/`ERROR`). 1h TTL terminal, 24h ceiling for `PENDING`. Container restart drops `PENDING` (acceptable — users see `答案已過期` via cache miss).

4. **Allowlist for user/group/room** — silent drop with structured `line.drop` log line so admins can discover new group/room IDs (LINE Console has no UI for this).

5. **Auto-enabled via env vars** — `LINE_CHANNEL_ACCESS_TOKEN` + `LINE_CHANNEL_SECRET` both set → `Platform.LINE` added to gateway config (mirrors Telegram pattern in `_apply_env_overrides`).

6. **Standalone HTTP server in `connect()`** — no shared aiohttp app exists in Hermes (each platform owns its own, like `WebhookAdapter`). Default port `LINE_WEBHOOK_PORT=8645`.

---

## Bugs the PR fixes (caught during real-world testing)

- **Auth map gap** (commit `dc5a8260`): `Platform.LINE` was missing from `GatewayRunner._is_user_authorized()` maps in upstream. Without this addition, every LINE message would silently fail authorization. Codex Round 1 caught this before any user impact.

- **Tools config KeyError** (commit `2fabed4d`): `hermes_cli/platforms.py` `PLATFORMS` registry didn't include LINE. Real user messages crashed with `KeyError: 'line'` in `_get_platform_tools`. Caught during live testing — bot replied "Sorry, I encountered an error (KeyError) 'line'" until fixed. **This commit is essential — without it, no LINE deployment can run any LLM agent.**

---

## Known limitations to mention in PR (acknowledged trade-offs)

- Pending message drain doesn't work for LINE (bypass-architecture) — follow-up messages received during an active LLM run are not queued.
- Tool-approval prompts cannot reach LINE users (suppressed `send`).
- Multimodal output (images/files) leaks as raw text/URLs.

If reviewers want any of these fixed before merge, scope-discussion follows.

---

## Pre-PR checklist

- [ ] Rebase against latest `upstream/main`
- [ ] Re-run all tests: `uv run pytest tests/gateway/platforms/line/`
- [ ] Run repo-wide tests to ensure no regression: `uv run pytest`
- [ ] Run linter/formatter per repo conventions: `ruff check`, `ruff format --check`
- [ ] Open PR with body from this plan + screenshots from `/tmp/line-final.png`
- [ ] Be ready to split into 2 PRs if requested

---

## After PR merges (if it does)

- raise-a-bull v2 can drop the fork dependency and use upstream `nousresearch/hermes-agent` Docker image directly
- Update raise-a-bull v2 `docker-compose.yml` and `zeabur.yaml` accordingly
- Archive `pwlee/hermes-agent` fork repo (or keep for future contributions)

---

## After PR rejection (worst case)

- Maintain `pwlee/hermes-agent` fork on a long-lived `feat/line-adapter` branch
- Set up GitHub Actions to weekly rebase against `upstream/main` + auto-build a derived Docker image (`ghcr.io/pwlee/hermes-with-line:<upstream-version>`)
- raise-a-bull v2 always references the GHCR image
- Ultimate fallback: replace LINE adapter with an external `line-proxy` service that translates LINE webhooks ↔ Hermes generic webhook gateway (no fork needed)
