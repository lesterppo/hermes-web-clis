---
name: mimo-web-cli
description: CLI for Xiaomi MiMo Studio via Firefox cookies. ~10-15s.
version: 1.0.0
tags: [mimo, xiaomi, web-cli, playwright]
platforms: [linux]
metadata:
  hermes:
    category: automation
---

# MiMo Web CLI

Playwright-based CLI for aistudio.xiaomimino.com. No Cloudflare = fast page loads.

## When to Use
- Multimodal tasks (MiMo-V2.5 has vision)
- Free alternative to paid vision models

## Quick Reference

```bash
python cli/mimo.py -o /tmp/out.md "prompt"
python cli/mimo.py -m mimo-v2.5 "multimodal task"
python cli/mimo.py --no-thinking "quick"
python cli/mimo.py --login  # refresh auth
```

## Pitfalls
- **Auth**: Xiaomi Account cookies from Firefox. Run `--login` to refresh.
- **Direct HTTP doesn't work**: `/open-apis/chat` returns 401. Playwright required.
- **Multi-turn**: Navigate to previous conversation URL (`/#/chat/<UUID>`).
- **Body-length detection**: Response completion detected by text stabilization, not DONE_JS.
