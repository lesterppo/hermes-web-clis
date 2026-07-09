#!/usr/bin/env python3
"""
gem-pw v4 — Self-contained Gemini Gem CLI. No external server needed.
Launches Chromium per call, uses persistent profile for session.

Commands:
  gem-pw <id> "prompt"                  Single-turn chat
  gem-pw <id> -c sess.json "prompt"     Multi-turn (persists conversation)
  gem-pw --create "Name" "Instr"        Create Gem
    --knowledge-file <f>                  Attach file to Gem knowledge
    --knowledge-code <url>                Import GitHub repo to knowledge
    --knowledge-photo <img>               Attach photo to knowledge
    --knowledge-folder <dir>              Zip & upload folder as knowledge
    -m <model> [--thinking extended]      Set default model
  gem-pw --edit <id> [options]          Edit existing Gem
    --name <n>                            Rename Gem
    --instructions <text>                 Replace instructions
    --knowledge-file <f>                  Add file to knowledge
    --knowledge-code <url>                Import GitHub repo to knowledge
    --knowledge-photo <img>               Add photo to knowledge
    --knowledge-folder <dir>              Zip & upload folder as knowledge
    -m <model> [--thinking extended]      Change default model
  gem-pw --delete <id>                  Delete Gem
  gem-pw --upload <id> -f <file> "Q"   Upload file + ask
  gem-pw --img <id> "description"       Generate image + download
  gem-pw --help                         This help
"""

import asyncio, base64, json, os, re, signal, sys, time
from datetime import datetime
from pathlib import Path

try: signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except: pass

PROFILE = str(Path.home() / ".gemini-cli" / "cr-profile")
OUTPUT_DIR = Path("/tmp")
QUIET = False
JSON_OUT = False  # compact output: {"ok":true,"f":"...","s":1234,"t":1.2}  # set by -q flag for agent consumption


def _log(msg):
    if QUIET: return
    ts = datetime.now().strftime("%H:%M:%S")
    os.write(2, f"[pw {ts}] {msg}\n".encode())

def _err(code, msg, **extra):
    d = {"ok": False, "err": code, "msg": msg}; d.update(extra); return d

def _ok(**kw): d = {"ok": True}; d.update(kw); return d


async def _launch():
    """Launch Chromium with persistent profile. Returns (pw, ctx, page)."""
    from playwright.async_api import async_playwright, Error as PwError
    # Clean stale profile locks from previous killed sessions
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lp = Path(PROFILE) / lock
        if lp.exists():
            try: lp.unlink()
            except: pass
    pw = await async_playwright().start()
    for attempt in range(3):
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=PROFILE, headless=False,
                args=['--no-sandbox','--disable-gpu','--disable-blink-features=AutomationControlled',
                      '--remote-debugging-port=0'],
                ignore_default_args=['--enable-automation'])
            page = await ctx.new_page()
            page.set_default_timeout(180000)
            return pw, ctx, page
        except PwError as e:
            if attempt < 2:
                _log(f"launch retry {attempt+1}: {e}")
                await asyncio.sleep(2)
            else:
                raise
    raise RuntimeError("Failed to launch")


async def _find_input(page, timeout=10):
    """Find chat input element. Retries for `timeout` seconds."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        el = await page.query_selector('div[role="textbox"][aria-label*="輸入提示"]')
        if not el: el = await page.query_selector('rich-textarea div[contenteditable="true"]')
        if not el: el = await page.query_selector('div[contenteditable="true"]:not(.ql-clipboard)')
        if el: return el
        await asyncio.sleep(1)
    return None


async def _type_and_submit(page, text):
    el = await _find_input(page)
    if not el: return False
    await el.click(); await asyncio.sleep(0.2)
    await page.keyboard.press("Control+a"); await asyncio.sleep(0.1)
    await page.keyboard.press("Backspace"); await asyncio.sleep(0.2)
    for i in range(0, len(text), 200):
        await el.type(text[i:i+200], delay=5)
    await asyncio.sleep(0.2)
    await page.keyboard.press("Enter")
    return True


async def _wait_response(page, timeout=120):
    t0 = time.time(); resp = ""
    for i in range(timeout // 3):
        await asyncio.sleep(3)
        _log(f"poll {i+1}/{timeout//3}")
        try:
            t = await page.evaluate("""() => {
                const msgs = document.querySelectorAll('.response-content, message-content, model-response');
                return msgs.length ? msgs[msgs.length-1].innerText : '';
            }""")
            if t and len(t) > len(resp): resp = t
        except: pass
        # Check completion: response exists AND all loading indicators are gone
        if resp:
            try:
                loading = await page.evaluate("""() => {
                    const sel = '.loading, .generating, .dot-pulse, [aria-busy="true"], .progress-indicator, .response-loading, .stream-text';
                    return document.querySelectorAll(sel).length;
                }""")
                if loading == 0:
                    await asyncio.sleep(2)
                    try:
                        t = await page.evaluate("""() => {
                            const msgs = document.querySelectorAll('.response-content, message-content');
                            return msgs.length ? msgs[msgs.length-1].innerText : '';
                        }""")
                        if t: resp = t
                    except: pass
                    break
            except: pass
        # Timeout guard: if resp hasn't changed in 6 polls (18s), it's probably done
        if resp and i >= 5:
            try:
                t = await page.evaluate("""() => {
                    const msgs = document.querySelectorAll('.response-content, message-content');
                    return msgs.length ? msgs[msgs.length-1].innerText : '';
                }""")
                if t and t == resp:  # unchanged for one cycle
                    await asyncio.sleep(1)
                    t2 = await page.evaluate("""() => {
                        const msgs = document.querySelectorAll('.response-content, message-content');
                        return msgs.length ? msgs[msgs.length-1].innerText : '';
                    }""")
                    if t2 == resp:  # unchanged for two cycles → done
                        _log("stable response, done")
                        break
            except: pass
    return resp, time.time() - t0


def _save(text, prompt, elapsed, gem_id=None, conv_file=None, output_path=None):
    if output_path:
        out = Path(output_path)
    else:
        ts = int(time.time())
        out = OUTPUT_DIR / f"gem-pw-{ts}.md"
    out.write_text(text, encoding="utf-8")
    _log(f"{elapsed:.1f}s {len(text)}c → {out}")
    r = {"ok": True, "f": str(out), "s": len(text), "b": len(text.encode("utf-8")), "t": round(elapsed, 1)}
    if gem_id: r["gem"] = gem_id
    if conv_file: r["c"] = conv_file
    if JSON_OUT:
        return {"ok": True, "f": str(out), "s": len(text), "t": round(elapsed, 1)}
    return r


def _check_page(body):
    if "Sign in" in body[:400]: return "NOT_SIGNED_IN"
    if "404" in body[:200] or "not found" in body[:200].lower(): return "NOT_FOUND"
    return None


async def _select_model(page, model, thinking):
    """Click model selector and pick model + thinking tier."""
    selectors = [
        'button[aria-label*="模式選擇器"]',
        'button:has-text("Pro")',
        'button:has-text("Flash")',
    ]
    clicked = False
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await asyncio.sleep(1)
                clicked = True
                _log(f"model selector clicked: {sel}")
                break
        except: pass

    if not clicked:
        _log("model selector not found, skipping model selection")
        return

    await asyncio.sleep(1)

    if model:
        model_map = {
            "flash-lite": ["3.1 Flash-Lite", "Flash-Lite"],
            "flash": ["3.5 Flash", "Flash"],
            "pro": ["3.1 Pro", "Pro"],
        }
        targets = model_map.get(model.lower(), [model])
        for t in targets:
            try:
                item = await page.query_selector(f'[role="menuitem"]:has-text("{t}")')
                if item:
                    await item.click()
                    await asyncio.sleep(1)
                    _log(f"base model: {t}")
                    break
            except: pass

    if thinking and thinking.lower() in ("extended", "extend"):
        await asyncio.sleep(0.5)
        try:
            ext_item = await page.query_selector('[role="menuitem"]:has-text("延伸思考")')
            if ext_item:
                await ext_item.click()
                await asyncio.sleep(1)
                _log("extended thinking: ON")
        except: pass

    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
    except: pass


# ═══════════════ CHAT ═══════════════

async def cmd_chat(gem_id, prompt, conv_file=None, model=None, thinking=None, brief=False, output_path=None, timeout=120):
    if brief:
        prompt = "Be concise. " + prompt
    pw, ctx, page = await _launch()
    try:
        conv_state = {}
        if conv_file:
            cf = Path(conv_file)
            if cf.exists():
                conv_state = json.loads(cf.read_text())
                old_url = conv_state.get("url","")
                if old_url and "gemini.google.com" in old_url:
                    _log(f"restoring conversation")
                    await page.goto(old_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(6)
                else:
                    await page.goto(f"https://gemini.google.com/gem/{gem_id}", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(4)
            else:
                await page.goto(f"https://gemini.google.com/gem/{gem_id}", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(4)
        else:
            await page.goto(f"https://gemini.google.com/gem/{gem_id}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)

        body = await page.inner_text("body")
        err = _check_page(body)
        if err: return _err(err, body[:200])
        if not await _find_input(page): return _err("NO_INPUT", "Chat input not found")

        if model or thinking:
            _log(f"model select: model={model} thinking={thinking}")
            await _select_model(page, model, thinking)

        if not await _type_and_submit(page, prompt): return _err("TYPE_FAILED", "Could not type")
        _log("waiting...")
        resp, elapsed = await _wait_response(page, timeout=timeout)
        if not resp.strip(): return _err("EMPTY", "No response after timeout")

        result = _save(resp, prompt, elapsed, gem_id=gem_id, conv_file=conv_file, output_path=output_path)
        if conv_file:
            conv_state["url"] = page.url
            Path(conv_file).write_text(json.dumps(conv_state))
        return result
    finally:
        try: await ctx.close()
        except: pass
        await pw.stop()


# ═══════════════ CREATE ═══════════════

async def cmd_create(name, instructions, knowledge_files=None, knowledge_code=None,
                     knowledge_photos=None, knowledge_folders=None, model=None, thinking=None):
    """Create a Gem with optional knowledge files, code repos, photos, folders."""
    pw, ctx, page = await _launch()
    try:
        _log("create: navigating to gems/create...")
        await page.goto("https://gemini.google.com/gems/create", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        ni = await page.query_selector('input[aria-label="輸入 Gem 名稱"]')
        if not ni: ni = await page.query_selector('input[placeholder="為 Gem 命名"]')
        if not ni: return _err("NO_NAME_INPUT", "Name input not found")
        await ni.click(); await asyncio.sleep(0.2); await ni.fill(name)
        _log(f"name: {name}")

        ie = await page.query_selector('div[contenteditable="true"][aria-label="向 Gemini 輸入提示"]')
        if not ie: return _err("NO_INST_INPUT", "Instructions input not found")
        await ie.click(); await asyncio.sleep(0.3)
        for i in range(0, len(instructions), 200):
            await ie.type(instructions[i:i+200], delay=5)
        _log(f"instructions: {len(instructions)} chars")
        await asyncio.sleep(2)

        if model:
            model_btn = await page.query_selector('button[aria-label*="模式選擇器"]')
            if model_btn:
                await model_btn.click(); await asyncio.sleep(3)
                model_map = {"flash-lite": "3.1 Flash-Lite", "flash": "3.5 Flash", "pro": "3.1 Pro"}
                target = model_map.get(model.lower(), model)
                await page.evaluate("(t) => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').includes(t)) i.click(); }); }", target)
                await asyncio.sleep(2)
                if thinking and thinking.lower() in ("extended", "extend"):
                    await model_btn.click(); await asyncio.sleep(4)
                    await page.evaluate("() => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').includes('延伸思考')) i.click(); }); }")
                    await asyncio.sleep(2)
                _log(f"model: {model}" + ("+ext" if thinking else ""))

        # Add knowledge
        if knowledge_folders:
            import tempfile, zipfile
            folder_files = []
            for fdir in knowledge_folders:
                fd = Path(fdir)
                if not fd.is_dir():
                    _log(f"SKIP: {fd} not a directory")
                    continue
                zip_path = Path(tempfile.mktemp(suffix=".zip"))
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in fd.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(fd))
                _log(f"zipped: {fd.name} → {zip_path.name} ({zip_path.stat().st_size} bytes)")
                folder_files.append(str(zip_path))
            knowledge_files = (knowledge_files or []) + folder_files

        all_files = (knowledge_files or []) + (knowledge_photos or [])
        if all_files or knowledge_code:
            _log("adding knowledge...")
            kb_btn = await page.query_selector('button[aria-label*="知識部分"][aria-label*="上載"]')
            if not kb_btn:
                _log("WARNING: knowledge button not found")
            else:
                for fpath in (knowledge_files or []):
                    fp = Path(fpath)
                    if not fp.exists(): _log(f"SKIP: {fp.name} not found"); continue
                    await kb_btn.click(); await asyncio.sleep(3)
                    await page.evaluate("() => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').includes('上載檔案')) i.click(); }); }")
                    await asyncio.sleep(2)
                    try:
                        async with page.expect_file_chooser(timeout=5000) as fc:
                            await page.evaluate("() => { const fi = document.querySelector('input[type=file]'); if (fi) fi.click(); }")
                        chooser = await fc.value
                        await chooser.set_files(str(fp))
                        await asyncio.sleep(3)
                        _log(f"uploaded: {fp.name}")
                    except Exception as e:
                        _log(f"upload failed: {fp.name}: {e}")

                for ppath in (knowledge_photos or []):
                    pp = Path(ppath)
                    if not pp.exists(): _log(f"SKIP: {pp.name} not found"); continue
                    await kb_btn.click(); await asyncio.sleep(3)
                    await page.evaluate("() => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').trim() === '相片') i.click(); }); }")
                    await asyncio.sleep(3)
                    dismiss = await page.query_selector('button:has-text("我明白了")')
                    if dismiss: await dismiss.click(); await asyncio.sleep(1)
                    try:
                        async with page.expect_file_chooser(timeout=5000) as fc:
                            await page.evaluate("() => { const fi = document.querySelector('input[type=file]'); if (fi) fi.click(); }")
                        chooser = await fc.value
                        await chooser.set_files(str(pp))
                        await asyncio.sleep(3)
                        _log(f"photo: {pp.name}")
                    except Exception as e:
                        _log(f"photo failed: {pp.name}: {e}")

                if knowledge_code:
                    await kb_btn.click(); await asyncio.sleep(3)
                    await page.evaluate("() => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').includes('匯入程式碼')) i.click(); }); }")
                    await asyncio.sleep(3)
                    gh = await page.query_selector('input[aria-label*="GitHub"]')
                    if not gh: gh = await page.query_selector('input[placeholder*="github.com"]')
                    if gh:
                        await gh.click(); await asyncio.sleep(0.3); await gh.fill(knowledge_code)
                        await asyncio.sleep(1)
                        imp = await page.query_selector('button:has-text("匯入")')
                        if not imp: imp = await page.query_selector('button:has-text("Import")')
                        if imp: await imp.click(); await asyncio.sleep(5); _log(f"code: {knowledge_code}")
                        else: _log("WARNING: Import button not found")
                    else:
                        _log("WARNING: GitHub URL input not found")

        sb = await page.query_selector('button:has-text("儲存")')
        if not sb: sb = await page.query_selector('button:has-text("Save")')
        gem_id = None
        if sb:
            await sb.click()
            await asyncio.sleep(6)
            for _ in range(10):
                for pg in ctx.pages:
                    m = re.search(r'/gem/([a-zA-Z0-9_-]+)', pg.url or "")
                    if m:
                        gem_id = m.group(1)
                        if gem_id not in ("view", "storybook", "brainstormer", "career-guide",
                                          "coding-partner", "learning-coach", "productivity-helper",
                                          "writing-editor", "create"):
                            break
                if gem_id: break
                await asyncio.sleep(1)
            if not gem_id:
                m = re.search(r'/gem/([a-zA-Z0-9_-]+)', page.url or "")
                if m: gem_id = m.group(1)
            if not gem_id:
                await page.goto("https://gemini.google.com/gems/view", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(4)
                gem_id = await page.evaluate("(name) => { const links = document.querySelectorAll('a[href*=\"/gem/\"]'); for (const a of links) { if (a.innerText.includes(name)) { const href = a.href; const idx = href.indexOf('/gem/'); if (idx >= 0) { const after = href.slice(idx + 5); const end = after.search(/[^a-zA-Z0-9_-]/); if (end >= 0) return after.slice(0, end); return after; } } } return null; }", name)

        _log(f"created: {gem_id or 'unknown'}")
        return _ok(action="create-gem", id=gem_id, name=name)
    finally:
        try: await ctx.close()
        except: pass
        await pw.stop()


# ═══════════════ EDIT ═══════════════

async def cmd_edit(gem_id, name=None, instructions=None, knowledge_files=None,
                   knowledge_code=None, knowledge_photos=None, knowledge_folders=None,
                   model=None, thinking=None):
    """Edit an existing Gem — change name, instructions, model, or add knowledge."""
    pw, ctx, page = await _launch()
    try:
        edit_url = f"https://gemini.google.com/gems/edit/{gem_id}"
        _log(f"edit: navigating to {edit_url}")
        await page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        if "/edit/" not in page.url:
            _log("not on edit page, trying options menu...")
            await page.goto(f"https://gemini.google.com/gems/{gem_id}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)
            await page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const ar = (b.getAttribute('aria-label')||'').toLowerCase();
                    if ((ar.includes('選項') || ar.includes('options')) && b.offsetParent) {
                        b.click(); return 'ok';
                    }
                }
                return 'not-found';
            }""")
            await asyncio.sleep(2)
            await page.evaluate("""() => {
                document.querySelectorAll('[role=menuitem]').forEach(i => {
                    if ((i.innerText||'').includes('編輯') || (i.innerText||'').includes('Edit'))
                        i.click();
                });
            }""")
            await asyncio.sleep(4)
            _log(f"after menu: {page.url}")

        changed = []

        if name:
            ni = await page.query_selector('input[aria-label="輸入 Gem 名稱"]')
            if not ni: ni = await page.query_selector('input[placeholder="為 Gem 命名"]')
            if ni:
                await ni.click(); await asyncio.sleep(0.2)
                await ni.fill(""); await ni.fill(name)
                _log(f"name → {name}")
                changed.append("name")

        if instructions:
            ie = await page.query_selector('div[contenteditable="true"][aria-label="向 Gemini 輸入提示"]')
            if ie:
                await ie.click(); await asyncio.sleep(0.3)
                await page.keyboard.press("Control+a"); await asyncio.sleep(0.1)
                await page.keyboard.press("Backspace"); await asyncio.sleep(0.2)
                for i in range(0, len(instructions), 200):
                    await ie.type(instructions[i:i+200], delay=5)
                _log(f"instructions → {len(instructions)} chars")
                changed.append("instructions")

        if model:
            model_btn = await page.query_selector('button[aria-label*="模式選擇器"]')
            if model_btn:
                await model_btn.click(); await asyncio.sleep(3)
                model_map = {"flash-lite": "3.1 Flash-Lite", "flash": "3.5 Flash", "pro": "3.1 Pro"}
                target = model_map.get(model.lower(), model)
                await page.evaluate("(t) => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').includes(t)) i.click(); }); }", target)
                await asyncio.sleep(2)
                if thinking and thinking.lower() in ("extended", "extend"):
                    await model_btn.click(); await asyncio.sleep(4)
                    await page.evaluate("() => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').includes('延伸思考')) i.click(); }); }")
                    await asyncio.sleep(2)
                _log(f"model: {model}" + ("+ext" if thinking else ""))
                changed.append("model")

        # Add knowledge
        if knowledge_folders:
            import tempfile, zipfile
            folder_files = []
            for fdir in knowledge_folders:
                fd = Path(fdir)
                if not fd.is_dir():
                    _log(f"SKIP: {fd} not a directory")
                    continue
                zip_path = Path(tempfile.mktemp(suffix=".zip"))
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in fd.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(fd))
                _log(f"zipped: {fd.name} → {zip_path.name} ({zip_path.stat().st_size} bytes)")
                folder_files.append(str(zip_path))
            knowledge_files = (knowledge_files or []) + folder_files

        all_files = (knowledge_files or []) + (knowledge_photos or [])
        if all_files or knowledge_code:
            _log("adding knowledge...")
            kb_btn = await page.query_selector('button[aria-label*="知識部分"][aria-label*="上載"]')
            if not kb_btn:
                kb_btn = await page.query_selector('button:has-text("新增檔案")')
            if not kb_btn:
                _log("WARNING: knowledge button not found on edit page")
            else:
                for fpath in (knowledge_files or []):
                    fp = Path(fpath)
                    if not fp.exists(): _log(f"SKIP: {fp.name} not found"); continue
                    await kb_btn.click(); await asyncio.sleep(3)
                    await page.evaluate("() => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').includes('上載檔案')) i.click(); }); }")
                    await asyncio.sleep(2)
                    try:
                        async with page.expect_file_chooser(timeout=5000) as fc:
                            await page.evaluate("() => { const fi = document.querySelector('input[type=file]'); if (fi) fi.click(); }")
                        chooser = await fc.value
                        await chooser.set_files(str(fp))
                        await asyncio.sleep(3)
                        _log(f"uploaded: {fp.name}")
                        changed.append(f"file:{fp.name}")
                    except Exception as e:
                        _log(f"upload failed: {fp.name}: {e}")

                for ppath in (knowledge_photos or []):
                    pp = Path(ppath)
                    if not pp.exists(): _log(f"SKIP: {pp.name} not found"); continue
                    await kb_btn.click(); await asyncio.sleep(3)
                    await page.evaluate("() => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').trim() === '相片') i.click(); }); }")
                    await asyncio.sleep(3)
                    dismiss = await page.query_selector('button:has-text("我明白了")')
                    if dismiss: await dismiss.click(); await asyncio.sleep(1)
                    try:
                        async with page.expect_file_chooser(timeout=5000) as fc:
                            await page.evaluate("() => { const fi = document.querySelector('input[type=file]'); if (fi) fi.click(); }")
                        chooser = await fc.value
                        await chooser.set_files(str(pp))
                        await asyncio.sleep(3)
                        _log(f"photo: {pp.name}")
                        changed.append(f"photo:{pp.name}")
                    except Exception as e:
                        _log(f"photo failed: {pp.name}: {e}")

                if knowledge_code:
                    await kb_btn.click(); await asyncio.sleep(3)
                    await page.evaluate("() => { document.querySelectorAll('[role=menuitem]').forEach(i => { if ((i.innerText||'').includes('匯入程式碼')) i.click(); }); }")
                    await asyncio.sleep(3)
                    gh = await page.query_selector('input[aria-label*="GitHub"]')
                    if not gh: gh = await page.query_selector('input[placeholder*="github.com"]')
                    if gh:
                        await gh.click(); await asyncio.sleep(0.3); await gh.fill(knowledge_code)
                        await asyncio.sleep(1)
                        imp = await page.query_selector('button:has-text("匯入")')
                        if not imp: imp = await page.query_selector('button:has-text("Import")')
                        if imp: await imp.click(); await asyncio.sleep(5); _log(f"code: {knowledge_code}"); changed.append(f"code:{knowledge_code.split('/')[-1]}")
                        else: _log("WARNING: Import button not found")
                    else:
                        _log("WARNING: GitHub URL input not found")

        if not changed:
            _log("no changes specified")
            return _ok(action="edit-gem", id=gem_id, changed=[], note="no changes")

        sb = await page.query_selector('button:has-text("儲存")')
        if not sb: sb = await page.query_selector('button:has-text("Save")')
        if not sb: sb = await page.query_selector('button:has-text("更新")')
        if not sb: sb = await page.query_selector('button:has-text("Update")')

        if sb:
            await sb.click()
            await asyncio.sleep(5)
            _log(f"saved! changes: {changed}")
        else:
            _log("WARNING: save button not found")

        return _ok(action="edit-gem", id=gem_id, changed=changed)
    finally:
        try: await ctx.close()
        except: pass
        await pw.stop()


# ═══════════════ DELETE ═══════════════

async def cmd_delete(gem_id):
    pw, ctx, page = await _launch()
    try:
        _log(f"delete: {gem_id}")
        await page.goto(f"https://gemini.google.com/gem/{gem_id}", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        clicked = await page.evaluate("""(gid) => {
            const btns = document.querySelectorAll('button, [role="button"]');
            for (const b of btns) {
                const ar = (b.getAttribute('aria-label')||'').toLowerCase();
                const tx = (b.innerText||'').toLowerCase();
                if ((ar.includes('gem') || ar.includes('選項') || ar.includes('options') || ar.includes('more')) &&
                    b.offsetParent) {
                    b.click();
                    return 'clicked:' + ar.slice(0,40);
                }
            }
            return 'not-found';
        }""", gem_id)
        _log(f"menu: {clicked}")
        await asyncio.sleep(2)

        deleted = await page.evaluate("""() => {
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const tx = (el.innerText||'').trim();
                if ((tx === '刪除' || tx === '刪除 Gem' || tx === 'Delete' || tx === 'Delete gem') &&
                    el.offsetParent && tx.length < 20) {
                    el.click();
                    return 'clicked:' + tx;
                }
            }
            const items = document.querySelectorAll('[role="menuitem"]');
            for (const item of items) {
                if ((item.innerText||'').trim() === '刪除') { item.click(); return 'clicked:menuitem'; }
            }
            return 'not-found';
        }""")
        _log(f"delete item: {deleted}")
        await asyncio.sleep(2)

        confirmed = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                const tx = (b.innerText||'').trim();
                if ((tx === '刪除' || tx === 'Delete') && b.offsetParent) {
                    b.click(); return 'confirmed:' + tx;
                }
            }
            return 'no-confirm';
        }""")
        _log(f"confirm: {confirmed}")
        await asyncio.sleep(3)

        return _ok(action="delete-gem", id=gem_id)
    finally:
        try: await ctx.close()
        except: pass
        await pw.stop()


# ═══════════════ UPLOAD ═══════════════

async def cmd_upload(gem_id, file_path, prompt):
    fp = Path(file_path)
    if not fp.exists(): return _err("FILE_NOT_FOUND", str(file_path))

    pw, ctx, page = await _launch()
    try:
        _log(f"upload: {fp.name}")
        await page.goto(f"https://gemini.google.com/gem/{gem_id}", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        body = await page.inner_text("body")
        err = _check_page(body)
        if err: return _err(err, body[:200])

        tools = await page.query_selector('[aria-label="上載同工具"]')
        if tools: await tools.click(); await asyncio.sleep(2)

        await page.evaluate("""() => {
            const items = document.querySelectorAll('toolbox-drawer-item, [role="menuitem"]');
            for (const item of items) {
                if ((item.innerText||'').trim().startsWith('上載檔案')) { item.click(); return; }
            }
        }""")
        await asyncio.sleep(2)

        fi = await page.wait_for_selector('input[type="file"]', state='attached', timeout=5000)
        if not fi: return _err("NO_UPLOAD", "File input not found")
        await fi.set_input_files(str(fp))
        await asyncio.sleep(3)

        if not await _type_and_submit(page, prompt): return _err("NO_INPUT", "Chat input not found")
        _log("waiting...")
        resp, elapsed = await _wait_response(page)
        if not resp.strip(): return _err("EMPTY", "No response")
        return _save(resp, prompt, elapsed, gem_id=gem_id)
    finally:
        try: await ctx.close()
        except: pass
        await pw.stop()


# ═══════════════ IMAGE GEN ═══════════════

async def cmd_image(gem_id, description):
    pw, ctx, page = await _launch()
    try:
        _log(f"image: {gem_id}")
        await page.goto(f"https://gemini.google.com/gem/{gem_id}", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        tools = await page.query_selector('[aria-label="上載同工具"]')
        if tools: await tools.click(); await asyncio.sleep(2)

        clicked = await page.evaluate("""() => {
            const items = document.querySelectorAll('toolbox-drawer-item');
            for (const item of items) {
                if ((item.innerText||'').trim().startsWith('製作圖像')) {
                    item.click(); return 'ok';
                }
            }
            return 'not-found';
        }""")
        _log(f"img btn: {clicked}")
        await asyncio.sleep(3)

        if clicked == 'ok':
            if not await _type_and_submit(page, description):
                return _err("NO_INPUT", "Chat input not found")
        else:
            if not await _type_and_submit(page, f"Generate an image: {description}"):
                return _err("NO_INPUT", "Chat input not found")

        _log("generating...")
        resp, elapsed = await _wait_response(page, timeout=60)
        if "Creating" in resp:
            _log("image still creating, waiting for render...")
            for _ in range(20):
                await asyncio.sleep(3)
                has_img = await page.evaluate("""() => {
                    return document.querySelectorAll('.response-content img[src], message-content img[src]').length;
                }""")
                if has_img > 0:
                    await asyncio.sleep(2)
                    break
            try:
                t = await page.evaluate("""() => {
                    const msgs = document.querySelectorAll('.response-content, message-content');
                    return msgs.length ? msgs[msgs.length-1].innerText : '';
                }""")
                if t: resp = t
            except: pass

        images = []
        try:
            img_els = await page.query_selector_all('.response-content img, message-content img')
            for idx, img_el in enumerate(img_els):
                try:
                    fname = OUTPUT_DIR / f"gem-img-{int(time.time())}-{idx}.png"
                    await img_el.screenshot(path=str(fname))
                    images.append(str(fname))
                    _log(f"image: {fname.name}")
                except Exception as e:
                    _log(f"img screenshot err: {e}")
        except Exception as e:
            _log(f"img err: {e}")

        r = {"ok": True, "t": round(elapsed, 1)}
        if resp.strip():
            r["s"] = len(resp)
            f = OUTPUT_DIR / f"gem-pw-{int(time.time())}.md"
            f.write_text(resp, encoding="utf-8"); r["f"] = str(f)
        if images: r["images"] = images
        if not resp.strip() and not images: r = _err("EMPTY", "No image generated")
        return r
    finally:
        try: await ctx.close()
        except: pass
        await pw.stop()


# ═══════════════ MAIN ═══════════════

async def main():
    global QUIET, JSON_OUT
    args = sys.argv[1:]
    if not args or args[0] in ("--help", "-h", "help"):
        print(json.dumps({"ok": True, "help": "gem-pw v4 — Self-contained Gemini Gem CLI",
            "commands": {
                "chat": "gem-pw <id> \"prompt\" [-c sess.json] [-m model] [--thinking extended] [-o out.md] [-t seconds] [--brief] [--new] [--json-out] [-q]",
                "create": "gem-pw --create \"Name\" \"Instructions\" [--knowledge-file <f>] [--knowledge-code <url>] [--knowledge-photo <img>] [--knowledge-folder <dir>] [-m model] [--thinking extended]",
                "edit": "gem-pw --edit <id> [--name <n>] [--instructions <text>] [--knowledge-file <f>] [--knowledge-code <url>] [--knowledge-photo <img>] [--knowledge-folder <dir>] [-m model] [--thinking extended]",
                "delete": "gem-pw --delete <id>",
                "upload": "gem-pw --upload <id> -f <file> \"question\"",
                "image": "gem-pw --img <id> \"description\""
            }}))
        sys.exit(0)

    if "-q" in args or "--quiet" in args: QUIET = True
    if "--json-out" in args: JSON_OUT = True

    if args[0] == "--create":
        kf = []; kc = None; kp = []; kdir = []; model = None; thinking = None
        name = None; inst = None; i = 1
        while i < len(args):
            if args[i] == "--knowledge-file" and i+1 < len(args): kf.append(args[i+1]); i += 2
            elif args[i] == "--knowledge-code" and i+1 < len(args): kc = args[i+1]; i += 2
            elif args[i] == "--knowledge-photo" and i+1 < len(args): kp.append(args[i+1]); i += 2
            elif args[i] == "--knowledge-folder" and i+1 < len(args): kdir.append(args[i+1]); i += 2
            elif args[i] == "-m" and i+1 < len(args): model = args[i+1]; i += 2
            elif args[i] == "--thinking" and i+1 < len(args): thinking = args[i+1]; i += 2
            elif args[i].startswith("-"): i += 1
            elif not name: name = args[i]; i += 1
            elif not inst: inst = args[i]; i += 1
            else: i += 1
        if not name or not inst:
            print(json.dumps(_err("USAGE", "--create <name> <instructions> [--knowledge-file <f>] [--knowledge-code <url>] [--knowledge-photo <img>] [--knowledge-folder <dir>] [-m <model>] [--thinking extended]")))
            sys.exit(1)
        print(json.dumps(await cmd_create(name, inst, knowledge_files=kf or None,
                                          knowledge_code=kc, knowledge_photos=kp or None,
                                          knowledge_folders=kdir or None,
                                          model=model, thinking=thinking)))
    elif args[0] == "--edit":
        if len(args) < 2: print(json.dumps(_err("USAGE", "--edit <gem-id> [--name <n>] [--instructions <text>] [--knowledge-file <f>] [--knowledge-code <url>] [--knowledge-photo <img>] [--knowledge-folder <dir>] [-m <model>] [--thinking extended]"))); sys.exit(1)
        gid = args[1]
        kf = []; kc = None; kp = []; kdir = []; model = None; thinking = None
        name = None; inst = None; i = 2
        while i < len(args):
            if args[i] == "--name" and i+1 < len(args): name = args[i+1]; i += 2
            elif args[i] == "--instructions" and i+1 < len(args): inst = args[i+1]; i += 2
            elif args[i] == "--knowledge-file" and i+1 < len(args): kf.append(args[i+1]); i += 2
            elif args[i] == "--knowledge-code" and i+1 < len(args): kc = args[i+1]; i += 2
            elif args[i] == "--knowledge-photo" and i+1 < len(args): kp.append(args[i+1]); i += 2
            elif args[i] == "--knowledge-folder" and i+1 < len(args): kdir.append(args[i+1]); i += 2
            elif args[i] == "-m" and i+1 < len(args): model = args[i+1]; i += 2
            elif args[i] == "--thinking" and i+1 < len(args): thinking = args[i+1]; i += 2
            elif args[i].startswith("-"): i += 1
            else: i += 1
        print(json.dumps(await cmd_edit(gid, name=name, instructions=inst,
                                        knowledge_files=kf or None,
                                        knowledge_code=kc, knowledge_photos=kp or None,
                                        knowledge_folders=kdir or None,
                                        model=model, thinking=thinking)))
    elif args[0] == "--delete":
        if len(args) < 2: print(json.dumps(_err("USAGE", "--delete <gem-id>"))); sys.exit(1)
        print(json.dumps(await cmd_delete(args[1])))
    elif args[0] == "--upload":
        fp = None; pp = []; i = 2; gid = args[1]
        while i < len(args):
            if args[i] == "-f" and i+1 < len(args): fp = args[i+1]; i += 2
            elif args[i].startswith("-"): i += 1
            else: pp.append(args[i]); i += 1
        p = " ".join(pp)
        if not fp or not p: print(json.dumps(_err("USAGE", "--upload <id> -f <file> <question>"))); sys.exit(1)
        print(json.dumps(await cmd_upload(gid, fp, p)))
    elif args[0] == "--img":
        if len(args) < 3: print(json.dumps(_err("USAGE", "--img <gem-id> <description>"))); sys.exit(1)
        print(json.dumps(await cmd_image(args[1], args[2])))
    else:
        try:
            m = re.search(r'gemini\.google\.com/gem/([a-zA-Z0-9_-]+)', args[0])
            gid = m.group(1) if m else (args[0] if '/' not in args[0] else args[0])
        except:
            print(json.dumps(_err("BAD_URL", "Invalid Gem ID or URL. Use gem-pw --help for usage.")))
            sys.exit(1)
        cf = None; model = None; thinking = None; brief = False; out = None; timeout = 120; pp = []; i = 1
        while i < len(args):
            if args[i] == "-c" and i+1 < len(args): cf = args[i+1]; i += 2
            elif args[i] == "-m" and i+1 < len(args): model = args[i+1]; i += 2
            elif args[i] == "--thinking" and i+1 < len(args): thinking = args[i+1]; i += 2
            elif args[i] == "-o" and i+1 < len(args): out = args[i+1]; i += 2
            elif args[i] == "-t" and i+1 < len(args): timeout = int(args[i+1]); i += 2
            elif args[i] in ("-q", "--quiet", "--json", "--json-out"): i += 1
            elif args[i] == "--brief": brief = True; i += 1
            elif args[i] == "--new": cf = None; i += 1
            elif args[i].startswith("-"): i += 1
            else: pp.append(args[i]); i += 1
        p = " ".join(pp).strip()
        if not p: print(json.dumps(_err("NO_PROMPT", "No prompt provided"))); sys.exit(1)
        print(json.dumps(await cmd_chat(gid, p, conv_file=cf, model=model, thinking=thinking, brief=brief, output_path=out, timeout=timeout)))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(json.dumps({"ok": False, "err": type(e).__name__, "msg": str(e)[:200]}))
        sys.exit(1)
