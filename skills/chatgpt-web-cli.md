---
name: chatgpt-web-cli
description: CLI for ChatGPT via CDP headed browser. ~3-5s.
version: 1.0.0
tags: [chatgpt, web-cli, cdp, playwright, headed]
platforms: [linux]
metadata:
  hermes:
    category: automation
---

# ChatGPT Web CLI

CDP-based CLI for chatgpt.com. **Requires headed Chromium** to bypass Cloudflare Turnstile.

## Prerequisites
- CDP server running HEADED: `python scripts/cdp_server.py start --headed`
- Logged into chatgpt.com in the CDP Chrome profile

## Quick Reference

```bash
python cli/chatgpt.py -o /tmp/out.md "prompt"
python cli/chatgpt.py --login   # one-time Cloudflare clearance
```

## Pitfalls
- **Headed REQUIRED**: Cloudflare Turnstile blocks headless browsers even with valid auth.
- **Editor**: Uses ProseMirror (`#prompt-textarea`), NOT `<textarea>`. Selector: `.ProseMirror`.
- **Click**: May need `force=True` on the input element.
- **Response**: Check `[data-message-author-role="assistant"]` for assistant messages.
- **Rate limiting**: Aggressive. The free tier has strict limits.
