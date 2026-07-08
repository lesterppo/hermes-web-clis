#!/usr/bin/env python3
"""
CLI for MiniMax Agent (agent.minimax.io) via Playwright + WSL Firefox cookies.
Uses TipTap/ProseMirror editor (not textarea). ~12-18s latency.
Token-efficient JSON pointer output.

Usage:
  python minimax.py "Hello"
  python minimax.py -m "MiniMax-M3 Thinking" "Complex task"
  python minimax.py -o /tmp/out.md "Quick prompt"
"""

import os, sys, json, time, argparse, textwrap, sqlite3, shutil
from datetime import datetime, timezone
from pathlib import Path

MINIMAX_HOME = Path.home() / ".minimax-cli"
MINIMAX_AUTH_FILE = MINIMAX_HOME / "auth.json"
MINIMAX_BROWSER_PROFILE = MINIMAX_HOME / "browser-profile"
MINIMAX_BASE_URL = "https://agent.minimax.io"
MINIMAX_DEFAULT_MODEL = "MiniMax-M3"

_Q = False

# Fast server path
_MINIMAX_SERVER_PORT = 9871
_MINIMAX_PID_FILE = MINIMAX_HOME / "server.pid"

def _server_running() -> bool:
    """Check if minimax_server.py is running."""
    if not _MINIMAX_PID_FILE.exists():
        return False
    try:
        pid = int(_MINIMAX_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{_MINIMAX_SERVER_PORT}/health", timeout=1)
        return True
    except Exception:
        return False

def _try_server_query(prompt: str) -> dict | None:
    """Try to query via running server. Auto-launches if not running."""
    if not _server_running():
        # Auto-launch the server on demand
        from pathlib import Path
        server_script = Path(__file__).resolve().parent / "minimax_server.py"
        if not server_script.exists():
            return None
        import subprocess
        log_path = Path.home() / ".chrome-daemon" / "minimax_server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [sys.executable, str(server_script)],
            stdout=open(log_path, "a"), stderr=open(log_path, "a"),
            start_new_session=True,
        )
        # Wait for server to be ready (up to 30s)
        deadline = time.time() + 30
        while time.time() < deadline:
            if _server_running():
                break
            time.sleep(0.5)
        if not _server_running():
            return None  # Failed to start, fall through to direct Playwright
    
    try:
        import urllib.request
        data = json.dumps({"prompt": prompt}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{_MINIMAX_SERVER_PORT}/query",
            data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                return result
    except Exception:
        pass
    return None

class MinimaxError(Exception):
    def __init__(self, code, msg):
        self.code = code; self.msg = msg; super().__init__(msg)

def fail(c, r): print(json.dumps({"ok":False,"err":c,"msg":r},ensure_ascii=False)); sys.exit(1)

def log(m):
    print(m, file=sys.stderr, flush=True)

def info(m):
    if not _Q and sys.stderr.isatty(): print(f"[minimax] {m}", file=sys.stderr)

# ── auth ─────────────────────────────────────────────────

def extract_firefox_cookies():
    ff = Path("/mnt/c/Users")
    for ud in ff.iterdir():
        if not ud.is_dir(): continue
        fp = ud / "AppData/Roaming/Mozilla/Firefox/Profiles"
        if not fp.exists(): continue
        for p in fp.iterdir():
            if not p.is_dir() or not (p / "cookies.sqlite").exists(): continue
            try:
                t = Path(f"/tmp/mm_ff_{os.getpid()}.sqlite")
                shutil.copy2(str(p / "cookies.sqlite"), str(t))
                c = sqlite3.connect(str(t)); cur = c.cursor()
                cur.execute("SELECT name,value,host FROM moz_cookies WHERE host LIKE '%minimax%' OR host LIKE '%hailuo%'")
                rows = cur.fetchall(); c.close(); t.unlink(missing_ok=True)
                if rows:
                    ck = {n: v.strip('"') for n, v, _ in rows}
                    info(f"Extracted {len(ck)} cookies from Firefox ({p.name})")
                    return ck
            except: pass
    return {}

def persist_auth(d):
    MINIMAX_HOME.mkdir(parents=True, exist_ok=True)
    d["saved_at"] = datetime.now(timezone.utc).isoformat()
    MINIMAX_AUTH_FILE.write_text(json.dumps(d, indent=2))

def get_auth():
    cs = os.environ.get("MINIMAX_COOKIE")
    if cs: return {"cookies": {k: v for p in cs.split("; ") if "=" in p for k, _, v in [p.partition("=")]}}
    if MINIMAX_AUTH_FILE.exists():
        try:
            d = json.loads(MINIMAX_AUTH_FILE.read_text())
            if d.get("cookies"): return d
        except: pass
    ck = extract_firefox_cookies()
    if ck:
        # Save only essential auth cookies to keep file small
        essential = {k: v for k, v in ck.items() if k in ("_token", "_sid", "ak_bmsc")}
        d = {"cookies": ck}; persist_auth(d); return d
    fail("no-auth", "No MiniMax cookies found. Log into https://agent.minimax.io in Windows Firefox first.")

def browser_login():
    from playwright.sync_api import sync_playwright
    info("Launching browser for MiniMax login...")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(MINIMAX_BROWSER_PROFILE), headless=False,
            viewport={"width":1280,"height":800},
            args=["--no-sandbox","--disable-gpu","--disable-blink-features=AutomationControlled"])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(MINIMAX_BASE_URL, wait_until="domcontentloaded")
        info("Waiting for login...")
        for i in range(300):
            cks = ctx.cookies()
            mm = [c for c in cks if "minimax" in (c.get("domain","")) and c.get("name") in ("_token","token","accessToken")]
            if mm:
                cd = {c["name"]:c["value"] for c in cks if "minimax" in (c.get("domain",""))}
                persist_auth({"cookies": cd})
                info(f"Login OK ({len(cd)} cookies)"); ctx.close(); return
            if i%30==0 and i>0: info(f"Waiting... ({i}s)")
            time.sleep(1)
        ctx.close(); fail("login-timeout","Login not detected within 5 min.")

# ── conversation ─────────────────────────────────────────

def load_conv(p):
    try: return json.loads(Path(p).read_text()) if Path(p).exists() else {}
    except: return {}

def save_conv(p, s):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(s, indent=2, ensure_ascii=False))

# ── JS extraction ────────────────────────────────────────

# MiniMax uses TipTap rich text editor. Response appears as rendered HTML.
EXTRACT_JS = """
() => {
    // MiniMax: assistant response text appears in .matrix-markdown.message-content
    // elements. Skip user messages (parent has justify-end class) and thought-process
    // blurbs. Take the longest non-user non-thought text.
    const contentEls = document.querySelectorAll('.matrix-markdown.message-content');
    let best = '';
    for (const el of contentEls) {
        // Skip user messages (parent has justify-end class)
        const parent = el.parentElement;
        if (parent && (parent.className.includes('justify-end') || parent.className.includes('justify-end'))) continue;
        const text = el.innerText?.trim() || '';
        // Skip thought-process blurbs (short and start with "Thought")
        if (text.startsWith('Thought') && text.length < 200) continue;
        if (text.length > best.length) best = text;
    }
    if (best) return best;
    
    // Fallback: .message-animate-in elements
    const msgEls = document.querySelectorAll('.message-animate-in');
    for (let i = msgEls.length - 1; i >= 0; i--) {
        const el = msgEls[i];
        const parent = el.parentElement;
        if (parent && parent.className.includes('justify-end')) continue;
        const text = el.innerText?.trim();
        if (text && !text.startsWith('Thought') && text.length > 1) {
            return text.split('\\n').filter(l => !l.match(/^\\d{2}:\\d{2}$/)).join('\\n').trim();
        }
    }
    return '';
}
"""

DONE_JS = """
() => {
    // MiniMax completion: stop button disappears, send re-enables
    const stopBtn = document.querySelector('button[class*="stop"], [aria-label*="stop" i]');
    const loading = document.querySelector('[class*="loading"], [class*="thinking-indicator"]');
    // Check if tip-tap editor is focused/active again (means response done)
    const editor = document.querySelector('.ProseMirror-focused, .ProseMirror');
    if (!stopBtn && !loading && editor) return true;
    return false;
}
"""

ERROR_JS = """
() => {
    const b = document.body.innerText;
    if (b.includes('Something went wrong')) return 'error';
    if (b.includes('rate limit') || b.includes('too many')) return 'rate-limit';
    // Check auth: sign in buttons + no user name = not logged in
    const signInCount = Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim()==='Sign in').length;
    if (signInCount >= 1 && !document.querySelector('.ProseMirror')) return 'auth-expired';
    return null;
}
"""

# ── browser ──────────────────────────────────────────────

def setup_cookies(ctx, auth):
    for n, v in auth.get("cookies", {}).items():
        d = ".minimax.io"
        if n in ("_token",): d = "agent.minimax.io"
        elif n in ("_sid","_lf"): d = "account.minimax.io"
        ctx.add_cookies([{"name":n,"value":v,"domain":d,"path":"/","httpOnly":False,"secure":True,"sameSite":"Lax"}])

def dismiss_modals(pg):
    for txt in ["Close", "Try it now", "Accept", "Got it"]:
        try:
            btns = pg.locator(f'button:has-text("{txt}")')
            for i in range(btns.count()):
                try:
                    b = btns.nth(i)
                    if b.is_visible(timeout=1000): b.click(); time.sleep(0.5)
                except: pass
        except: pass

def switch_model(pg, model):
    if model == MINIMAX_DEFAULT_MODEL and "Thinking" not in model: return
    log(f"[MINIMAX:MODEL] {model}")
    for sel in ['button:has-text("MiniMax-M3")', '[class*="model"] button']:
        try:
            b = pg.locator(sel).first
            if b.count()>0 and b.is_visible(timeout=3000): b.click(); time.sleep(1); break
        except: continue
    for sel in [f'[role="option"]:has-text("{model}")', f'li:has-text("{model}")', f'div:has-text("{model}")']:
        try:
            o = pg.locator(sel).first
            if o.count()>0 and o.is_visible(timeout=2000): o.click(); time.sleep(1); return
        except: continue

def toggle_thinking(pg, thinking: bool):
    """Toggle Thinking switch on MiniMax."""
    if thinking: return  # Default is on
    log("[MINIMAX:THINKING] off")
    try:
        sw = pg.locator('button[role="switch"]:has-text("Thinking")').first
        if sw.count()>0 and sw.is_visible(timeout=2000):
            is_on = sw.get_attribute("aria-checked")
            if is_on == "true": sw.click(); time.sleep(0.5)
    except: pass

def send_prompt(pg, prompt, model=MINIMAX_DEFAULT_MODEL, thinking=True, conv_url=None, debug=False):
    log("[MINIMAX:LOADING]")
    
    # Block slow resources for faster page load
    _block_slow(pg)
    
    if conv_url:
        pg.goto(conv_url, wait_until="domcontentloaded", timeout=30000)
    else:
        pg.goto(MINIMAX_BASE_URL, wait_until="domcontentloaded", timeout=30000)
    
    # Smart wait: wait for editor instead of fixed sleep(6)
    try:
        pg.wait_for_selector('.ProseMirror', timeout=10000)
    except Exception:
        time.sleep(4)  # Fallback if selector not found
    
    dismiss_modals(pg); time.sleep(0.5)
    
    # Only switch model if not default
    if model != MINIMAX_DEFAULT_MODEL or "Thinking" in model:
        switch_model(pg, model); time.sleep(0.5)

    # MiniMax uses TipTap ProseMirror editor
    editor = pg.locator('.ProseMirror').first
    if editor.count() == 0:
        editor = pg.locator("textarea").first
    if editor.count() == 0 or not editor.is_visible(timeout=8000):
        raise MinimaxError("no-input", "Chat editor not found. Auth may be expired — try --login.")
    
    if debug: info(f"Sending ({len(prompt)} chars)")
    
    # Focus and type (ProseMirror needs keyboard events, but low delay)
    editor.click(); time.sleep(0.1)
    pg.keyboard.type(prompt, delay=5); time.sleep(0.2)
    pg.keyboard.press("Enter")

    # Poll for response (0.3s intervals)
    text = ""; deadline = time.time() + 300
    done_at = None  # timestamp when DONE was first detected
    while time.time() < deadline:
        try:
            e = pg.evaluate(ERROR_JS)
            if e=="auth-expired": raise MinimaxError("auth-expired","Auth expired. Re-login with --login.")
            elif e=="error": raise MinimaxError("minimax-error","MiniMax returned an error.")
            elif e=="rate-limit": raise MinimaxError("rate-limit","Rate limited.")
        except MinimaxError: raise
        except Exception: pass
        try: done = pg.evaluate(DONE_JS)
        except Exception: done = False
        if done:
            if done_at is None:
                done_at = time.time()
            try: text = pg.evaluate(EXTRACT_JS)
            except Exception: pass
            # After DONE first fires, wait at least 6s for MiniMax's slow
            # streaming to finish rendering into the DOM, then extract.
            if text and len(text) > 30 and (time.time() - done_at) >= 10:
                break
        else:
            done_at = None
        time.sleep(0.3)
    return text, pg.url


def _block_slow(pg):
    """Block images, fonts, media for faster page load."""
    try:
        def _abort_slow(route):
            if route.request.resource_type in {"image", "font", "media"}:
                route.abort()
            else:
                route.continue_()
        pg.route("**/*", _abort_slow)
    except Exception:
        pass

# ── main ─────────────────────────────────────────────────

def main():
    global _Q
    p = argparse.ArgumentParser(description="CLI for MiniMax Agent", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", nargs="*"); p.add_argument("-p","--prompt-flag")
    p.add_argument("-m","--model", default=MINIMAX_DEFAULT_MODEL)
    p.add_argument("-c","--conversation"); p.add_argument("--new", action="store_true")
    p.add_argument("-o","--output"); p.add_argument("--json", action="store_true")
    p.add_argument("--no-thinking",action="store_true",help="Disable thinking mode")
    p.add_argument("-l","--login", action="store_true"); p.add_argument("--debug", action="store_true")
    p.add_argument("-q","--quiet", action="store_true")
    args = p.parse_args()
    if args.quiet: _Q = True

    if args.login: browser_login(); print(json.dumps({"ok":True,"msg":"Login saved"}, ensure_ascii=False)); return

    prompt = args.prompt_flag or (" ".join(args.prompt) if args.prompt else None)
    if not prompt and not sys.stdin.isatty(): prompt = sys.stdin.read().strip()
    if not prompt: p.print_help(); sys.exit(1)

    model = args.model; thinking = not args.no_thinking
    conv = load_conv(args.conversation) if args.conversation else {}
    if args.new: conv = {}
    if conv.get("model") and model == MINIMAX_DEFAULT_MODEL: model = conv["model"]
    conv_url = conv.get("url") if not args.new else None

    auth = get_auth()

    # Fast path: use running minimax_server.py if available
    result = _try_server_query(prompt)
    if result:
        text = result.get("text", "")
        if not text: raise MinimaxError("empty-response", "No response from server.")
        log("[MINIMAX:DONE]")
        if args.conversation:
            save_conv(args.conversation, conv)
        if args.output:
            op = Path(args.output); op.write_text(text, encoding="utf-8")
            print(json.dumps({"f":str(op),"s":op.stat().st_size,"b":text.count("```")//2}, ensure_ascii=False))
        elif args.json: print(json.dumps({"ok":True,"text":text,"model":model}, ensure_ascii=False))
        else: print(text)
        return

    br = ctx = pg = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            br = pw.chromium.launch(headless=True,
                args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"])
            ctx = br.new_context(viewport={"width":1280,"height":800})
            setup_cookies(ctx, auth)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            text, url = send_prompt(pg, prompt, model=model, thinking=thinking,
                                    conv_url=conv_url, debug=args.debug)
        finally:
            if pg: 
                try: pg.close()
                except: pass
            if ctx: 
                try: ctx.close()
                except: pass
            if br: 
                try: br.close()
                except: pass
            try: pw.stop()
            except: pass
        
        if not text: raise MinimaxError("empty-response","No response.")
        log("[MINIMAX:DONE]")
        if args.conversation: conv["url"]=url; conv["model"]=model; save_conv(args.conversation, conv)
        if args.output:
            op = Path(args.output); op.write_text(text, encoding="utf-8")
            print(json.dumps({"f":str(op),"s":op.stat().st_size,"b":text.count("```")//2},ensure_ascii=False))
        elif args.json: print(json.dumps({"ok":True,"text":text,"url":url,"model":model}, ensure_ascii=False))
        else: print(text)
    except MinimaxError as e:
        print(json.dumps({"ok":False,"err":e.code,"msg":e.msg},ensure_ascii=False))
        sys.exit(1)
    except SystemExit: raise
    except Exception as e: fail("error", str(e))

if __name__ == "__main__": main()
