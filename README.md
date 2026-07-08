# Hermes Web CLIs

Bundle of 7 AI-agent-native CLIs for web AI platforms — no API keys needed.
Each CLI authenticates via browser cookies and returns token-efficient JSON pointer output.

## Quick Start

```bash
# Install
pip install playwright && playwright install chromium
git clone https://github.com/lesterppo/hermes-web-clis
cd hermes-web-clis
pip install -r requirements.txt

# Login (one-time, opens visible browser)
python scripts/cdp_server.py login

# Start CDP daemon (headed — required for ChatGPT & Minimax)
python scripts/cdp_server.py start --headed

# Use any CLI
python cli/claude.py "Explain quantum computing"
python cli/grok.py "Write a haiku about coding"
python cli/qwen.py "Debug this Python error: ..."
```

## Architecture

Two approaches, chosen per platform:

| Approach | Platforms | Latency | Auth |
|----------|-----------|---------|------|
| **HTTP API** | Claude | ~5s | Firefox `sessionKey` cookie |
| **CDP (Chrome DevTools Protocol)** | Qwen, Kimi, Minimax, ChatGPT | 3-20s | Persistent Chrome profile |
| **Playwright standalone** | Grok, MiMo | 2-15s | Firefox cookies injected into Chromium |

### Tier 1: HTTP API (Claude)
Claude exposes an internal API at `claude.ai/api/organizations/{org}/chat_conversations/{id}/completion`.
The CLI extracts the `sessionKey` cookie from Firefox and calls this endpoint directly.

### Tier 2: CDP Browser (Qwen, Kimi, Minimax, ChatGPT)
These platforms use aggressive anti-bot protection (Cloudflare, Akamai, Alibaba WAF).
A long-lived headed Chromium instance runs as a daemon with `--remote-debugging-port=9223`.
CLIs connect via Playwright's `connect_over_cdp()`.

**Why headed?** Cloudflare Turnstile detects headless browsers. Some platforms (Minimax)
render responses in virtualized DOM containers invisible in headless mode.

### Tier 3: Playwright Standalone (Grok, MiMo)
These platforms work with Firefox cookies injected into a fresh headless Chromium per query.
No persistent browser needed.

## Platforms

| # | CLI | Platform | Auth Source | Approach | Latency |
|---|-----|----------|-------------|----------|---------|
| 1 | `claude.py` | claude.ai | Firefox sessionKey | HTTP API | ~5s |
| 2 | `grok.py` | grok.com | Firefox sso cookie | Playwright standalone | ~2s |
| 3 | `mimo.py` | aistudio.xiaomimimo.com | Firefox cookies | Playwright standalone | ~12s |
| 4 | `qwen.py` | chat.qwen.ai | Chrome CDP profile | CDP | ~18s |
| 5 | `kimi.py` | kimi.com | Chrome CDP profile | CDP | ~10s |
| 6 | `minimax.py` | agent.minimax.io | Chrome CDP profile | CDP (headed) | ~4s |
| 7 | `chatgpt.py` | chatgpt.com | Chrome CDP profile | CDP (headed) | ~3s |

## CDP Server

The CDP server is the backbone for platforms requiring persistent authentication:

```bash
python scripts/cdp_server.py start --headed   # launch daemon (headed)
python scripts/cdp_server.py start             # launch daemon (headless)
python scripts/cdp_server.py status            # check health
python scripts/cdp_server.py stop              # shut down
python scripts/cdp_server.py login             # one-time interactive login
```

The daemon double-forks to survive terminal disconnection and survives Node.js v24 EPIPE crashes.

## CLI Usage

All CLIs share a common interface:

```bash
# Basic prompt
python cli/<name>.py "Your prompt"

# Token-efficient JSON pointer output (for AI agents)
python cli/<name>.py -o /tmp/out.md "Your prompt"
# → {"ok":true,"f":"/tmp/out.md","s":450,"b":0}

# Multi-turn conversation
python cli/<name>.py -c chat.json "Turn 1"
python cli/<name>.py -c chat.json "Turn 2"

# Model selection (platform-dependent)
python cli/claude.py -m claude-sonnet-4-6 "prompt"
python cli/grok.py -m think "reasoning task"
python cli/qwen.py -m qwen3-max "complex task"

# Disable thinking (faster, where supported)
python cli/qwen.py --no-thinking "quick question"
python cli/minimax.py --no-thinking "quick question"
```

## Token-Efficient Output

All CLIs support `-o FILE` for agent-optimized output:

```json
{"ok":true,"f":"/tmp/out.md","s":450,"b":2,"m":"fast","t":2.3}
```

Fields: `f`=file path, `s`=bytes, `b`=code blocks, `m`=model, `t`=elapsed seconds.
The calling agent reads the output file with a standard file-read tool.
This saves 60-90% tokens vs returning raw response text.

## Auth Setup

### Step 1: Log into platforms in your browser
Open each platform in Firefox/Chrome and sign in:
- https://claude.ai (Firefox)
- https://grok.com (Firefox)
- https://aistudio.xiaomimino.com (Firefox)
- https://chat.qwen.ai (Chrome)
- https://kimi.com (Chrome)
- https://agent.minimax.io (Chrome)
- https://chatgpt.com (Chrome)

### Step 2: Extract cookies / create CDP profile

**For HTTP API & Playwright standalone (Claude, Grok, MiMo):**
```bash
python cli/claude.py --save-all    # extract Firefox sessionKey
python cli/grok.py --save-auth     # extract Firefox sso cookie
python cli/mimo.py --login         # extract Firefox cookies
```

**For CDP platforms (Qwen, Kimi, Minimax, ChatGPT):**
```bash
python scripts/cdp_server.py login  # opens visible Chrome, log in, close when done
```

### Step 3: Start CDP daemon (for CDP platforms)
```bash
python scripts/cdp_server.py start --headed
```

## Skills

Each CLI has a companion skill file in `skills/` for Hermes Agent integration.
Skills provide:
- Trigger conditions (when to use this CLI)
- Quick reference commands
- Pitfalls and platform quirks
- DOM selectors and extraction patterns
- Auth refresh procedures

For other AI agents: load the relevant skill file before using the CLI.

## Requirements

- Python 3.10+
- Playwright (`pip install playwright && playwright install chromium`)
- Firefox (for cookie extraction on Claude, Grok, MiMo)
- Chrome/Chromium (for CDP platforms)
- WSL or Linux (Firefox cookie extraction uses SQLite)

## Pitfalls

- **ChatGPT + Minimax require headed CDP.** Headless mode triggers Cloudflare or DOM virtualization issues.
- **Cookie expiry.** Auth cookies expire in hours to days. Re-run `--save-auth` or `cdp_server.py login` when queries fail.
- **Node.js v24 EPIPE.** The CDP server daemonizes to avoid this. Standalone CLIs suppress stderr during cleanup.
- **Rate limits.** All platforms enforce rate limits. The CLIs include cooldown handling.
- **DOM fragility.** CSS selectors may change with platform updates. Check `skills/` for current selectors.

## Contributing

When a platform changes its DOM, update the extraction selectors in both:
1. The CLI script in `cli/`
2. The skill file in `skills/`

Test with a simple prompt before submitting.

## License

MIT
