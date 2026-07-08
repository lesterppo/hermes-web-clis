---
name: grok-web-cli
description: CLI for Grok (xAI) via Firefox cookies. ~2-5s.
version: 1.0.0
tags: [grok, xai, web-cli, playwright]
platforms: [linux]
metadata:
  hermes:
    category: automation
---

# Grok Web CLI

Playwright-based CLI for grok.com. Uses Firefox `sso` cookie. ~2s latency.

## When to Use
- Fastest response (~2s) among web CLIs
- Concise answers
- Reasoning tasks (via `-m think` flag)

## Quick Reference

```bash
python cli/grok.py -o /tmp/out.md "prompt"
python cli/grok.py -m think "complex reasoning"
python cli/grok.py --save-auth   # refresh cookies
```

## Pitfalls
- **Extraction**: User messages filtered by `items-end` parent alignment.
- **Free tier**: Only `fast` model works. `expert`/`heavy` require SuperGrok.
- **Cookie expiry**: Re-run `--save-auth` when auth fails.
