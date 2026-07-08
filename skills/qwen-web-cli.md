---
name: qwen-web-cli
description: CLI for Qwen Chat via CDP browser. ~15-20s.
version: 1.0.0
tags: [qwen, web-cli, cdp, playwright]
platforms: [linux]
metadata:
  hermes:
    category: automation
---

# Qwen Web CLI

CDP-based CLI for chat.qwen.ai. Uses persistent Chrome profile.

## Prerequisites
- CDP server running: `python scripts/cdp_server.py start`
- Logged into chat.qwen.ai in the CDP Chrome profile

## Quick Reference

```bash
python cli/qwen.py -o /tmp/out.md "prompt"
python cli/qwen.py --no-thinking "quick question"
python cli/qwen.py -m qwen3-max "complex task"
```

## Pitfalls
- **Thinking mode**: Responses take 15-20s with thinking. Use `--no-thinking` for speed.
- **Auth**: Chrome profile must have valid Qwen session. Re-login via `cdp_server.py login`.
- **Model loading**: Page shows "Model loading..." after navigation. Wait loop handles this.
- **Financial queries blocked**: Qwen blocks stock/finance keywords. Fail-fast detection.
- **Cookie domains**: Token at `.qwen.ai`, UI cookies at `chat.qwen.ai`. Use cookies_by_domain format.
