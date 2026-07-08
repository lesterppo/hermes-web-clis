---
name: kimi-web-cli
description: CLI for Kimi via CDP browser. ~10-15s.
version: 1.0.0
tags: [kimi, web-cli, cdp, playwright]
platforms: [linux]
metadata:
  hermes:
    category: automation
---

# Kimi Web CLI

CDP-based CLI for kimi.com. Uses persistent Chrome profile.

## Prerequisites
- CDP server running: `python scripts/cdp_server.py start`
- Logged into kimi.com in the CDP Chrome profile

## Quick Reference

```bash
python cli/kimi.py -o /tmp/out.md "prompt"
python cli/kimi.py --save-all   # refresh accounts
```

## Pitfalls
- **Input selector**: Uses `textarea` on the loaded page. May need fallback to `[contenteditable]`.
- **Auth**: Kimi uses `kimi-auth` JWT cookie. `--save-all` extracts from Chrome.
- **Financial queries blocked**: Same as Qwen — fail-fast detection.
- **Response extraction**: Multiple fallback selectors (message, chat, assistant, response).
