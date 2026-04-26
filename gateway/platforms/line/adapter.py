"""LINE Messaging API adapter for Hermes Agent gateway.

Pattern borrowed from gateway/platforms/webhook.py for the request cache,
and gateway/platforms/telegram.py for env var / allowlist handling.

This is a skeleton (Task 1 of the LINE adapter plan). Subsequent tasks add
webhook handling, allowlist, cache, dispatch, postback handling, and the
HTTP server.
"""
from __future__ import annotations

from gateway.platforms.base import BasePlatformAdapter


class LineAdapter(BasePlatformAdapter):
    """LINE adapter. See docs/messaging/line.md for setup.

    NOTE: __init__ is intentionally not overridden in Task 1.
    BasePlatformAdapter.__init__ requires (config, platform), and
    Platform.LINE will be added to the Platform enum in a later task
    along with the run.py registry wiring. For now we only need the
    class to be importable.
    """

    name = "line"

    async def connect(self) -> None:
        # Wired in Task 3
        pass

    async def disconnect(self) -> None:
        pass
