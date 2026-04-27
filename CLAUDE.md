# Hermes Agent Fork — CLAUDE.md

Fork of NousResearch/hermes-agent. Working branch: `feat/line-adapter`.

## Purpose

raise-a-bull v2: Hermes Agent + Outline wiki 透過 LINE Messaging API 協作。
Bot identity: Mojo（夢酒館助手），deployed at samantha-wsl as `bot-hermes-mojo`.

## Key Files

| File | Role |
|---|---|
| `gateway/platforms/line.py` | LINE adapter（727 行，單檔） |
| `toolsets.py` | `hermes-line` toolset definition |
| `gateway/config.py` | `Platform.LINE` enum + auto-enable |
| `gateway/run.py` | LINE factory wiring + allowlist maps |
| `hermes_cli/platforms.py` | `PLATFORMS` registry entry |
| `tests/gateway/test_line_*.py` | 66 tests across 12 files |

## Deployment

- samantha-wsl: `~/docker/bot-hermes-mojo/`
- Rebuild: `git pull && docker build -t hermes-fork:latest . && docker compose up -d --force-recreate`
- Tunnel: `hermes-mojo.pwlee.xyz` → cloudflared named tunnel → port 8645

## Upstream

- Issue: https://github.com/NousResearch/hermes-agent/issues/16611
- PR not yet opened — waiting for maintainer response

## Next

- Wire Outline MCP: add MCP server to `hermes-config.yaml` pointing to `https://pw-mba.tail5a1118.ts.net/mcp`
- Test Outline query via LINE

## Dev Notes

- Use `uv run pytest tests/gateway/test_line_*.py -q` to run LINE tests
- `log` → `logger` rename done (matches codebase convention)
- `_http_handler` returns 200 immediately, dispatches events via `asyncio.create_task`
  so LINE typing animation fires before LLM starts
