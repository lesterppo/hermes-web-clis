#!/usr/bin/env python3
"""
ChatGPT CLI — Playwright + Chrome profile (like Kimi).
⚠️ ChatGPT's Cloudflare Turnstile is the strictest of all AI platforms.
Headless mode works ONLY after a successful --login (visible browser).

Setup:
  python chatgpt.py --login    # One-time: opens chatgpt.com, log in, solve CAPTCHA
  python chatgpt.py "prompt"   # Then use normally

How it works:
  --login opens a visible browser (not headless). Cloudflare Turnstile 
  auto-resolves in a real browser. After login, cf_clearance is saved 
  to the persistent browser profile. Subsequent calls reuse it.
  
  If cf_clearance expires, re-run --login.

Note: This is the same architecture as Kimi CLI. ChatGPT's Cloudflare
is stricter, so the clearance expires faster.
"""

import os, sys, json, time, argparse, sqlite3, shutil
from pathlib import Path

def _safe_close(ctx, pw):
    """Close Playwright context/process, suppressing the Node.js v24 EPIPE
    crash that fires on stdout/stderr pipe teardown under PTY/subprocess."""
    _err_fd = None
    try:
        _err_fd = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
    except: pass
    try:
        if ctx: ctx.close()
    except: pass
    try:
        if pw: pw.stop()
    except: pass
    if _err_fd is not None:
        try:
            os.dup2(_err_fd, 2); os.close(_err_fd)
        except: pass
HOME = Path.home()
DIR = HOME / ".chatgpt-cli"
ACCOUNTS_FILE = DIR / "accounts.json"
BASE = "https://chatgpt.com"
DEFAULT_MODEL = "gpt-4o"
_Q = False

def fail(c,r): print(json.dumps({"ok":False,"err":c,"msg":r},ensure_ascii=False)); sys.exit(1)
def log(m): print(m,file=sys.stderr,flush=True)
def info(m):
    if not _Q and sys.stderr.isatty(): print(f"[chatgpt] {m}",file=sys.stderr)

def load_accounts():
    if ACCOUNTS_FILE.exists():
        try: return json.loads(ACCOUNTS_FILE.read_text())
        except: pass
    return {}

# Page server integration
_CHATGPT_SERVER_PORT = 9870
_CHATGPT_PID_FILE = DIR / "server.pid"

def _chatgpt_server_running() -> bool:
    if not _CHATGPT_PID_FILE.exists(): return False
    try:
        os.kill(int(_CHATGPT_PID_FILE.read_text().strip()), 0)
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{_CHATGPT_SERVER_PORT}/health", timeout=1)
        return True
    except: return False

def _try_chatgpt_server(prompt: str) -> dict | None:
    if not _chatgpt_server_running():
        from pathlib import Path as _Path
        server_script = _Path(__file__).resolve().parent.parent / "scripts" / "page_server.py"
        if not server_script.exists():
            return None
        import subprocess as _sp
        log_path = _Path.home() / ".chrome-daemon" / "chatgpt_server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _sp.Popen(
            [sys.executable, str(server_script), '--platform', 'chatgpt', '--port', str(_CHATGPT_SERVER_PORT), '--headed', '--start'],
            stdout=open(log_path, "a"), stderr=open(log_path, "a"),
            start_new_session=True,
        )
        import time as _time
        deadline = _time.time() + 30
        while _time.time() < deadline:
            if _chatgpt_server_running():
                break
            _time.sleep(0.5)
        if not _chatgpt_server_running():
            return None
    try:
        import urllib.request as _ur
        data = json.dumps({"prompt": prompt}).encode()
        req = _ur.Request(f"http://127.0.0.1:{_CHATGPT_SERVER_PORT}/query",
                          data=data, headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            if result.get("ok"): return result
    except: pass
    return None

def save_accounts(a):
    DIR.mkdir(parents=True,exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(a,indent=2))

def save_all_accounts():
    """Extract session info from Firefox."""
    for ud in Path("/mnt/c/Users").iterdir():
        if not ud.is_dir(): continue
        fp = ud / "AppData/Roaming/Mozilla/Firefox/Profiles"
        if not fp.exists(): continue
        for p in fp.iterdir():
            if not (p/"cookies.sqlite").exists(): continue
            try:
                t = Path(f"/tmp/cgs_{os.getpid()}.sqlite")
                shutil.copy2(str(p/"cookies.sqlite"),str(t))
                c = sqlite3.connect(str(t)); cur = c.cursor()
                cur.execute("SELECT name,value FROM moz_cookies WHERE host LIKE '%chatgpt%' OR host LIKE '%openai%'")
                ck = {n:v.strip('"') for n,v in cur.fetchall()}
                c.close(); t.unlink(missing_ok=True)
                for n,v in ck.items():
                    if n.startswith('__Secure-next-auth.session-token'):
                        label = p.name.replace(".default-release","").replace(".default","")[:15]
                        save_accounts({label: {"session": v[:20]+"...", "cookies": ck, "added": time.time()}})
                        print(f"✓ Saved: {label}")
                        return
            except: pass
    fail("no-auth","No ChatGPT session in Firefox. Log into chatgpt.com first.")

def list_accounts():
    a = load_accounts()
    if not a: print("No accounts. Run: --save-all"); return []
    for l, d in a.items():
        print(f"  {l}: session={d.get('session','?')}")
    return list(a.keys())

def sync_chrome_profile():
    """Copy Chrome cookies for Playwright use."""
    chrome = None
    env = os.environ.get("CHATGPT_CHROME_PROFILE","")
    if env: chrome = Path(env)
    else:
        for ud in Path("/mnt/c/Users").iterdir():
            if not ud.is_dir(): continue
            c = ud / "AppData/Local/Google/Chrome/User Data"
            if c.exists(): chrome = c; break
    if not chrome: return None
    
    profile = DIR / "chrome-profile"
    profile.mkdir(parents=True,exist_ok=True)
    src = chrome / "Default" / "Cookies"
    mark = profile / ".synced"
    if mark.exists() and src.exists():
        try:
            if src.stat().st_mtime <= mark.stat().st_mtime: return str(profile)
        except: pass
    info("Syncing Chrome profile...")
    for item in ["Default/Cookies","Default/Cookies-journal","Local State"]:
        sp = chrome/item; dp = profile/item
        if sp.exists():
            dp.parent.mkdir(parents=True,exist_ok=True)
            try: shutil.copy2(str(sp),str(dp))
            except: pass
    mark.touch()
    return str(profile)

def browser_login():
    """Open visible browser for ChatGPT login + Cloudflare Turnstile."""
    from playwright.sync_api import sync_playwright
    profile = str(DIR / "browser-profile")
    Path(profile).mkdir(parents=True,exist_ok=True)
    
    info("Opening browser... Log into ChatGPT and solve any Cloudflare challenge.")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            profile, headless=False,
            viewport={'width':1280,'height':800},
            args=['--no-sandbox','--disable-gpu','--disable-blink-features=AutomationControlled'])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(BASE, wait_until='commit')
        
        for i in range(600):
            try:
                body = pg.locator('body').inner_text()[:500]
                if 'ChatGPT' in body and 'Log in' not in body[:300]:
                    DIR.mkdir(parents=True,exist_ok=True)
                    (DIR/"auth.json").write_text(json.dumps({"logged_in":True,"saved_at":time.time()},indent=2))
                    info("Login successful! You can close the browser.")
                    ctx.close()
                    print(json.dumps({"ok":True,"msg":"Login complete"},ensure_ascii=False))
                    return
            except: pass
            if i%60==0 and i>0: info(f"Waiting... ({i}s)")
            time.sleep(1)
        ctx.close()
        fail("login-timeout","Login not detected within 10 minutes.")

def extract_response(body, prompt):
    """Extract ChatGPT response after the prompt in page body."""
    idx = body.find(prompt[:30])
    if idx < 0: return ''
    after = body[idx + min(len(prompt), 30):]
    lines = []; skip = True
    for line in after.split('\n'):
        line = line.strip()
        if not line: continue
        if skip and len(line) > 3: skip = False
        if not skip:
            if line in ('Regenerate','Copy','Read aloud','4o','o1','o3','o4-mini'): break
            if line == 'ChatGPT' and len(lines) > 0: continue
            lines.append(line)
    return '\n'.join(lines)

def chatgpt_chat(prompt, model=DEFAULT_MODEL, conv_url=None, profile_path=None):
    # Try page server first
    import urllib.request as _ur2, json as _js2
    result = _try_chatgpt_server(prompt)
    if result:
        return result.get("text", ""), ""
    # Fall back to direct Playwright
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            profile_path, headless=False,  # Non-headless required for Cloudflare
            viewport={'width':1280,'height':800},
            args=['--no-sandbox','--disable-gpu','--disable-blink-features=AutomationControlled'])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        
        url = conv_url or BASE
        pg.goto(url, wait_until='commit', timeout=20000)
        time.sleep(6)
        
        title = pg.title()
        if 'Just a moment' in title:
            ctx.close()
            fail("cloudflare","ChatGPT Cloudflare blocks headless. Run: chatgpt.py --login")
        
        # Find or focus the input
        prompt_box = pg.locator('#prompt-textarea, textarea, div[role="textbox"], [contenteditable="true"]').first
        if prompt_box.count() == 0:
            # Try clicking in the input area
            pg.mouse.click(640, 600); time.sleep(1)
            prompt_box = pg.locator('#prompt-textarea, textarea, div[role="textbox"], [contenteditable="true"]').first
        
        if prompt_box.count() == 0:
            ctx.close()
            fail("no-input","Chat input not found. Session may have expired. Run --login.")
        
        prompt_box.click(force=True); time.sleep(0.5)
        prompt_box.fill(prompt); time.sleep(0.5)
        pg.keyboard.press('Enter'); time.sleep(2)
        
        text = ""; deadline = time.time() + 180
        pre = len(pg.locator('body').inner_text())
        last = pre; stable = 0
        
        while time.time() < deadline:
            body = pg.locator('body').inner_text()
            cur = len(body)
            if cur > last: last = cur; stable = 0
            else: stable += 1
            if cur > pre + 100 and stable >= 5:
                text = extract_response(body, prompt)
                if text: break
            time.sleep(0.5)
        
        url = pg.url
        ctx.close()
        
        if not text: raise Exception("empty-response")
        return text, url

def load_conv(p):
    try: return json.loads(Path(p).read_text()) if Path(p).exists() else {}
    except: return {}
def save_conv(p,s):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(s,indent=2,ensure_ascii=False))

def main():
    global _Q
    p = argparse.ArgumentParser()
    p.add_argument("prompt",nargs="*"); p.add_argument("-p","--prompt-flag")
    p.add_argument("-m","--model",default=DEFAULT_MODEL)
    p.add_argument("-c","--conversation"); p.add_argument("--new",action="store_true")
    p.add_argument("-o","--output"); p.add_argument("--json",action="store_true")
    p.add_argument("-q","--quiet",action="store_true")
    p.add_argument("-l","--login",action="store_true")
    p.add_argument("--accounts",action="store_true")
    p.add_argument("--save-all",action="store_true")
    args = p.parse_args()
    if args.quiet: _Q = True
    
    if args.login: browser_login(); return
    if args.save_all: save_all_accounts(); return
    if args.accounts: list_accounts(); return
    
    prompt = args.prompt_flag or (" ".join(args.prompt) if args.prompt else None)
    if not prompt and not sys.stdin.isatty(): prompt = sys.stdin.read().strip()
    if not prompt: p.print_help(); sys.exit(1)
    
    conv = load_conv(args.conversation) if args.conversation else {}
    if args.new: conv = {}
    conv_url = conv.get("url") if not args.new else None
    
    # Use browser-profile (from --login) first, Chrome profile as fallback
    profile = str(DIR / "browser-profile")
    if not (Path(profile) / "Default").exists():
        profile = sync_chrome_profile()
    if not profile:
        fail("no-auth","No profile. Run: chatgpt.py --login")
    
    try:
        text, url = chatgpt_chat(prompt, model=args.model, conv_url=conv_url, profile_path=profile)
        log("[CHATGPT:DONE]")
        if args.conversation: conv["url"] = url; save_conv(args.conversation,conv)
        if args.output:
            op = Path(args.output); op.write_text(text,encoding="utf-8")
            print(json.dumps({"f":str(op),"s":op.stat().st_size,"b":text.count("```")//2},ensure_ascii=False))
        elif args.json:
            print(json.dumps({"ok":True,"text":text,"model":args.model},ensure_ascii=False))
        else:
            print(text)
    except SystemExit: raise
    except Exception as e: fail("error",str(e))

if __name__=="__main__":
    try: main()
    except SystemExit: raise
    except Exception as e: fail("error",str(e))
