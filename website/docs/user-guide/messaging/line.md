---
sidebar_position: 2
title: "LINE"
description: "Set up Hermes Agent as a LINE Messaging API bot"
---

# LINE Setup

Hermes Agent integrates with [LINE](https://line.me/) as a full-featured Messaging API bot. Once connected, you can chat with your agent on iOS, Android, or desktop LINE clients, use it in groups and rooms, and receive scheduled task results. The integration uses LINE's official [Messaging API](https://developers.line.biz/en/services/messaging-api/) — webhook delivery, Reply API for free responses, and Push API for outbound messages.

LINE has two API quirks the adapter handles transparently:

- **Reply tokens are single-use and short-lived.** If the LLM takes longer than ~45 seconds, the reply token may expire before the answer is ready. The adapter automatically falls back to a Quick Reply postback button — the user taps it to fetch the queued response, avoiding the cost of a Push API message.
- **Group/room source types are distinct from user source.** LINE separates `user` (DM), `group` (multi-user chat with full IDs), and `room` (multi-user chat without IDs). The adapter uses three independent allowlists so you can grant access at any level.

## Step 1: Create a LINE Official Account

LINE bots run on top of an Official Account (formerly "LINE@"). Create one at [LINE Official Account Manager](https://manager.line.biz/):

1. Sign in with your personal LINE account
2. Click **Create** → fill in the account name, category, and region
3. Accept the terms — your Official Account is provisioned immediately

## Step 2: Provision a Messaging API Channel

Open the [LINE Developers Console](https://developers.line.biz/console/) and do the following:

1. Select (or create) a **Provider** — this is the legal entity owning the bot
2. Inside the Provider, click **Create a Messaging API channel**
3. Link the channel to the Official Account from Step 1
4. Fill in the channel name, description, and icon

Once the channel exists, you'll find two tabs you need:

| Tab | What you need |
|-----|--------------|
| **Basic settings** | **Channel secret** (used to verify webhook signatures) and **Your user ID** (starts with `U`) |
| **Messaging API** | **Channel access token** — click **Issue** to generate a long-lived token |

:::warning
Treat the **Channel access token** like a password. Anyone with it can send messages as your bot. If it leaks, click **Reissue** in the same tab — old tokens are revoked immediately.
:::

## Step 3: Disable LINE's Default Auto-Replies

By default, every Messaging API channel ships with three auto-reply behaviors that interfere with bot operation. Disable all three:

1. In the **Messaging API** tab of your channel, click **Edit** next to **LINE Official Account features**
2. In the response settings page that opens, set:
   - **Greeting message** → Disabled
   - **Auto-reply** → Disabled
   - **Webhook** → Enabled

If you skip this, your bot will reply twice to every message — once from the auto-responder, once from Hermes.

## Step 4: Find Your User ID

Hermes uses LINE user IDs (which start with `U`) to control who can chat with the bot.

1. Open the [LINE Developers Console](https://developers.line.biz/console/) → your channel → **Basic settings** tab
2. Scroll down to find **Your user ID** — copy the value (it looks like `U1234567890abcdef1234567890abcdef`)

This is the user ID of the LINE account you signed in with. Save it for the next step.

:::tip
To find another user's ID without console access, add the bot to a chat with them, send any message, and check the gateway logs for a `line.drop` entry containing the unrecognized user ID.
:::

## Step 5: Configure Hermes

### Option A: Interactive Setup (Recommended)

```bash
hermes gateway setup
```

Select **LINE** when prompted. The wizard asks for your channel access token, channel secret, and allowed user IDs, then writes everything to `~/.hermes/.env`.

### Option B: Manual Configuration

Add the following to `~/.hermes/.env`:

```bash
LINE_CHANNEL_ACCESS_TOKEN=YOUR_LONG_LIVED_TOKEN
LINE_CHANNEL_SECRET=YOUR_CHANNEL_SECRET
LINE_ALLOWED_USERS=U1234567890abcdef1234567890abcdef    # comma-separated for multiple users
```

### Start the Gateway

```bash
hermes gateway
```

The gateway starts an HTTP listener on `LINE_WEBHOOK_PORT` (default `8645`). The next step is exposing it to LINE.

## Step 6: Set the Webhook URL

LINE pushes incoming events to a public HTTPS URL. The adapter exposes them at `/line/webhook`.

### Local development with a tunnel

For local testing, expose port 8645 with a tunneling service such as [ngrok](https://ngrok.com/) or [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```bash
ngrok http 8645
# copy the https://....ngrok-free.app URL
```

### Cloud deployment

Use your platform's public HTTPS URL (Fly.io, Railway, Render, etc.). Make sure the inbound HTTPS port routes traffic to `LINE_WEBHOOK_PORT` inside the container.

### Register the URL with LINE

1. In the LINE Developers Console → your channel → **Messaging API** tab
2. Set **Webhook URL** to `https://YOUR-PUBLIC-HOST/line/webhook`
3. Click **Verify** — LINE sends a probe request and reports success/failure
4. Toggle **Use webhook** ON

If verification fails, check that:

- The gateway is running and reachable from the public internet
- The URL ends with `/line/webhook` (the adapter does not respond at `/`)
- TLS is valid — LINE rejects self-signed certificates

You can health-check the webhook endpoint at `GET /line/webhook/health` — it returns `{"status": "ok", "platform": "line"}`.

## Group and Room Chats

LINE has two kinds of multi-user chats, with different ID semantics:

| Source type | Prefix | What it represents | Allowlist env var |
|-------------|--------|---------------------|--------------------|
| `user` | `U` | DM with a single user | `LINE_ALLOWED_USERS` |
| `group` | `C` | Group chat with stable IDs | `LINE_ALLOWED_GROUPS` |
| `room` | `R` | Multi-user chat without persistent group ID | `LINE_ALLOWED_ROOMS` |

All three allowlists default-deny — an empty list means no access at that source type. To allow the bot to respond in a group, you must add its `C`-prefixed ID to `LINE_ALLOWED_GROUPS`.

### How to discover a group/room ID

Group and room IDs aren't exposed in the LINE app. Discover them this way:

1. Add the bot to the group/room
2. Send any message
3. Check `hermes gateway` logs for a `line.drop unauthorised` entry — the dropped event includes the source IDs broken out by type

```
line.drop unauthorised src_type=group user=U1234... group=Cabcdef0123456789... room=None
```

For a group, copy the value of `group=`. For a room, copy `room=`. Add it to the appropriate allowlist:

```bash
LINE_ALLOWED_GROUPS=Cabcdef0123456789...
```

Restart the gateway to pick up the new value.

## Mention Gate (Quiet Mode in Groups)

By default, in any allowed group/room, the bot responds to every message. To make the bot only respond when explicitly addressed, enable the mention gate:

```bash
LINE_REQUIRE_MENTION=true
```

With this set, group/room messages are accepted only if they:

- Mention the bot by display name (e.g., `@小茉 hello`)
- The bot's display name is auto-fetched from LINE's `/v2/bot/info` endpoint at startup. You can override it manually with `LINE_BOT_DISPLAY_NAME=YourBotName`.

:::warning Fail-closed behavior
If `LINE_REQUIRE_MENTION=true` is set but the bot's display name is empty (auto-fetch failed and no manual override), the gate **drops every group message** and emits an operator warning in the logs. This is intentional fail-closed behavior — the gate must have something to match against, or it can't enforce the rule. Set `LINE_BOT_DISPLAY_NAME` explicitly if your bot doesn't have a discoverable display name.
:::

DM messages always bypass the mention gate.

### Free-Response Groups and Rooms

You may want certain groups/rooms to bypass the mention gate even when it's enabled globally — for example, a private team channel where you want every message answered:

```bash
LINE_FREE_RESPONSE_GROUPS=Cprivateteam0123...
LINE_FREE_RESPONSE_ROOMS=Rresearchroom0123...
```

Each ID listed here must also appear in the corresponding allowlist (`LINE_ALLOWED_GROUPS` / `LINE_ALLOWED_ROOMS`). The gateway logs a warning at startup if a free-response ID is missing from the allowlist — otherwise the message would be dropped before the free-response check runs.

## Slow LLM Quick Reply Fallback

LINE's reply token expires after approximately 60 seconds (LINE's documented limit) and is single-use. If the LLM takes longer than the threshold (default 45 seconds, leaving a 15-second safety margin), the reply token may already be invalid by the time the answer is ready.

Rather than burning a Push API call (which is metered), the adapter sends a **Quick Reply postback button** at the threshold:

1. User sends a message — the adapter starts the LLM and shows a loading indicator
2. At ~45 seconds, the adapter posts a Quick Reply button: "📋 Show response"
3. When the LLM finishes, the answer is cached
4. The user taps the button — LINE delivers a postback event with a fresh reply token, and the adapter sends the cached answer using the new token

Tune the threshold and cache TTL:

```bash
LINE_SLOW_RESPONSE_THRESHOLD=45     # seconds before showing the button (default: 45)
LINE_CACHE_TTL=3600                 # how long to keep the cached answer (default: 1 hour)
```

You can also customize the button labels and notice texts:

```bash
LINE_BUTTON_LABEL="📋 Show response"
LINE_PENDING_TEXT="Working on it — tap when you're ready to see the answer"
LINE_DELIVERED_TEXT="✓ Response already delivered earlier in this thread"
LINE_EXPIRED_TEXT="The cached response has expired. Please ask again."
```

## Home Channel

The home channel is where Hermes delivers cron job results, cross-platform notifications, and `send_message` calls that don't specify a chat. Configure it in `~/.hermes/.env`:

```bash
LINE_HOME_CHANNEL=U1234567890abcdef...
LINE_HOME_CHANNEL_NAME="My Notes"      # optional friendly label
```

For DMs, this is your user ID. For a group/room home channel, use the `C`/`R` ID — and make sure it's also in the corresponding allowlist.

You can also set this from inside the chat by typing `/sethome` in any allowed chat with the bot.

## Sending Files

LINE's Messaging API only accepts media via **public HTTPS URLs** — it cannot upload local file bytes. This is a LINE platform constraint, not a Hermes limitation.

The adapter handles this gracefully:

- **Images via HTTPS URL** → sent successfully
- **Local file path** → the adapter logs a warning and skips the attachment, but text content still delivers

If you need to send images generated locally, you have a few options:

- Host the file on a public URL (S3, R2, GitHub Pages, etc.) and pass the URL
- Set `LINE_PUBLIC_URL` and proxy media through your own webhook host (planned future enhancement — track via the LINE adapter PR for status)
- Forward through a service that uploads to a CDN, then pass the resulting URL

This constraint also applies to documents, video, and voice attachments — none can be uploaded directly via the Messaging API.

## Token Length Limits

Each LINE message is capped at **5000 characters** (LINE's documented limit). The adapter chunks longer responses by Python string length into up to 5 message objects (5000 chars each — LINE caps a single Push or Reply call to 5 messages).

Responses longer than **25,000 characters** are truncated. The final chunk is appended with `… (truncated)` so you know the answer was cut off. If a response includes one image and many text segments, only the first 4 text segments after the image are sent (the rest are silently dropped).

## Inbound Non-Text Messages

The adapter only responds to **text messages**. Inbound stickers, images, voice notes, location shares, and file uploads are silently ignored — you get no reply and no error. This is by design (LINE's inbound media model is more complex than a simple text channel and the agent is text-only). If you send a sticker and get no response, that's why.

## Known Limitations

### No Push API delivery for tool approvals or background events

LINE's Push API is metered (charged per message after a free monthly quota), so the adapter intentionally **suppresses** `self.send()` calls from the base session pipeline. This avoids unexpected per-message costs from chatty agent behaviors like compaction notices and tool approval prompts.

The downside: any tool call that requires interactive approval (the dangerous-command gate) **cannot reach the user via LINE** — the session hangs waiting for a reply that will never arrive.

:::warning
If you use the agent with terminal access via LINE, pre-approve trusted commands with `/approve always` in your LINE conversation before triggering anything that could hit the dangerous-command gate. Without this, the session hangs on the first gated tool call.
:::

### Typing indicator is DM-only

LINE's loading indicator API only supports `user` source types (DMs). For groups and rooms, the adapter skips the indicator silently — you'll see a small delay before the response with no "typing" feedback.

### In-memory cache (no persistence across restarts)

The slow-LLM Quick Reply cache lives in memory only — there's no Redis backend. If the gateway restarts while a slow LLM is computing and a Quick Reply button is already in the user's chat, tapping the button will show `LINE_EXPIRED_TEXT` because the cache entry was lost. PENDING entries are kept for up to 24 hours (non-configurable) before the prune sweep removes them; READY/DELIVERED/ERROR entries follow `LINE_CACHE_TTL`.

### Mention-stripping in groups

When the mention gate accepts a group message, the bot's display-name prefix (e.g., `@小茉 `) is **stripped from the text** before passing it to the LLM. So a message like `@小茉 hello` reaches the agent as just `hello` — useful for cleanliness, but means the agent doesn't see its own name in the prompt context. A message containing **only** the mention with no body text after stripping is silently dropped.

### Cron deliveries with local-file media (`MEDIA:`) silently fail

Cron jobs that emit only `MEDIA:/local/path/img.png` payloads (no text) cannot be delivered to LINE because LINE has no native file upload — the adapter routes through `send_image_file` which returns failure, and the cron framework currently logs but doesn't retry. **Workaround:** in cron job prompts, host the file on a public HTTPS URL and emit `![alt](https://...)` markdown instead — the LINE adapter's reply builder extracts that as a native image bubble.

### `hermes-line` toolset includes approval-gated tools

The toolset inherits `_HERMES_CORE_TOOLS` (terminal, execute_code, write_file, browser tools, etc.) which can trigger the dangerous-command approval gate. Combined with the suppressed `self.send()` (above), an unguarded session can hang on the first gated tool call. The mitigation is the same `:::warning` admonition above: pre-approve trusted commands with `/approve always` once per LINE conversation, or run a tool-restricted toolset by setting `default_toolset` for the LINE platform in `~/.hermes/config.yaml` to a safer subset (e.g. `hermes-web` for web-only research bots).

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Webhook verification fails | Ensure the URL is HTTPS, publicly reachable, ends with `/line/webhook`, and TLS is valid (no self-signed certs). |
| Bot responds twice to every message | LINE's auto-responder is enabled. Disable greeting, auto-reply, and group auto-reply in the **LINE Official Account features** settings (Step 3). |
| Bot responds to nothing | Check that `LINE_ALLOWED_USERS` contains your user ID (the one from console **Basic settings** → **Your user ID**, not your LINE display name). |
| Group messages dropped | Add the group's `C`-prefixed ID to `LINE_ALLOWED_GROUPS`. Discover it via the `line.drop` log entry after sending a message in the group. |
| Bot ignores group messages even when allowed | If `LINE_REQUIRE_MENTION=true`, the message must mention the bot by display name. Either drop the requirement, set `LINE_BOT_DISPLAY_NAME` explicitly, or add the group to `LINE_FREE_RESPONSE_GROUPS`. |
| `Failed to fetch LINE bot info` warning | The auto-name-fetch failed (usually a token problem). The bot still works for DMs. Set `LINE_BOT_DISPLAY_NAME` manually if you need group mention gating. |
| Quick Reply button doesn't appear | Check that the LLM is actually slow enough to trigger it (default threshold is 45s). Lower `LINE_SLOW_RESPONSE_THRESHOLD` to test. |
| `401 Unauthorized` returned to LINE (gateway logs) | `LINE_CHANNEL_SECRET` is wrong — the gateway returns 401 from `/line/webhook` when HMAC verification fails. Copy the secret again from **Basic settings** (it's distinct from the access token). |
| Reply/Push API failures in logs | Channel access token is invalid or revoked. Reissue it from the **Messaging API** tab and update `LINE_CHANNEL_ACCESS_TOKEN`. |

## Security

:::warning
All three LINE allowlists default-deny. An empty `LINE_ALLOWED_USERS` means no users can chat with the bot via DM. Always set this explicitly.
:::

The `LINE_ALLOW_ALL_USERS=true` override exists for low-trust testing only — never enable it on a production bot, especially one with terminal access.

Webhook signatures are verified using HMAC-SHA256 with constant-time comparison. Requests with missing or invalid signatures return `401` and are not dispatched to the agent.

For more details, see the [Security documentation](/user-guide/security). You can also use [DM pairing](/user-guide/messaging#dm-pairing-alternative-to-allowlists) for a more dynamic approach to user authorization.

## Reference: Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LINE_CHANNEL_ACCESS_TOKEN` | Yes | — | Long-lived channel access token |
| `LINE_CHANNEL_SECRET` | Yes | — | Channel secret for HMAC signature verification |
| `LINE_ALLOWED_USERS` | Yes | — | Comma-separated list of `U`-prefixed user IDs; default-deny — an empty list means no DM access |
| `LINE_ALLOWED_GROUPS` | No | — | Comma-separated list of `C`-prefixed group IDs |
| `LINE_ALLOWED_ROOMS` | No | — | Comma-separated list of `R`-prefixed room IDs |
| `LINE_ALLOW_ALL_USERS` | No | `false` | If `true`, bypass all source allowlists (users, groups, AND rooms) — NOT recommended for production |
| `LINE_HOME_CHANNEL` | No | — | Where cron results and notifications are delivered |
| `LINE_HOME_CHANNEL_NAME` | No | `Home` | Friendly label for the home channel |
| `LINE_WEBHOOK_PORT` | No | `8645` | Local port for the webhook listener |
| `LINE_REQUIRE_MENTION` | No | `false` | Require @mention in groups/rooms |
| `LINE_BOT_DISPLAY_NAME` | No | auto-fetched | Manual override of bot's display name for the mention gate |
| `LINE_FREE_RESPONSE_GROUPS` | No | — | Groups that bypass the mention gate (must also be in `LINE_ALLOWED_GROUPS`) |
| `LINE_FREE_RESPONSE_ROOMS` | No | — | Rooms that bypass the mention gate (must also be in `LINE_ALLOWED_ROOMS`) |
| `LINE_SLOW_RESPONSE_THRESHOLD` | No | `45` | Seconds before showing the Quick Reply button |
| `LINE_CACHE_TTL` | No | `3600` | Seconds to cache the response for postback retrieval |
| `LINE_PENDING_TEXT` | No | (built-in) | Text shown when the slow-response button appears |
| `LINE_DELIVERED_TEXT` | No | (built-in) | Reply when a user taps the button after delivery |
| `LINE_EXPIRED_TEXT` | No | (built-in) | Reply when the cache TTL has elapsed |
| `LINE_BUTTON_LABEL` | No | `📋 Show response` | Label on the Quick Reply postback button |
