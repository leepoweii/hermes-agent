---
sidebar_position: 4
title: "LINE"
description: "Set up Hermes Agent as a LINE Messaging API bot"
---

# LINE Messaging API

Hermes can run as a LINE bot in 1-on-1 chats, groups, and multi-person rooms.

## Prerequisites

1. A LINE Official Account (free): <https://www.linebiz.com/jp-en/manual/OfficialAccountManager/registration/>
2. A Messaging API channel under that account: <https://developers.line.biz/console/>
3. A publicly reachable HTTPS URL for the webhook (Hermes gateway). For local development use ngrok, cloudflared, or Tailscale Serve. For production use your deploy provider's HTTPS endpoint.

## Setup

Run the wizard:

```bash
hermes setup
```

Choose **LINE** when prompted. You'll be asked for:

- **Channel access token** — LINE Developers Console → your channel → Messaging API → Channel access token (long-lived).
- **Channel secret** — Same page → Basic settings → Channel secret.
- **Allowed users (CSV)** — LINE user IDs in 1-on-1 chats that may message the bot. Find your own ID in Basic settings → "Your user ID".
- **Allowed groups (CSV)** — LINE group IDs (start with `C`). See "Discovering group/room IDs" below.
- **Allowed rooms (CSV)** — LINE room IDs (start with `R`).

> **Important:** All three allowlists are independent and **default-deny**. An empty list = **no access** for that source type (1-on-1 / group / room) — messages are silently dropped. There is no "leave empty for open access" mode; to allow every sender on a source type during debugging, set `LINE_ALLOW_ALL_USERS=true` in `~/.hermes/.env` (debug only — bypasses all three allowlists).

## Webhook URL

After Hermes starts the gateway, register the webhook URL with LINE:

```
https://<your-gateway-host>/line/webhook
```

The default port is **8645** (override with `LINE_WEBHOOK_PORT`).

LINE Developers Console → Messaging API → Webhook URL → paste the URL → **Verify** → **Use webhook = on**.

## Discovering group/room IDs

There is no UI in the LINE Developers Console to list group IDs. The adapter logs every dropped (unauthorized) source so you can capture the ID after-the-fact:

1. Add the bot to the target group/room.
2. Ask anyone in the group/room to send any message to the bot.
3. Read the gateway log:

   ```bash
   docker logs <hermes-container> 2>&1 | grep line.drop
   ```

   Output line:
   ```
   line.drop unauthorised src_type=group user=Uxxxx group=Cyyyy room=None
   ```

4. Add `Cyyyy` to `LINE_ALLOWED_GROUPS` (re-run `hermes setup` or edit `~/.hermes/.env` directly).
5. Restart Hermes.

## How responses work

- Replies use the **LINE Reply API** (free, 60-second token window).
- If an LLM response takes longer than ~50 seconds, the bot sends a Quick Reply button (`📋 Show response`, overridable via `LINE_BUTTON_LABEL`). When any user in the chat taps it, the cached answer is delivered using a fresh reply token from the postback event.
- **No Push API is used** — there is no per-message cost.

## Group / room behaviour

- All members in an allowed group share the same Hermes session (`group_sessions_per_user: false` recommended). This is the natural "team-style" UX. Override in config if you want per-user isolation.
- Unauthorized members in an allowed group **do not** trigger a binding flow — the group itself is the trust boundary. Whoever the LINE group admin added is trusted by Hermes.

## Tool-approval prompts

The LINE adapter suppresses incidental `self.send()` calls (cost-saving — avoids LINE Push API). This means **dangerous-command approval prompts cannot reach the user** — sessions will hang on any approval-gated tool call. To mitigate: pre-approve trusted commands with `/approve always` in a LINE conversation, or configure the agent to avoid triggering dangerous-command gates (shell execution, file deletion).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Webhook verification fails on LINE Console | Wrong URL, or HMAC mismatch (channel secret typo). |
| Bot never replies in a group | Group ID not in `LINE_ALLOWED_GROUPS`; check `docker logs <container> \| grep line.drop`. |
| Reply token expired error | LLM exceeded 60s and the postback flow also failed; check that Quick Reply payload includes a valid `request_id`. |
| Bot replies "Response expired — please ask again." | Cache TTL (1 hour) elapsed, or container restarted while answer was PENDING. |
| Tool calls never complete (session hangs) | Approval prompts can't reach LINE users. Pre-approve trusted commands with `/approve always`. |
