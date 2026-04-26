"""LINE webhook helpers: HMAC validation + event parsing.

Spec: https://developers.line.biz/en/reference/messaging-api/#signature-validation
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """Constant-time compare LINE's X-Line-Signature header against an HMAC-SHA256
    of the raw body using the channel secret.
    """
    if not signature:
        return False
    expected = base64.b64encode(
        hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def parse_events(body: bytes) -> list[dict[str, Any]]:
    """Parse a LINE webhook body into the raw events list. Returns [] on no events.
    Does NOT validate types — leaves that to the dispatcher.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    return payload.get("events", []) or []
