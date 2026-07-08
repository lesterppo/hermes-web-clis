#!/usr/bin/env python3
"""
CLI for Xiaomi MiMo Studio (aistudio.xiaomimimo.com) via Playwright + WSL Firefox cookies.
~10-15s latency. Token-efficient JSON pointer output.
Supports multi-turn, model switching, thinking toggle.

Usage:
  python mimo.py "Hello"
  python mimo.py -m mimo-v2.5 "Multimodal task"
  python mimo.py -c chat.json "Turn 1" && python mimo.py -c chat.json "Turn 2"
  python mimo.py -o /tmp/out.md "Quick prompt"
"""

import os, sys, json, time, argparse, textwrap, sqlite3, shutil
from datetime import datetime, timezone
from pathlib import Path

MIMO_HOME = Path.home() / ".mimo-cli"
MIMO_AUTH_FILE = MIMO_HOME / "auth.json"
MIMO_BROWSER_PROFILE = MIMO_HOME / "browser-profile"
MIMO_BASE_URL = "https://aistudio.xiaomimimo.com"
MIMO_DEFAULT_MODEL = "mimo-v2.5-pro"
MIMO_MODEL_LABELS = {"mimo-v2.5-pro":"MiMo-V2.5-Pro","mimo-v2.5":"MiMo-V2.5","mimo-v2-flash":"MiMo-V2-Flash"}

_Q = False

# Page server integration
_MIMO_SERVER_PORT = 9872
_MIMO_PID_FILE = MIMO_HOME / "server.pid"

def _mimo_server_running() -> bool:
    if not _MIMO_PID_FILE.exists():
        return False
    try:
        pid = int(_MIMO_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{_MIMO_SERVER_PORT}/health", timeout=1)
        return True
    except Exception:
        return False

def _try_mimo_server(prompt: str, new_conv: bool = False) -> dict | None:
    if not _mimo_server_running():
        from pathlib import Path
        server_script = Path(__file__).resolve().parent / "mimo_server.py"
        if not server_script.exists():
            return None
        import subprocess
        log_path = Path.home() / ".chrome-daemon" / "mimo_server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [sys.executable, str(server_script)],
            stdout=open(log_path, "a"), stderr=open(log_path, "a"),
            start_new_session=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if _mimo_server_running():
                break
            time.sleep(0.5)
        if not _mimo_server_running():
            return None
    try:
        import urllib.request
        data = json.dumps({"prompt": prompt}).encode()
        path = "/query/new" if new_conv else "/query"
        req = urllib.request.Request(
            f"http://127.0.0.1:{_MIMO_SERVER_PORT}{path}",
            data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                return result
    except Exception:
        pass
    return None

class MimoError(Exception):
    def __init__(self, code, msg):
        self.code = code; self.msg = msg; super().__init__(msg)

def fail(c,r): print(json.dumps({"ok":False,"err":c,"msg":r},ensure_ascii=False)); sys.exit(1)
def log(m): print(m,file=sys.stderr,flush=True)
def info(m):
    if not _Q and sys.stderr.isatty(): print(f"[mimo] {m}",file=sys.stderr)

# ── auth ─────────────────────────────────────────────────

def extract_firefox_cookies():
    for ud in Path("/mnt/c/Users").iterdir():
        if not ud.is_dir(): continue
        fp = ud / "AppData/Roaming/Mozilla/Firefox/Profiles"
        if not fp.exists(): continue
        for p in fp.iterdir():
            if not p.is_dir() or not (p / "cookies.sqlite").exists(): continue
            try:
                t = Path(f"/tmp/mimo_ff_{os.getpid()}.sqlite")
                shutil.copy2(str(p / "cookies.sqlite"), str(t))
                c = sqlite3.connect(str(t)); cur = c.cursor()
                cur.execute("SELECT name,value,host FROM moz_cookies WHERE host LIKE '%xiaomi%' OR host LIKE '%mimo%'")
                rows = cur.fetchall(); c.close(); t.unlink(missing_ok=True)
                if rows:
                    ck = {n:v.strip('"') for n,v,_ in rows}
                    info(f"Extracted {len(ck)} cookies from Firefox ({p.name})")
                    return ck
            except: pass
    return {}

def persist_auth(d):
    MIMO_HOME.mkdir(parents=True,exist_ok=True)
    d["saved_at"]=datetime.now(timezone.utc).isoformat()
    MIMO_AUTH_FILE.write_text(json.dumps(d,indent=2))

def get_auth():
    cs = os.environ.get("MIMO_COOKIE")
    if cs: return {"cookies":{k:v for p in cs.split("; ") if "=" in p for k,_,v in [p.partition("=")]}}
    if MIMO_AUTH_FILE.exists():
        try:
            d = json.loads(MIMO_AUTH_FILE.read_text())
            if d.get("cookies"): return d
        except: pass
    ck = extract_firefox_cookies()
    if ck: d = {"cookies":ck}; persist_auth(d); return d
    fail("no-auth","No Xiaomi cookies found. Log into https://aistudio.xiaomimimo.com in Windows Firefox first.")

def browser_login():
    from playwright.sync_api import sync_playwright
    info("Launching browser for MiMo login...")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(MIMO_BROWSER_PROFILE),headless=False,
            viewport={"width":1280,"height":800},
            args=["--no-sandbox","--disable-gpu","--disable-blink-features=AutomationControlled"])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(f"{MIMO_BASE_URL}/#/c",wait_until="domcontentloaded")
        info("Waiting for login...")
        for i in range(300):
            cks = ctx.cookies()
            xm = [c for c in cks if "xiaomi" in (c.get("domain",""))]
            if len(xm) >= 2:
                cd = {c["name"]:c["value"] for c in xm}
                persist_auth({"cookies":cd})
                info(f"Login OK ({len(cd)} cookies)"); ctx.close(); return
            if i%30==0 and i>0: info(f"Waiting... ({i}s)")
            time.sleep(1)
        ctx.close(); fail("login-timeout","Login not detected within 5 min.")

# ── conversation ─────────────────────────────────────────

def load_conv(p):
    try: return json.loads(Path(p).read_text()) if Path(p).exists() else {}
    except: return {}

def save_conv(p,s):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(s,indent=2,ensure_ascii=False))

# ── JS extraction ────────────────────────────────────────

EXTRACT_JS = """
() => {
    // Get the response from the LAST "Thought for Xs" block
    const body = document.body.innerText;
    const footerIdx = body.indexOf('Developer demo platform');
    if (footerIdx < 0) return '';
    const beforeFooter = body.substring(0, footerIdx);
    
    // Find ALL "Thought for" matches, use the last one
    const thoughtMatches = [...beforeFooter.matchAll(/Thought for [\\d.]+ seconds?/g)];
    if (thoughtMatches.length === 0) return '';
    const lastThought = thoughtMatches[thoughtMatches.length - 1];
    const thoughtIdx = lastThought.index + lastThought[0].length;
    const afterThought = beforeFooter.substring(thoughtIdx);
    
    const lines = afterThought.split('\\n').map(l => l.trim()).filter(l => {
        if (!l) return false;
        if (l.startsWith('MiMo') || l.startsWith('MiMo-')) return false;
        // Filter out cookie consent and marketing UI chrome
        if (l.startsWith('We use cookies')) return false;
        if (l.startsWith('Cookie')) return false;
        if (l.includes('Accept All') || l.includes('Decline All')) return false;
        if (l.startsWith('Citation sources')) return false;
        if (l.startsWith('Flagship Model') || l.startsWith('Kingsoft')) return false;
        if (l.startsWith('Flexible Subscription') || l.startsWith('Daily free')) return false;
        if (l.startsWith('TokenPlan') || l.startsWith('Try Now')) return false;
        if (l.startsWith('API Service')) return false;
        if (l.includes('Cookie Policy') || l.includes('Cookie Setting')) return false;
        if (l.startsWith('Learn More')) return false;
        return true;
    });
    return lines.join('\\n').trim();
}
"""

DONE_JS = """
() => {
    // Simple heuristic: body text has stabilized (stopped growing)
    const body = document.body.innerText;
    const currentLen = body.length;
    const prevLen = parseInt(document.body.getAttribute('data-body-len') || '0');
    document.body.setAttribute('data-body-len', currentLen.toString());
    // Return true when length hasn't changed AND "Thought for" is present
    if (currentLen === prevLen && currentLen > 500 && body.includes('Thought for')) {
        const footerIdx = body.indexOf('Developer demo platform');
        const matches = [...body.matchAll(/Thought for [\\d.]+ seconds?/g)];
        if (matches.length > 0 && footerIdx > 0) {
            const lastIdx = matches[matches.length - 1].index + matches[matches.length - 1][0].length;
            const after = body.substring(lastIdx, footerIdx).trim();
            if (after.length > 5) return true;
        }
    }
    return false;
}
"""

ERROR_JS = """
() => {
    const b = document.body.innerText;
    if (b.includes('Something went wrong')) return 'error';
    if (b.includes('rate limit')) return 'rate-limit';
    const ta = document.querySelector('textarea');
    if (ta && ta.placeholder && ta.placeholder.includes('Sign in')) return 'auth-expired';
    return null;
}
"""

# ── browser helpers ──────────────────────────────────────

def setup_cookies(ctx,auth):
    for n,v in auth.get("cookies",{}).items():
        d = ".xiaomimimo.com"
        if n in ("passToken","pass_ua","deviceId","state","tick","sns-bind-step"): d = ".account.xiaomi.com"
        ctx.add_cookies([{"name":n,"value":v,"domain":d,"path":"/","httpOnly":False,"secure":True,"sameSite":"Lax"}])

def dismiss_modals(pg):
    for txt in ["Accept All","Dismiss"]:
        try:
            b = pg.locator(f'button:has-text("{txt}")').first
            if b.count()>0 and b.is_visible(timeout=1000): b.click(); time.sleep(0.5)
        except: pass

def switch_model(pg,model):
    label = MIMO_MODEL_LABELS.get(model,model)
    if model == MIMO_DEFAULT_MODEL: return
    log(f"[MIMO:MODEL] {label}")
    for sel in ['button[class*="model"]','button:has-text("MiMo")']:
        try:
            b = pg.locator(sel).first
            if b.count()>0 and b.is_visible(timeout=2000): b.click(); time.sleep(1); break
        except: continue
    for sel in [f'text="{label}"',f'[role="option"]:has-text("{label}")',f'li:has-text("{label}")']:
        try:
            o = pg.locator(sel).first
            if o.count()>0 and o.is_visible(timeout=2000): o.click(); time.sleep(1); return
        except: continue

def toggle_thinking(pg, thinking: bool):
    """Toggle thinking mode. MiMo has thinking on by default."""
    if thinking: return  # Default is on
    log("[MIMO:THINKING] off")
    try:
        # Find thinking toggle — typically a switch or button
        for sel in ['button[role="switch"]:has-text("Thinking")','[class*="thinking"] button','[class*="think"]']:
            sw = pg.locator(sel).first
            if sw.count()>0 and sw.is_visible(timeout=2000):
                is_on = sw.get_attribute("aria-checked")
                if is_on == "true": sw.click(); time.sleep(0.5)
                return
        # Fallback: try keyboard shortcut or look for thinking icon
        # MiMo-V2.5-Pro has thinking on by default — clicking the model button area
    except: pass

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

def send_prompt(pg, prompt, model=MIMO_DEFAULT_MODEL, thinking=True, conv_url=None, debug=False):
    log("[MIMO:LOADING]")
    
    # Block slow resources for faster page load
    _block_slow(pg)
    
    # Navigate to conversation URL if continuing, otherwise new chat
    if conv_url:
        pg.goto(conv_url, wait_until="domcontentloaded", timeout=30000)
    else:
        pg.goto(f"{MIMO_BASE_URL}/#/c", wait_until="domcontentloaded", timeout=30000)
    
    # Smart wait: wait for textarea instead of fixed sleep(5)
    try:
        pg.wait_for_selector('textarea', timeout=10000)
    except Exception:
        time.sleep(4)  # Fallback
    
    dismiss_modals(pg); time.sleep(0.3)
    
    # Early auth check
    ta = pg.locator("textarea").first
    if ta.count() > 0 and ta.is_visible(timeout=3000):
        ph = ta.get_attribute("placeholder") or ""
        if "sign in" in ph.lower():
            raise MimoError("auth-expired",
                "MiMo session expired. Log into https://aistudio.xiaomimimo.com in Windows Firefox, "
                "then run: python mimo.py --login")
    
    # Only switch model on first turn
    if not conv_url and model != MIMO_DEFAULT_MODEL:
        switch_model(pg, model); time.sleep(0.5)
        dismiss_modals(pg); time.sleep(0.5)
    
    toggle_thinking(pg, thinking)
    time.sleep(0.3)

    ta = pg.locator("textarea").first
    if ta.count()==0 or not ta.is_visible(timeout=8000):
        raise MimoError("no-input","Chat input not found. Auth may be expired.")
    if debug: info(f"Sending ({len(prompt)} chars)")
    ta.fill(prompt); time.sleep(0.2); ta.press("Enter")

    # Poll for response (0.5s intervals)
    pre_len = len(pg.locator("body").inner_text())
    text = ""; deadline = time.time() + 300
    consecutive_stable = 0
    last_len = pre_len
    text_stable_count = 0
    last_text_len = 0
    while time.time() < deadline:
        try:
            e = pg.evaluate(ERROR_JS)
            if e=="auth-expired": raise MimoError("auth-expired","Auth expired. Re-login.")
            elif e=="error": raise MimoError("mimo-error","MiMo error.")
            elif e=="rate-limit": raise MimoError("rate-limit","Rate limited.")
        except MimoError: raise
        except Exception: pass
        
        current_len = len(pg.locator("body").inner_text())
        if current_len > last_len:
            last_len = current_len
            consecutive_stable = 0
        else:
            consecutive_stable += 1
        
        # Body has grown past initial AND stabilized (3s no growth → 6 cycles at 0.5s)
        if current_len > pre_len + 50 and consecutive_stable >= 6:
            try: text = pg.evaluate(EXTRACT_JS)
            except: text = ""
            if text and len(text) > 2:
                # Don't break immediately — MiMo's DOM text nodes lag behind
                # body.innerText settling. Wait for extracted text to stabilize.
                cur_text_len = len(text)
                if cur_text_len == last_text_len and cur_text_len > 0:
                    text_stable_count += 1
                    if text_stable_count >= 6:  # ~3s of stable extracted text
                        break
                else:
                    last_text_len = cur_text_len
                    text_stable_count = 1
        time.sleep(0.5)
    # Strip user prompt if it appears at the start of the response
    if text.startswith(prompt):
        text = text[len(prompt):].strip()
    return text, pg.url

# ── main ─────────────────────────────────────────────────

def main():
    global _Q
    p = argparse.ArgumentParser(description="CLI for Xiaomi MiMo Studio")
    p.add_argument("prompt",nargs="*"); p.add_argument("-p","--prompt-flag")
    p.add_argument("-m","--model",default=MIMO_DEFAULT_MODEL)
    p.add_argument("-c","--conversation"); p.add_argument("--new",action="store_true")
    p.add_argument("-o","--output"); p.add_argument("--json",action="store_true")
    p.add_argument("--no-thinking",action="store_true",help="Disable thinking mode")
    p.add_argument("-l","--login",action="store_true"); p.add_argument("--debug",action="store_true")
    p.add_argument("-q","--quiet",action="store_true")
    args = p.parse_args()
    if args.quiet: _Q = True

    if args.login: browser_login(); print(json.dumps({"ok":True,"msg":"Login saved"},ensure_ascii=False)); return

    prompt = args.prompt_flag or (" ".join(args.prompt) if args.prompt else None)
    if not prompt and not sys.stdin.isatty(): prompt = sys.stdin.read().strip()
    if not prompt: p.print_help(); sys.exit(1)

    # Fast path: try page server first (avoids Node.js v24 EPIPE in subprocess)
    model = args.model
    server_result = _try_mimo_server(prompt, new_conv=args.new)
    if server_result:
        text = server_result.get("text", "")
        if not text: raise MimoError("empty-response", "No response from server.")
        log("[MIMO:DONE]")
        if args.conversation:
            conv["url"] = "server"; conv["model"] = model
            save_conv(args.conversation, conv)
        if args.output:
            op = Path(args.output); op.write_text(text, encoding="utf-8")
            print(json.dumps({"f":str(op),"s":op.stat().st_size,"b":text.count("```")//2}, ensure_ascii=False))
        elif args.json: print(json.dumps({"ok":True,"text":text,"model":model}, ensure_ascii=False))
        else: print(text)
        return

    model = args.model; thinking = not args.no_thinking
    conv = load_conv(args.conversation) if args.conversation else {}
    if args.new: conv = {}
    # Use model from conversation if continuing, unless explicitly overridden
    if conv.get("model") and model == MIMO_DEFAULT_MODEL: model = conv["model"]
    conv_url = conv.get("url") if not args.new else None

    # Suppress Node.js v24 EPIPE crashes during Playwright shutdown
    # Node v24 throws on unhandled 'error' events on streams
    env = os.environ.copy()
    env.setdefault("NODE_NO_WARNINGS", "1")
    
    auth = get_auth()
    br = ctx = pg = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            br = pw.chromium.launch(headless=True,
                args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"])
            ctx = br.new_context(viewport={"width":1280,"height":800})
            setup_cookies(ctx,auth)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            text, url = send_prompt(pg, prompt, model=model, thinking=thinking,
                                    conv_url=conv_url, debug=args.debug)
        finally:
            # Close in reverse order — context before browser
            # Wrap each close in try/except to survive Node.js pipe errors
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
        
        if not text: raise MimoError("empty-response","No response.")
        log("[MIMO:DONE]")
        if args.conversation:
            conv["url"]=url; conv["model"]=model
            save_conv(args.conversation, conv)
        if args.output:
            op = Path(args.output); op.write_text(text,encoding="utf-8")
            # Token-efficient: return tiny JSON pointer
            print(json.dumps({"f":str(op),"s":op.stat().st_size,"b":text.count("```")//2},ensure_ascii=False))
        elif args.json: print(json.dumps({"ok":True,"text":text,"url":url,"model":model},ensure_ascii=False))
        else: print(text)
    except MimoError as e:
        print(json.dumps({"ok":False,"err":e.code,"msg":e.msg},ensure_ascii=False))
        sys.exit(1)
    except SystemExit: raise
    except Exception as e: fail("error",str(e))

if __name__ == "__main__": main()
