"""LINE Reply API client + Quick Reply button construction.

Quick Reply payload shape per LINE docs:
https://developers.line.biz/en/reference/messaging-api/#quick-reply
"""
from __future__ import annotations

import json
from typing import Any

import httpx

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_LOADING_URL = "https://api.line.me/v2/bot/chat/loading/start"

PENDING_REPLY_TEXT = "🤔 還在思考中，請稍候。如果太久沒回應，請重發訊息。"
EXPIRED_REPLY_TEXT = "答案已過期，請重新提問。"
ALREADY_DELIVERED_TEXT = "剛才已經回過了 ✅"


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
