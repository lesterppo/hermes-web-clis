# AGENTS.md — AI Agent Instructions

You are an AI agent with access to this repository of 8 AI web CLIs.
Use these CLIs to query web AI platforms without API keys.

## When to Use

Use a CLI from this repo when:
- The user wants a free alternative to paid API access
- You need a specific model (Claude, Grok, Qwen, Kimi, MiniMax, MiMo, ChatGPT)
- You're rate-limited on your primary API and need a fallback
- You want to cross-reference answers across multiple platforms

## Quick Reference

```bash
CLI=cli/<name>.py
PY=python3  # or path to venv python

# Agent-optimized (PREFERRED — saves tokens)
$PY $CLI -o /tmp/out.md "prompt"
# → {"ok":true,"f":"/tmp/out.md","s":N,"b":N}
# Then: read_file /tmp/out.md

# Direct text output (more tokens)
$PY $CLI "prompt"
```

## Platform Selection

| Platform | CLI | Requires CDP? | Best for |
|----------|-----|---------------|----------|
| claude.ai | `cli/claude.py` | No | Fast, reliable, coding |
| grok.com | `cli/grok.py` | No | Fastest (~2s), concise |
| chat.qwen.ai | `cli/qwen.py` | Yes | Chinese, reasoning |
| kimi.com | `cli/kimi.py` | Yes | Chinese, long-form |
| agent.minimax.io | `cli/minimax.py` | Yes (headed) | Multi-agent, thinking |
| aistudio.xiaomimimo.com | `cli/mimo.py` | No | Multimodal |
| chatgpt.com | `cli/chatgpt.py` | Yes (headed) | General purpose |
| gemini.google.com | `cli/gem-pw.py` | No | Gem CRUD, knowledge mgmt, extended thinking |

## Prerequisites Check

Before using a CLI, verify:
1. CDP server running? `python scripts/cdp_server.py status` (for CDP platforms)
2. Auth valid? Each CLI has `--accounts` or auth check
3. Playwright installed? `python -c "from playwright.sync_api import sync_playwright"`

## CDP Server Lifecycle

```
# Start (one-time per session)
python scripts/cdp_server.py start --headed

# Verify
python scripts/cdp_server.py status
# → "Running (PID N) / Browser: Chrome/148.0..."

# Use CLIs normally...

# Stop when done
python scripts/cdp_server.py stop
```

## Error Recovery

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `[QWEN:LOADING]` hangs | Auth expired | Re-login in Chrome, restart CDP |
| `no-input` / `no-auth` | Cookies stale | `cli/<name>.py --save-auth` or `cdp_server.py login` |
| Cloudflare "Just a moment" | Headless detected | Use `--headed` flag on CDP server |
| EPIPE crash | Node.js v24 | CDP server handles this; for standalone, suppress stderr on cleanup |
| Empty response | Wrong DOM selectors | Check `skills/<name>.md` for current selectors |
| `EMPTY` after 200 polls | Extended thinking slow | Use `-t 600` for large knowledge bases |
| Profile lock / `SingletonLock` | Stale Chrome process | Kill Chrome, gem-pw auto-cleans locks |

## Integration with Hermes Agent

Place skill files from `skills/` into `~/.hermes/skills/automation/`.
Each skill provides trigger keywords, quick reference, and platform-specific pitfalls.

## Output Format

All CLIs return JSON on stdout when using `-o FILE`:
```json
{"ok": true, "f": "/tmp/out.md", "s": 450, "b": 2}
```
On error:
```json
{"ok": false, "err": "no-auth", "msg": "Auth expired"}
```

Always check `ok` before reading the output file.
