# Handoff Notes
_Last updated: 2026-05-14 23:59_

## 本次完成

- **探索 PR #23197**（NousResearch/hermes-agent）：maintainer 合成 7 支社群 PR 的 LINE adapter，其中包含原作者的 PR #18153
- **發現 silent answer-loss bug**：`_pending_buttons` 是單一 slot dict；當第一個 postback button 尚未被 tap 時，第二個 LLM 答案抵達會被 `set_ready()` no-op 靜默丟棄
- **設計並實作完整修復**，分 5 個 task，全部 subagent-driven development 完成：
  - Task 1：`RequestCache` 擴充 `question_preview`、`register_ready()`、`list_retrievable_for_chat()`
  - Task 2：`_pending_buttons` 從 `Dict[str, str]` 改為 `Dict[str, collections.deque]`，`send()` routing 修正
  - Task 3：`_last_question` 記錄每個 chat 的最後提問文字（button label 用）
  - Task 4：`/check-pending` 命令攔截，回傳所有 queued 答案的 Template Button
  - Task 5：module docstring 更新，feature branch push 到 origin
- **額外 fix**：`/check-pending` 不能汙染 `_last_question`（intercept 移到 store 之前）；cleanup triple `_cache.get()` call
- **Branch pushed**：`feat/line-check-pending-queue` → origin
- **88/88 tests passing**

## 進行中 / 未完成

- **PR 尚未開**：`feat/line-check-pending-queue` 在 fork 的 origin，還沒對 NousResearch/hermes-agent 開 PR
  - 停在：branch 已 push，等使用者找時間 review 後再開

## 下次建議從這裡開始

1. **Review feature branch**：`git log feat/line-check-pending-queue --oneline` 或直接看 GitHub fork，確認 7 commits 沒問題
2. **開 PR**：`gh pr create --repo NousResearch/hermes-agent --head <fork>:feat/line-check-pending-queue --base main`
   - PR 標題建議：`fix(line): fix silent answer-loss with deque queue + /check-pending command`
   - 描述需說明：silent drop bug 原因、deque 解法、`/check-pending` UX
3. **Optional**：確認 hermes-agent 的 upstream 是否有 `.github/CONTRIBUTING.md` 或 PR template

## 重要 context

- **這是 fork**，不是直接 clone upstream。upstream 是 NousResearch/hermes-agent。
- **PR #23197** 是 maintainer 合成版，已 merge 到 upstream main，本次工作是在此基礎上加新功能
- **`create_source` AttributeError** 是 upstream 已知 bug，upstream commit `7c6709732` 已修為 `build_source`，本 branch 已包含此 fix
- **LINE Template Button 限制**：每次 reply 最多 5 則（`LINE_MAX_MESSAGES_PER_CALL`），`/check-pending` 已遵守此限制
- **Push API vs Reply Token**：所有 `/check-pending` 回應都用 reply token（免費），不用 Push API（計費）
- **Trello**：使用者說「save to TRELLO 之後我找時間 review」→ 已建立 Trello card（見 Step 2）
