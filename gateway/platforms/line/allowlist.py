"""LINE source allowlist. Mirrors the env-var pattern used by Telegram/Discord.

Decision order (per spec):
  1. Allowlist check first — silent drop if not allowed
  2. Configuration check second — placeholder reply if no LLM yet
  3. Normal flow — process the message
"""
from __future__ import annotations

from typing import Any


def is_allowed(event: dict[str, Any], cfg: dict[str, list[str]]) -> bool:
    """Return True if the event's source is in the appropriate allowlist.

    cfg expected shape:
        {"users": ["U..."], "groups": ["C..."], "rooms": ["R..."]}
    """
    source = event.get("source") or {}
    src_type = source.get("type")
    if src_type == "user":
        return source.get("userId") in cfg.get("users", [])
    if src_type == "group":
        return source.get("groupId") in cfg.get("groups", [])
    if src_type == "room":
        return source.get("roomId") in cfg.get("rooms", [])
    return False
