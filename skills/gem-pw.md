---
name: gem-pw
description: Self-contained Gemini Gem CLI via Playwright Chromium.
version: 4.0.0
tags: [gemini, gem, web-cli, playwright, cdp, browser-automation]
platforms: [linux, wsl]
metadata:
  hermes:
    category: automation
---

# Gemini Gem CLI (gem-pw)

Self-contained CLI for interacting with Gemini Gems via Playwright + Chromium.
Launches its own Chromium per call — no external server dependency.
Bypasses gemini-webapi's broken RPC auth by driving the real web UI.

## When to Use

- gemini-webapi returns UNAUTHENTICATED (status 1016) for Gem operations
- Need file upload, image generation, create/edit/delete via Gem web UI
- Need multi-turn conversation with URL-based persistence
- Need Pro + Extended Thinking with configurable timeout

## Quick Reference

```bash
# Chat
python cli/gem-pw.py <gem-id> "prompt"
python cli/gem-pw.py <gem-id> -m pro --thinking extended -t 600 "deep analysis"
python cli/gem-pw.py <gem-id> -c session.json "multi-turn"

# Create Gem
python cli/gem-pw.py --create "Name" "Instructions" -m pro --thinking extended
python cli/gem-pw.py --create "Name" "Instr" \
  --knowledge-file paper.pdf \
  --knowledge-code https://github.com/user/repo \
  --knowledge-folder /path/to/project

# Edit Gem
python cli/gem-pw.py --edit <id> --name "New Name"
python cli/gem-pw.py --edit <id> --instructions "New prompt..."
python cli/gem-pw.py --edit <id> -m pro --thinking extended
python cli/gem-pw.py --edit <id> --knowledge-code https://github.com/user/repo
python cli/gem-pw.py --edit <id> --knowledge-file document.pdf

# Delete / Upload / Image
python cli/gem-pw.py --delete <id>
python cli/gem-pw.py --upload <id> -f file.txt "summarize"
python cli/gem-pw.py --img <id> "a cat on a rainbow"

# Token-efficient output
python cli/gem-pw.py <id> -o /tmp/out.md --json-out "prompt"
```

## Architecture

Self-contained Playwright Chromium: launches per call with persistent profile
at `~/.gemini-cli/cr-profile/`. No external server needed. ~5s launch overhead
but immune to memory-pressure kills.

Uses `launch_persistent_context` with stealth flags:
`--disable-blink-features=AutomationControlled`,
`ignore_default_args=['--enable-automation']`.

## Output Format

Agent-optimized JSON pointer (with `--json-out`):
```json
{"ok": true, "f": "/tmp/gem-pw-1783463794.md", "s": 1234, "t": 12.3}
```

## Platform-Specific Selectors

Gemini UI uses Traditional Chinese locale. Key selectors:
- Input: `div[role="textbox"][aria-label*="輸入提示"]`
- Response: `.response-content, message-content, model-response`
- Knowledge menu: `button[aria-label*="知識部分"][aria-label*="上載"]`
- Model selector: `button[aria-label*="模式選擇器"]`
- Save button: `button:has-text("儲存")`

## Pitfalls

- **Auth**: One-time `gem-pw-login` signs into Gemini in visible Chromium. Session persists.
- **Pro + Extended Thinking with large knowledge**: May exceed default 120s timeout. Use `-t 600`.
- **Custom elements**: Angular Material components (`TOOLBOX-DRAWER-ITEM`) require `page.evaluate()` click.
- **Stale profile locks**: Auto-cleaned on launch. Kill orphaned Chrome if persistent.
- **Only one instance at a time**: Concurrent calls fight over the profile directory.
- **Xvfb required in WSL**: `Xvfb :0 -screen 0 1920x1080x24 &>/dev/null &`
- **Node.js v24 EPIPE**: Redirect stderr with `os.dup2()` before closing contexts.
