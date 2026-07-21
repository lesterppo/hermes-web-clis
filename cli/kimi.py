#!/usr/bin/env python3
"""
CLI for Kimi (kimi.com) via Playwright + Chrome cookies.
Token-efficient: saves accounts, auto-switches on rate limits.

Setup:
  python kimi.py --save-all           # One-time: extracts Chrome session
  python kimi.py --accounts           # List saved accounts

Usage:
  python kimi.py "prompt"
  python kimi.py -m kimi-k3 "prompt"          # K3 · Max (default, latest flagship)
  python kimi.py -m kimi-k3-swarm "prompt"    # K3 Swarm · Max
  python kimi.py -m kimi-k2.6 "prompt"        # K2.6 · Fast
  python kimi.py --account label "prompt"
  python kimi.py -c chat.json "Turn 1" && python kimi.py -c chat.json "Turn 2"

Models (slug -> exact UI label; the picker only matches labels, not slugs):
  kimi-k3       -> "K3 · Max"          (default — flagship Chat & Agent all-rounder)
  kimi-k3-swarm -> "K3 Swarm · Max"    (max search / batch processing)
  kimi-k2.6     -> "K2.6 · Fast"       (fast chat, quick replies)
  kimi-k2       -> "K2.6 · Fast"       (legacy alias; K2 series discontinued 2026-05-25)
"""

import os, sys, json, time, argparse, sqlite3, shutil, re
from pathlib import Path

HOME = Path.home()
DIR = HOME / ".kimi-cli"
ACCOUNTS_FILE = DIR / "accounts.json"
PROFILE_DIR = DIR / "chrome-profile"  # Native reusable profile (created by --login)
BASE = "https://www.kimi.com"
DEFAULT_MODEL = "kimi-k3"
# Maps our slug to the exact label Kimi renders in its model picker.
# switch_model() MUST click the label, never the slug — the UI has no slug text.
MODEL_LABELS = {
    "kimi-k3": "K3 · Max",
    "kimi-k3-swarm": "K3 Swarm · Max",
    "kimi-k2.6": "K2.6 · Fast",
    "kimi-k2": "K2.6 · Fast",  # legacy alias; K2 series discontinued 2026-05-25
}
CHROME_SRC = None  # Auto-detected from WSL Windows mount

def _find_chrome_profile():
    """Auto-detect Windows Chrome profile path from WSL."""
    global CHROME_SRC
    if CHROME_SRC: return CHROME_SRC
    # Check env var first
    env_path = os.environ.get("KIMI_CHROME_PROFILE", "")
    if env_path:
        CHROME_SRC = Path(env_path)
        return CHROME_SRC
    # Auto-detect from /mnt/c/Users/
    for ud in Path("/mnt/c/Users").iterdir():
        if not ud.is_dir(): continue
        candidate = ud / "AppData/Local/Google/Chrome/User Data"
        if candidate.exists():
            CHROME_SRC = candidate
            return CHROME_SRC
    return None
RATE_COOLDOWN = 300
_Q = False

# Financial terms that Kimi/Qwen block — fail fast instead of hanging
_FINANCE_KEYWORDS = [
    'stock price', 'share price', 'market cap', 'trading at', 'dividend yield',
    'earnings report', 'quarterly revenue', 'p/e ratio', 'balance sheet',
    'cash flow statement', 'income statement', 'ebitda', 'eps ', 'pe ratio',
    'nyse', 'nasdaq', 'ticker', 'etf price', 'index fund', 's&p 500',
    'dow jones', 'ftse', 'hang seng', 'nikkei', 'stock market',
]
_FINANCE_TICKER_RE = re.compile(r'\$[A-Z]{1,5}\b|\b[A-Z]{1,5}\s+(?:stock|share|ticker)\b', re.IGNORECASE)

def _is_finance_query(prompt: str) -> bool:
    """Detect if a prompt is a financial query that Kimi will block."""
    pl = prompt.lower()
    if any(kw in pl for kw in _FINANCE_KEYWORDS):
        return True
    if _FINANCE_TICKER_RE.search(prompt):
        return True
    return False

def fail(c, r):
    print(json.dumps({"ok": False, "err": c, "msg": r}, ensure_ascii=False)); sys.exit(1)
def log(m): print(m, file=sys.stderr, flush=True)
def info(m):
    if not _Q and sys.stderr.isatty(): print(f"[kimi] {m}", file=sys.stderr)

# ── accounts ─────────────────────────────────────────────

def load_accounts():
    if ACCOUNTS_FILE.exists():
        try: return json.loads(ACCOUNTS_FILE.read_text())
        except: pass
    return {}

def save_accounts(accts):
    DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(accts, indent=2))

def extract_kimi_account():
    """Extract kimi-auth JWT from Firefox and decode identity."""
    for ud in Path("/mnt/c/Users").iterdir():
        if not ud.is_dir(): continue
        fp = ud / "AppData/Roaming/Mozilla/Firefox/Profiles"
        if not fp.exists(): continue
        for p in fp.iterdir():
            if not (p / "cookies.sqlite").exists(): continue
            try:
                t = Path(f"/tmp/ke_{os.getpid()}.sqlite")
                shutil.copy2(str(p / "cookies.sqlite"), str(t))
                c = sqlite3.connect(str(t)); cur = c.cursor()
                cur.execute("SELECT name,value FROM moz_cookies WHERE name='kimi-auth' AND host LIKE '%kimi%'")
                row = cur.fetchone()
                c.close(); t.unlink(missing_ok=True)
                if row:
                    jwt = row[1].strip('"')
                    try:
                        import base64
                        parts = jwt.split(".")
                        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                        data = json.loads(base64.urlsafe_b64decode(payload))
                        return {
                            "jwt": jwt,
                            "sub": data.get("sub", "?")[:20],
                            "email": data.get("email", data.get("sub", "unknown")),
                            "profile": p.name,
                        }
                    except:
                        return {"jwt": jwt, "sub": "unknown", "email": "unknown", "profile": p.name}
            except: pass
    return None

def save_all_accounts():
    """Extract and save account info from Firefox JWT."""
    acct = extract_kimi_account()
    if not acct:
        fail("no-auth", "No Kimi session in Firefox. Log into kimi.com first.")
    
    accts = load_accounts()
    label = acct["profile"].replace(".default-release", "").replace(".default", "")
    if len(label) > 15: label = label[:15]
    
    accts[label] = {
        "sub": acct["sub"],
        "email": acct["email"],
        "jwt": acct["jwt"],
        "profile": acct["profile"],
        "added": time.time(),
    }
    save_accounts(accts)
    print(f"✓ Saved account: {label} ({acct['email']})")

def list_accounts():
    accts = load_accounts()
    if not accts:
        print("No accounts. Run: kimi.py --save-all")
        return []
    print(f"  {'LABEL':<15} {'ACCOUNT':<35} {'DEFAULT'}")
    print(f"  {'-'*15} {'-'*35} {'-'*7}")
    default = list(accts.keys())[0]
    for label, a in accts.items():
        marker = "←" if label == default else ""
        print(f"  {label:<15} {a.get('email','?')[:34]:<35} {marker}")
    return list(accts.keys())

# ── browser profile ──────────────────────────────────────

def sync_profile():
    """Return a usable Chrome profile path for headless runs.

    Priority:
      1. Native login profile from `kimi.py --login` (preferred — contains a
         real, send-capable session).
      2. Fall back to copying Windows Chrome cookies (works only if the
         Windows session is currently signed in and not DPAPI-locked).
    """
    # 1. Native login profile (created by --login) — first choice
    if _profile_ready():
        return str(PROFILE_DIR)

    # 2. Fallback: copy Windows Chrome cookies
    chrome_src = _find_chrome_profile()
    if not chrome_src:
        return None

    profile_dir = DIR / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    src_cookies = chrome_src / "Default" / "Cookies"
    mark = profile_dir / ".synced"

    if mark.exists() and src_cookies.exists():
        try:
            if src_cookies.stat().st_mtime <= mark.stat().st_mtime:
                return str(profile_dir)
        except: pass

    info("Syncing Chrome cookies (fallback). For a reliable session, run: kimi.py --login")
    for item in ["Default/Cookies", "Default/Cookies-journal", "Local State"]:
        sp = chrome_src / item; dp = profile_dir / item
        if sp.exists():
            dp.parent.mkdir(parents=True, exist_ok=True)
            try: shutil.copy2(str(sp), str(dp))
            except: pass

    mark.touch()
    return str(profile_dir)

# ── Chrome login helper (headed, reusable profile) ────────

def chrome_login_helper():
    """Launch HEADED Chrome (DISPLAY=:0) so the user can sign into kimi.com
    once. Saves the session into our native PROFILE_DIR, which every later
    headless run reuses. Returns the profile dir path."""
    import shutil as _shutil
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    disp = os.environ.get("DISPLAY", ":0")
    info(f"Launching headed Chrome on DISPLAY={disp}. Sign in to kimi.com, then close the window.")
    info(f"Profile will be saved at: {PROFILE_DIR}")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,
                executable_path=_resolve_chrome_exe(),
                args=[
                    f"--display={disp}",
                    "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                    # Don't let Playwright's default automation flags interfere
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(viewport={"width": 1200, "height": 800})
            pg = ctx.new_page()
            pg.goto(BASE, wait_until="commit", timeout=45000)
            # Kimi holds the connection open (persistent WebSocket), so
            # 'domcontentloaded' never fires — use a fixed settle wait.
            time.sleep(6)
            info("Waiting for you to log in (window is open)... press Enter here when done.")
            try:
                input("Press ENTER after you have signed in to kimi.com and can chat > ")
            except EOFError:
                # Non-interactive: wait a fixed grace period for the user to log in
                time.sleep(60)
            # Confirm login by checking for the composer / absence of login wall
            logged_in = False
            try:
                body = pg.locator('body').inner_text()
                logged_in = "Log in with phone" not in body and "Continue with Google" not in body \
                            and pg.locator('[contenteditable="true"]').count() > 0
            except: pass
            ctx.close(); browser.close()
            if logged_in:
                print(f"✓ Logged in. Profile saved at {PROFILE_DIR}")
                print(f"  Reuse with: python kimi.py \"prompt\"   (auto-detected)")
                return str(PROFILE_DIR)
            else:
                print("⚠ Login not detected. Profile saved but may be incomplete — re-run --login.")
                return str(PROFILE_DIR)
    except Exception as e:
        fail("login-failed", f"Could not launch headed Chrome: {e}")

def _resolve_chrome_exe():
    """Find a Chrome/Chromium executable to drive headed login."""
    import shutil as _shutil
    for cand in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"]:
        found = _shutil.which(cand) or (cand if os.path.exists(cand) else None)
        if found:
            return found
    # Fall back to Playwright's bundled chromium
    return None

def _profile_ready():
    """True if our native profile exists with a Cookies db (i.e. --login ran)."""
    cookies = PROFILE_DIR / "Default" / "Network" / "Cookies"
    return cookies.exists()

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

def extract_response(pg):
    """Extract the last Kimi AI response.

    Kimi renders the conversation in a `.layout-content` (`.main`) container.
    Within it, the structure after a reply is:
        <prompt>\\nEdit\\nCopy\\nShare\\n<RESPONSE TEXT>\\n\\n\\nAsk anything, or task an agent...
    So the reply is the text AFTER the last "Share" marker and BEFORE the
    composer placeholder ("Ask anything"). This is far more reliable than
    `body.inner_text()` (which mixes in sidebar chat titles) or the
    `message-list` node (which is virtualized/empty under automation).
    Falls back to the body-level Share-walk if the container is missing.
    """
    try:
        main = pg.locator('.layout-content, .main').first
        if main.count():
            text = main.inner_text()
        else:
            text = pg.locator('body').inner_text()
        lines = text.split('\n')
        shares = [i for i, l in enumerate(lines) if l.strip() == 'Share']
        if not shares:
            return ''
        start = shares[-1] + 1
        # Stop at the composer placeholder (excludes the model badge etc.)
        end = len(lines)
        for i in range(start, len(lines)):
            if lines[i].strip().startswith('Ask anything'):
                end = i
                break
        reply = '\n'.join(lines[start:end]).strip()
        if reply:
            return reply
    except: pass
    return ''

def _current_model(pg):
    """Read the active model label from the composer badge (e.g. 'K3 · Max')."""
    try:
        badge = pg.locator('[class*="model"]').first
        if badge.count() and badge.is_visible(timeout=1000):
            return badge.inner_text().strip()
    except: pass
    return ""

def switch_model(pg, model):
    """Switch Kimi model via UI. Clicks the exact picker label, not the slug."""
    label = MODEL_LABELS.get(model, model)
    if model == DEFAULT_MODEL and _current_model(pg) == MODEL_LABELS.get(DEFAULT_MODEL, DEFAULT_MODEL):
        return
    info(f"Switching model to {model} ({label})")
    try:
        pg.locator('[class*="model"]').first.click(); time.sleep(1)
        pg.locator(f'text="{label}"').first.click(); time.sleep(1)
        # Close the picker if it is still open / re-confirm
        cur = _current_model(pg)
        if cur and cur != label:
            # Picker may not have closed; try once more
            try:
                pg.locator(f'text="{label}"').first.click(); time.sleep(1)
            except: pass
    except Exception as e:
        info(f"switch_model failed: {e}")

def _set_thinking(pg, want_on: bool):
    """Toggle Kimi's thinking/reasoning switch (when a toggle control exists)."""
    try:
        # Kimi exposes a thinking toggle near the composer; try common labels.
        for sel in ['button[aria-label*="think" i]', 'button:has-text("Thinking")',
                    '[class*="think"]']:
            loc = pg.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1000):
                # Heuristic: if it reads ON we leave it; click to flip state.
                loc.click(); time.sleep(0.5); return
    except: pass

def _attach_files(pg, files):
    """Attach one or more files/images via Kimi's composer toolkit.

    The composer has a hidden ``input[type=file]`` inside the 'Add files &
    photos' toolkit item. Clicking the item label is unreliable under headless,
    but Playwright's ``set_input_files`` works directly on the hidden input.
    Returns True if at least one attachment chip appeared.
    """
    if not files:
        return False
    paths = [str(Path(f).resolve()) for f in files if Path(f).exists()]
    if not paths:
        return False
    # Open the toolkit so the input is in the live DOM tree.
    try:
        pg.locator('.toolkit-trigger-btn').first.click(timeout=3000); time.sleep(2)
    except: pass
    fi = pg.locator('input[type=file]')
    if fi.count() == 0:
        return False
    # Set on every file input (Kimi may present separate doc/image inputs).
    attached = False
    for i in range(fi.count()):
        try:
            fi.nth(i).set_input_files(paths); time.sleep(2)
            attached = True
        except: pass
    time.sleep(1)
    return attached
def kimi_chat(prompt, model=DEFAULT_MODEL, conv_url=None, profile_path=None, thinking=True, files=None):
    from playwright.sync_api import sync_playwright
    
    pw = sync_playwright().start()
    ctx = pg = None
    try:
        ctx = pw.chromium.launch_persistent_context(
            profile_path, headless=True,
            viewport={'width': 1280, 'height': 800},
            args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        if conv_url:
            pg.goto(conv_url, wait_until='commit', timeout=45000)
            time.sleep(6)
        else:
            pg.goto(BASE, wait_until='commit', timeout=45000)
            # Kimi holds the connection open (persistent WebSocket); 'domcontentloaded'
            # never fires. Use a fixed settle wait instead.
            time.sleep(4)
            # Start a fresh chat so parallel in-flight chats can't block the reply.
            # The sidebar "New Chat" item is outside the viewport under headless,
            # so Playwright's .click() times out. Use a JS .click() which works.
            # Also dismiss any modal overlays first.
            try:
                pg.keyboard.press('Escape'); time.sleep(0.5)
            except: pass
            try:
                pg.locator('text="New Chat"').first.evaluate("e => e.click()")
            except:
                try:
                    pg.locator('text="New Chat"').first.click(force=True, timeout=3000)
                except: pass
            time.sleep(2)
            switch_model(pg, model)
            if not thinking:
                _set_thinking(pg, False)

        pre_shares = pg.locator('body').inner_text().count('Share')

        # Dismiss any modal/popup that might intercept pointer events
        try:
            modal = pg.locator('.modal-mask, .dialog-mask, [class*="modal"], [class*="dialog"]').first
            if modal.count() and modal.is_visible(timeout=2000):
                # Try clicking the close button or pressing Escape
                try:
                    close_btn = pg.locator('[class*="close"], [class*="cancel"], button:has-text("Close"), button:has-text("Cancel")').first
                    if close_btn.count() and close_btn.is_visible(timeout=1000):
                        close_btn.click(force=True, timeout=2000); time.sleep(1)
                except: pass
                try:
                    pg.keyboard.press('Escape'); time.sleep(1)
                except: pass
        except: pass

        editor = pg.locator('[contenteditable="true"]').first
        if editor.count() == 0:
            raise Exception("no-input")
        # Attach files/images before typing the prompt (opens the toolkit,
        # sets the hidden file input, chip appears in the composer).
        if files:
            try:
                _attach_files(pg, files)
            except Exception as e:
                info(f"attach failed: {e}")
        # Use force=True to bypass video/modal pointer interception (Kimi
        # sometimes shows promotional videos or modal overlays that block
        # normal click actionability checks).
        editor.click(force=True, timeout=5000); time.sleep(0.5)
        editor.fill(prompt); time.sleep(0.5)
        editor.press('Enter'); time.sleep(1)

        try:
            for sel in ['button[aria-label*="send" i]', 'button[type="submit"]']:
                btn = pg.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    btn.click(force=True, timeout=3000); time.sleep(0.5); break
        except: pass

        text = ""; deadline = time.time() + 180
        while time.time() < deadline:
            try:
                body = pg.locator('body').inner_text()
                if body.count('Share') > pre_shares:
                    time.sleep(3)
                    # Primary extractor uses the assistant message container;
                    # the DOM may still be settling, so retry until stable.
                    text = extract_response(pg)
                    if text and len(text) > 2: break
            except: pass
            time.sleep(0.5)

        url = pg.url

        if not text: raise Exception("empty-response")
        return text, url
    finally:
        _safe_close(ctx, pw)

# ── conversation ─────────────────────────────────────────

def load_conv(p):
    try: return json.loads(Path(p).read_text()) if Path(p).exists() else {}
    except: return {}
def save_conv(p, s):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(s, indent=2, ensure_ascii=False))

# ── main ─────────────────────────────────────────────────

def main():
    global _Q
    p = argparse.ArgumentParser()
    p.add_argument("prompt", nargs="*"); p.add_argument("-p", "--prompt-flag")
    p.add_argument("-m", "--model", default=DEFAULT_MODEL,
                   help="Model slug: kimi-k3 (default), kimi-k3-swarm, kimi-k2.6, kimi-k2")
    p.add_argument("--no-thinking", action="store_true",
                   help="Disable thinking/reasoning mode (faster).")
    p.add_argument("-f", "--file", action="append", default=[],
                   help="Attach a file or image (repeatable). e.g. -f doc.pdf -f pic.png")
    p.add_argument("-c", "--conversation"); p.add_argument("--new", action="store_true")
    p.add_argument("-o", "--output"); p.add_argument("--json", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--accounts", action="store_true")
    p.add_argument("--account", help="Account label")
    p.add_argument("--save-all", action="store_true")
    p.add_argument("--login", action="store_true",
                   help="Launch HEADED Chrome on DISPLAY=:0 to sign in once; saves a reusable profile.")
    p.add_argument("--set-default", help="Set default account")
    p.add_argument("--remove", help="Remove account")
    p.add_argument("--reset-limits", action="store_true")
    args = p.parse_args()
    if args.quiet: _Q = True
    
    if args.save_all: save_all_accounts(); return
    if args.reset_limits: (DIR / "rate_limits.json").unlink(missing_ok=True); print("Reset."); return
    if args.remove:
        accts = load_accounts()
        if args.remove in accts: del accts[args.remove]; save_accounts(accts); print(f"Removed '{args.remove}'")
        else: print(f"'{args.remove}' not found")
        return
    if args.set_default:
        accts = load_accounts()
        if args.set_default in accts:
            val = accts.pop(args.set_default); save_accounts({args.set_default: val, **accts})
            print(f"'{args.set_default}' is now default")
        else: print(f"'{args.set_default}' not found")
        return
    if args.accounts: list_accounts(); return
    
    if args.login:
        chrome_login_helper(); return
    
    prompt = args.prompt_flag or (" ".join(args.prompt) if args.prompt else None)
    if not prompt and not sys.stdin.isatty(): prompt = sys.stdin.read().strip()
    if not prompt: p.print_help(); sys.exit(1)
    
    # Pre-check: Kimi blocks financial queries — fail fast
    if _is_finance_query(prompt):
        fail("content-filter",
            "Kimi blocks financial/stock queries. Use fin-agent-cli for stock data, "
            "or Gemini/DeepSeek/MiniMax for financial analysis.")
    
    conv = load_conv(args.conversation) if args.conversation else {}
    if args.new: conv = {}
    conv_url = conv.get("url") if not args.new else None
    
    # Sync Chrome profile
    profile_path = sync_profile()
    if not profile_path:
        fail("no-auth", "Chrome profile not found. Log into kimi.com in Windows Chrome.")
    
    # Rate limit tracking
    rl_file = DIR / "rate_limits.json"
    cd = json.loads(rl_file.read_text()) if rl_file.exists() else {}
    
    result = None
    accts = load_accounts()
    if not accts:
        # No saved accounts — just use Chrome profile directly
        info("No saved accounts. Using Chrome profile. Run --save-all for multi-account.")
        try:
            text, url = kimi_chat(prompt, model=args.model, conv_url=conv_url, profile_path=profile_path, thinking=not args.no_thinking, files=args.file)
            result = text
            if args.conversation: conv["url"] = url
        except Exception as e:
            fail("error", str(e))
    else:
        labels = list(accts.keys())
        start = labels.index(args.account) if args.account and args.account in labels else 0
        
        for attempt in range(len(labels)):
            idx = (start + attempt) % len(labels); lbl = labels[idx]
            last = cd.get(lbl, 0)
            if time.time() - last < RATE_COOLDOWN:
                info(f"Skip '{lbl}' (cooldown {int(RATE_COOLDOWN - (time.time() - last))}s)"); continue
            info(f"Try: {lbl}")
            try:
                text, url = kimi_chat(prompt, model=args.model, conv_url=conv_url, profile_path=profile_path, thinking=not args.no_thinking, files=args.file)
                result = text
                if args.conversation: conv["url"] = url
                log("[KIMI:DONE]"); break
            except Exception as e:
                if "rate-limit" in str(e):
                    cd[lbl] = time.time()
                    rl_file.parent.mkdir(parents=True, exist_ok=True)
                    rl_file.write_text(json.dumps(cd))
                    info("Rate-limited → next"); continue
                raise
    
    if result is None: fail("rate-limit", "All accounts rate-limited.")
    if args.conversation: save_conv(args.conversation, conv)
    if args.output:
        op = Path(args.output); op.write_text(result, encoding="utf-8")
        print(json.dumps({"f": str(op), "s": op.stat().st_size, "b": result.count("```") // 2}, ensure_ascii=False))
    elif args.json:
        print(json.dumps({"ok": True, "text": result, "model": args.model}, ensure_ascii=False))
    else:
        print(result)

if __name__ == "__main__":
    try: main()
    except SystemExit: raise
    except Exception as e: fail("error", str(e))
