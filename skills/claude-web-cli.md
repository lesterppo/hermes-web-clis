---
name: claude-web-cli
description: CLI for Claude.ai via browser cookies. No API key. ~5-8s.
version: 1.0.0
tags: [claude, web-cli, http-api, browser-cookie]
platforms: [linux]
metadata:
  hermes:
    category: automation
---

# Claude Web CLI

Token-efficient CLI for claude.ai using Firefox sessionKey cookie. No paid API key needed.

## When to Use
- Need Claude's coding/reasoning without API costs
- Fallback when primary API is rate-limited
- Cross-reference answers with other platforms

## Quick Reference

```bash
# Single prompt (token-efficient)
python cli/claude.py -o /tmp/out.md "prompt"

# Model selection
python cli/claude.py -m claude-sonnet-4-6 "prompt"
python cli/claude.py -m claude-haiku-4-5 "prompt"

# Multi-turn
python cli/claude.py -c chat.json "Turn 1"
python cli/claude.py -c chat.json "Turn 2"

# List available models
python cli/claude.py --list-models

# Re-extract auth cookies
python cli/claude.py --save-all
```

## Pitfalls
- **Auth**: Firefox `sessionKey` cookie. Re-run `--save-all` when expired.
- **Rate limiting**: ~5-8 rapid queries trigger 5-min cooldown. Multi-account support.
- **Model gating**: Free tier = Sonnet 4.6, Sonnet 4.5, Haiku 4.5. Opus/Fable = Pro only.
- **No attachment support**: CLI doesn't implement file uploads.
- **Complex prompts**: Architecture work takes 38-69s, not 5-8s. Budget 60s.
