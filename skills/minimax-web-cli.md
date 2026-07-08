---
name: minimax-web-cli
description: CLI for MiniMax Agent via CDP headed browser. ~4-8s.
version: 1.0.0
tags: [minimax, web-cli, cdp, playwright, headed]
platforms: [linux]
metadata:
  hermes:
    category: automation
---

# MiniMax Web CLI

CDP-based CLI for agent.minimax.io. **Requires headed Chromium** — headless mode hides responses.

## Prerequisites
- CDP server running HEADED: `python scripts/cdp_server.py start --headed`
- Logged into agent.minimax.io in the CDP Chrome profile

## Quick Reference

```bash
python cli/minimax.py -o /tmp/out.md "prompt"
python cli/minimax.py --no-thinking "quick"
python cli/minimax.py --login   # refresh auth
```

## Pitfalls
- **Headed REQUIRED**: Responses render via virtualized DOM invisible in headless mode.
- **Editor**: ProseMirror/TipTap editor. Use `page.keyboard.type()`, not `fill()`.
- **Send**: ProseMirror editor — Enter key submits. May need `[class*="enter"]` button fallback.
- **Response**: Use DOM queries (`[class*=message],[class*=bubble]`) not `body.innerText`.
- **Modals**: Dismiss "Close", "Try it now", "Download desktop" before typing.
