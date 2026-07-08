#!/usr/bin/env python3
"""
CLI for Kimi (kimi.com) via Playwright + Chrome cookies.
Token-efficient: saves accounts, auto-switches on rate limits.

Setup:
  python kimi.py --save-all           # One-time: extracts Chrome session
  python kimi.py --accounts           # List saved accounts

Usage:
  python kimi.py "prompt"
  python kimi.py -m kimi-k1.5 "prompt"
  python kimi.py --account label "prompt"
  python kimi.py -c chat.json "Turn 1" && python kimi.py -c chat.json "Turn 2"
"""

import os, sys, json, time, argparse, sqlite3, shutil, re
from pathlib import Path

HOME = Path.home()
DIR = HOME / ".kimi-cli"
ACCOUNTS_FILE = DIR / "accounts.json"
BASE = "https://www.kimi.com"
DEFAULT_MODEL = "kimi-k2"
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
    """Copy Chrome cookies only if newer. Returns profile path."""
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
    
    info("Syncing Chrome cookies...")
    for item in ["Default/Cookies", "Default/Cookies-journal", "Local State"]:
        sp = chrome_src / item; dp = profile_dir / item
        if sp.exists():
            dp.parent.mkdir(parents=True, exist_ok=True)
            try: shutil.copy2(str(sp), str(dp))
            except: pass
    
    mark.touch()
    return str(profile_dir)

# ── Kimi chat via Playwright ──────────────────────────────

# JS extraction — use DOM selectors instead of fragile body-text parsing
EXTRACT_JS = """
() => {
    // Kimi: assistant messages have specific class patterns
    // Try multiple selectors for the last assistant response
    const selectors = [
        '[class*="message"][class*="assistant"]',
        '[class*="chat"] [class*="assistant"]',
        '[data-role="assistant"]',
        '.chat-item.assistant',
        '[class*="bot"] [class*="content"]',
        '[class*="response"]',
    ];
    for (const sel of selectors) {
        const els = document.querySelectorAll(sel);
        if (els.length > 0) {
            const text = els[els.length - 1].innerText?.trim();
            if (text && text.length > 10) return text;
        }
    }
    return '';
}
"""

DONE_JS = """
() => {
    // Kimi: response is done when the send/stop button state changes.
    // Multiple detection strategies since Kimi UI changes frequently.
    const sendBtn = document.querySelector('button[aria-label*="send" i], button[type="submit"], [class*="send-btn"]');
    const stopBtn = document.querySelector('button[aria-label*="stop" i], [class*="stop-btn"], [class*="stop-generat"]');
    const loading = document.querySelector('[class*="loading"], [class*="thinking"], [class*="spinner"]');
    // Done = send/input available + nothing stopping + not loading
    if (!stopBtn && !loading) return true;
    // Also check: if we see a "Share" or "Copy" button, response is done
    const shareBtn = document.querySelector('[aria-label*="share" i], [class*="share"]');
    const copyBtn = document.querySelector('[aria-label*="copy" i], [class*="copy"]');
    if ((shareBtn || copyBtn) && !stopBtn) return true;
    return false;
}
"""


def extract_response_js(pg):
    """Extract assistant response using JS DOM selectors."""
    try:
        text = pg.evaluate(EXTRACT_JS)
        if text and len(text) > 10:
            return text
    except Exception:
        pass
    return ""


def extract_response(body):
    """Extract last AI response from Kimi page body.
    
    Strategy: walk backwards from "Share" marker. Chat history titles 
    are short (<30 chars). The actual response is longer text.
    """
    lines = body.split('\n')
    share_indices = [i for i, l in enumerate(lines) if l.strip() == 'Share']
    if not share_indices:
        return ''
    
    last_share = share_indices[-1]
    
    # Walk backwards, collect lines until we hit something that's clearly
    # not a chat history title (short text = sidebar, long text = response)
    result = []
    for i in range(last_share - 1, -1, -1):
        line = lines[i].strip()
        if not line or line == 'Copy':
            continue
        # Long lines are the actual response
        if len(line) > 40:
            result.insert(0, line)
        # Short lines that are NOT UI chrome might be response snippets
        elif len(line) > 3 and not any(line.startswith(p) for p in ('K2', 'K1', 'Ctrl')):
            # If we already have long lines, short ones before them are 
            # likely part of the response (e.g., code blocks, lists)
            if result:
                result.insert(0, line)
            # First short line we encounter — check if it's a chat title
            elif line not in ('New Chat', 'Chat History', 'All Chats', 'Get App',
                              'Upgrade', 'Lock Sidebar'):
                result.insert(0, line)
        # Stop if we hit obvious sidebar chrome
        if line in ('New Chat', 'Chat History', 'All Chats') and result:
            break
    
    return '\n'.join(result) if result else ''

def switch_model(pg, model):
    """Switch Kimi model via UI."""
    if model == DEFAULT_MODEL: return
    info(f"Switching model to {model}")
    try:
        pg.locator('[class*="model"]').first.click(); time.sleep(1)
        pg.locator(f'text="{model}"').first.click(); time.sleep(1)
    except: pass

def kimi_chat(prompt, model=DEFAULT_MODEL, conv_url=None, profile_path=None):
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
            pg.goto(conv_url, wait_until='domcontentloaded', timeout=30000)
            time.sleep(6)
        else:
            pg.goto(BASE, wait_until='domcontentloaded', timeout=30000)
            time.sleep(4)
            switch_model(pg, model)
        
        editor = pg.locator('[contenteditable="true"]').first
        if editor.count() == 0:
            raise Exception("no-input")
        
        # Kimi has an animated video background that intercepts pointer events.
        # Force-click through it, then type.
        editor.click(force=True); time.sleep(0.5)
        editor.fill(prompt); time.sleep(0.5)
        editor.press('Enter'); time.sleep(1)
        
        try:
            for sel in ['button[aria-label*="send" i]', 'button[type="submit"]']:
                btn = pg.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    btn.click(); time.sleep(0.5); break
        except: pass
        
        text = ""; deadline = time.time() + 180
        done_at = None
        while time.time() < deadline:
            try:
                done = pg.evaluate(DONE_JS)
            except Exception:
                done = False
            if done:
                if done_at is None:
                    done_at = time.time()
                text = extract_response_js(pg)
                # Wait at least 8s after DONE for Kimi's slow streaming
                if text and len(text) > 20 and (time.time() - done_at) >= 8:
                    break
            else:
                done_at = None
            time.sleep(0.5)
            # Hard timeout fallback: after 90s, extract whatever is there
            if time.time() > deadline - 90 and not text:
                text = extract_response_js(pg)
                if text and len(text) > 20:
                    break
        
        url = pg.url
        
        if not text: raise Exception("empty-response")
        return text, url
    finally:
        # Clean shutdown to avoid Node.js EPIPE crashes
        if ctx:
            try: ctx.close()
            except: pass
        try: pw.stop()
        except: pass

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
    p.add_argument("-m", "--model", default=DEFAULT_MODEL)
    p.add_argument("-c", "--conversation"); p.add_argument("--new", action="store_true")
    p.add_argument("-o", "--output"); p.add_argument("--json", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--accounts", action="store_true")
    p.add_argument("--account", help="Account label")
    p.add_argument("--save-all", action="store_true")
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
            text, url = kimi_chat(prompt, model=args.model, conv_url=conv_url, profile_path=profile_path)
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
                text, url = kimi_chat(prompt, model=args.model, conv_url=conv_url, profile_path=profile_path)
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
