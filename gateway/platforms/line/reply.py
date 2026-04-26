"""LINE Reply API client + Quick Reply button construction.

Quick Reply payload shape per LINE docs:
https://developers.line.biz/en/reference/messaging-api/#quick-reply
"""
from __future__ import annotations

import json
from typing import Any

import httpx

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

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
