---
name: kimi-web-cli
description: CLI for Kimi (kimi.com) via Playwright + native Chrome profile. No API key.
version: 2.0.0
tags: [kimi, web-cli, playwright, browser-cookie]
platforms: [linux]
metadata:
  hermes:
    category: automation
---

# Kimi Web CLI

Browser-cookie CLI for kimi.com (Moonshot AI). No API key — uses a reusable
native Chrome profile created once via `--login`. Drives the live UI to send
prompts, switch models (K3 · Max / K3 Swarm · Max / K2.6 · Fast), attach
files/images, and hold multi-turn conversations.

## Prerequisites

- Playwright + Chromium: `python -m playwright install chromium`
- One-time interactive login (RECOMMENDED):

```bash
python cli/kimi.py --login
#   → launches headed Chrome on DISPLAY=:0; sign in to kimi.com once.
#   → session saved to ~/.kimi-cli/chrome-profile (reused by all runs).
```

The legacy `--save-all` (extract Firefox `kimi-auth` JWT) still works for
read-only access but cannot reliably post — prefer `--login`.

## Quick Reference

```bash
# Latest model (K3 · Max) is the default
python cli/kimi.py -o /tmp/out.md "prompt"

# Pick a model
python cli/kimi.py -m kimi-k3-swarm "prompt"
python cli/kimi.py -m kimi-k2.6 "prompt"          # fast; --no-thinking recommended

# Disable thinking for speed
python cli/kimi.py --no-thinking "prompt"

# Attach files / images (repeatable)
python cli/kimi.py -f report.pdf -f diagram.png "Summarize both."
python cli/kimi.py -f photo.jpg "What is in this image?"

# Multi-turn (same state file carries context)
python cli/kimi.py -c chat.json "Turn 1"
python cli/kimi.py -c chat.json "Turn 2"
python cli/kimi.py -c chat.json --new "Fresh thread"
```

## Pitfalls

- **DISPLAY for `--login`**: headed Chrome needs a real X server. On WSL use
  `DISPLAY=:0` (Xvfb or a real X). Headless runs reuse the saved profile and
  need no display.
- **Page load**: kimi.com holds a persistent WebSocket open, so
  `wait_until='domcontentloaded'` never fires — use `wait_until='commit'` +
  a fixed settle wait.
- **Response extraction**: reply lives in `.layout-content`/`.main`, AFTER the
  last "Share" marker and BEFORE the composer placeholder ("Ask anything").
  `body.inner_text()` mixes in sidebar chat titles and must not be used.
- **"New Chat" click**: the sidebar item is outside the viewport under
  headless, so a JS `.click()` is used (Playwright `.click()` times out).
- **Image generation**: kimi.com's chat composer has NO image-gen button
  (toolkit: Add files & photos, Plugins, Skills, Goal). Image *upload + read*
  works; image *generation* is a separate Kimi app, not reachable here.
- **Cross-chat memory**: Kimi persists user facts (e.g. a name) across threads
  in a session, so even `--new` may recall earlier facts — that's a Kimi
  feature, not a CLI bug.
- **EPIPE**: on Node.js v24, suppress stderr on Playwright cleanup to avoid a
  spurious crash (handled internally by `_safe_close`).
