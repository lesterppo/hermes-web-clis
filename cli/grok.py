#!/usr/bin/env python3
"""
Grok CLI — Playwright browser automation for grok.com.
Auth: Firefox sso cookie. No API key needed.

Setup: auto-extracts sso cookie from Firefox profiles.
Usage:
  python grok.py "prompt"
  python grok.py -m heavy "deep reasoning prompt"
  python grok.py -o /tmp/out.md "prompt"
  python grok.py -c chat.json "multi-turn prompt"
  python grok.py --new -c chat.json "start fresh"
  python grok.py --brief "concise answer"
  python grok.py -l                     # Login flow (opens visible browser)

Models:
  fast    — Grok 4.3 Fast (default, quick responses)
  auto    — Auto-select Fast or Expert
  expert  — Grok 4.3 Expert (deeper responses)
  heavy   — Team of Experts (deep reasoning, thinking mode)

Agent-native output (use -o flag):
  {"ok":true,"f":"/tmp/out.md","s":450,"b":2,"m":"fast","t":22.3}
"""

import os, sys, json, time, argparse, sqlite3, shutil, re
from pathlib import Path

HOME = Path.home()
DIR = HOME / ".grok-cli"
AUTH_FILE = DIR / "auth.json"
CONV_DIR = DIR / "conversations"
BASE_URL = "https://grok.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Models
# fast = Grok 4.3 Fast (always available, free tier)
# auto, expert, heavy = require SuperGrok subscription
MODEL_FAST = "fast"
MODELS = [MODEL_FAST]
MODEL_LABELS = {MODEL_FAST: "Fast"}

_Q = False
_BRIEF = False

def fail(code, msg):
    print(json.dumps({"ok": False, "err": code, "msg": str(msg)[:500]}, ensure_ascii=False))
    sys.exit(1)

def log(msg):
    if not _Q and sys.stderr.isatty():
        print(f"[grok] {msg}", file=sys.stderr)

def extract_firefox_cookies():
    """Extract grok.com cookies from Windows Firefox profiles on WSL."""
    ff_base = Path("${WIN_HOME}/AppData/Roaming/Mozilla/Firefox/Profiles")
    if not ff_base.exists():
        return {}
    
    best = {}
    for profile in ff_base.iterdir():
        cookies_file = profile / "cookies.sqlite"
        if not cookies_file.exists():
            continue
        try:
            tmp = Path(f"/tmp/grok_ff_{profile.name}.sqlite")
            shutil.copy2(str(cookies_file), str(tmp))
            conn = sqlite3.connect(str(tmp))
            cur = conn.cursor()
            cur.execute(
                "SELECT name,value,host FROM moz_cookies "
                "WHERE host LIKE '%grok.com' AND name IN ('sso','sso-rw','cf_clearance','__cf_bm')"
            )
            rows = cur.fetchall()
            conn.close()
            tmp.unlink(missing_ok=True)
            
            if len(rows) > len(best):
                best = {}
                for name, value, host in rows:
                    best[name] = value.strip('"')
        except Exception:
            pass
    
    return best

def check_chrome_cookies():
    """Check Chrome cookies for grok.com (WSL, via browser_cookie3)."""
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome()
        cookies = {}
        for c in cj:
            if 'grok.com' in c.domain and c.name in ('sso', 'sso-rw'):
                cookies[c.name] = c.value
        return cookies
    except Exception:
        return {}

def load_auth():
    """Load auth from file or extract from browser."""
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text())
        except Exception:
            pass
    
    cookies = extract_firefox_cookies()
    if not cookies:
        cookies = check_chrome_cookies()
    
    if cookies:
        auth = {"cookies": cookies}
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTH_FILE.write_text(json.dumps(auth))
        return auth
    
    return {}

def setup_cookies(context, auth):
    """Inject cookies into Playwright context."""
    cookies = auth.get("cookies", {})
    for name, value in cookies.items():
        context.add_cookies([{
            "name": name, "value": value,
            "domain": ".grok.com", "path": "/",
            "httpOnly": name in ("sso", "sso-rw"),
            "secure": True, "sameSite": "None",
        }])

def dismiss_modals(page):
    """Dismiss any popups/modals."""
    for text in ["Close", "Accept", "Accept All", "Dismiss", "Got it", "Maybe later"]:
        try:
            btns = page.locator(f'button:has-text("{text}")')
            for i in range(btns.count()):
                btn = btns.nth(i)
                if btn.is_visible():
                    btn.click()
                    time.sleep(0.5)
        except Exception:
            pass

def switch_model(page, model):
    """Switch model via the model selector dropdown."""
    if model == MODEL_FAST:
        return  # Default
    
    label = MODEL_LABELS.get(model, model)
    
    try:
        # Click the model selector button
        model_btn = page.locator('button:has-text("Fast"), button:has-text("Auto"), '
                                  'button:has-text("Expert"), button:has-text("Heavy")').first
        if model_btn.count() > 0:
            current = model_btn.inner_text()
            if label in current:
                return  # Already on this model
            model_btn.click()
            time.sleep(1.5)
        
        # Find the dropdown menu item — use role selectors for precision
        # Grok uses role="menuitem" for model options
        option = page.locator(f'[role="menuitem"]:has-text("{label}")').first
        if option.count() > 0 and option.is_visible():
            option.click()
            time.sleep(2)
            log(f"Model: {label}")
        else:
            # Fallback: try plain text match scoped to the dropdown
            option = page.locator(f'[role="menu"] text="{label}"').first
            if option.count() > 0:
                option.click()
                time.sleep(2)
                log(f"Model: {label}")
            else:
                log(f"Model switch to {label} failed — continuing with current model")
    except Exception as e:
        log(f"Model switch: {e}")

def load_conv(conv_file):
    """Load conversation state."""
    if conv_file and Path(conv_file).exists():
        try:
            return json.loads(Path(conv_file).read_text())
        except Exception:
            pass
    return {}

def save_conv(conv_file, conv):
    """Save conversation state."""
    if conv_file:
        CONV_DIR.mkdir(parents=True, exist_ok=True)
        Path(conv_file).write_text(json.dumps(conv))

def extract_response(page):
    """Extract the last assistant response from the Grok chat page.
    
    Grok uses 'message-bubble' class divs — one per message.
    The last one is always the assistant's response.
    Falls back to full body text if bubbles not found.
    """
    try:
        # Direct extraction: message-bubble elements
        js_result = page.evaluate("""() => {
            const bubbles = document.querySelectorAll('[class*="message-bubble"]');
            // User messages are right-aligned (items-end), assistant are left (items-start)
            const UI_NOISE = new Set(['Toggle Sidebar', 'Toggle', 'Sidebar', 'New Chat',
                'Search', 'Ask Grok anything', 'What do you want to know?']);
            const real = [];
            for (const b of bubbles) {
                const text = b.textContent.trim();
                // Check parent alignment: user = items-end, assistant = items-start
                const parent = b.parentElement;
                const parentCls = parent?.className || '';
                const isUser = parentCls.includes('items-end');
                if (text.length > 10 && !isUser && !UI_NOISE.has(text)
                    && !text.startsWith('Fast') && !text.startsWith('Auto')
                    && !text.startsWith('Expert') && !text.startsWith('Heavy')) {
                    real.push(text);
                }
            }
            return real.length > 0 ? real[real.length - 1] : '';
        }""")
        
        text = ""
        if js_result and len(js_result.strip()) > 5:
            text = js_result.strip()
        else:
            # Fallback: get all body text and try to find the response
            body = page.locator("body").inner_text()
            if body:
                # Try to extract just the latest response
                # Look for pattern: user message followed by assistant response
                text = body
        
        if text:
            # Strip "Thought for Xs" thinking indicator prefix
            text = re.sub(r'^Thought for \d+s\s*', '', text)
            
            # Strip common UI chrome from end
            text = re.sub(r'\n+(Fast|Auto|Expert|Heavy|Ask Grok anything|'
                          r'What do you want to know\?|Search|New Chat|Imagine).*$',
                          '', text, flags=re.DOTALL)
            
            return text.strip()
        
        return ""
    except Exception as e:
        log(f"Extraction: {e}")
        return ""

def send_prompt(page, prompt, model=MODEL_FAST):
    """Send a prompt and wait for the complete response."""
    dismiss_modals(page)
    
    # Add brief prefix if requested
    if _BRIEF:
        prompt = "Be concise. " + prompt
    
    # Switch model
    switch_model(page, model)
    
    # Find input element (TipTap/ProseMirror contenteditable div)
    input_el = None
    is_tiptap = False
    
    for sel in ['[contenteditable="true"]', '[role="textbox"]', '.ProseMirror']:
        el = page.locator(sel).first
        if el.count() > 0:
            try:
                el.wait_for(state="visible", timeout=3000)
                input_el = el
                is_tiptap = True
                break
            except Exception:
                pass
    
    if input_el is None:
        for sel in ['textarea:visible', 'textarea:not([aria-hidden])']:
            el = page.locator(sel).first
            if el.count() > 0:
                try:
                    el.wait_for(state="visible", timeout=3000)
                    input_el = el
                    break
                except Exception:
                    pass
    
    if input_el is None:
        fail("no-input", "Cannot find input element on grok.com")
    
    # Type the prompt
    if is_tiptap:
        input_el.click(force=True)
        time.sleep(0.5)
        page.keyboard.press("Control+a")
        page.keyboard.press("Backspace")
        time.sleep(0.2)
        page.keyboard.type(prompt, delay=10)
    else:
        input_el.click()
        time.sleep(0.5)
        input_el.fill(prompt)
    
    time.sleep(0.5)
    
    # Submit
    input_el.press("Enter")
    time.sleep(1)
    
    # Fallback: click Submit button if Enter didn't work
    for btn_text in ["Submit", "Send", "→"]:
        btn = page.locator(f'button:has-text("{btn_text}")').first
        if btn.count() > 0:
            try:
                if btn.is_visible() and btn.is_enabled():
                    btn.click()
                    time.sleep(1)
                    break
            except Exception:
                pass
    
    # Wait for response completion
    # Strategy: wait for Stop button to disappear and Copy button to appear
    log("Waiting for response...")
    deadline = time.time() + 120
    started = time.time()
    
    while time.time() < deadline:
        stop_btns = page.locator('button:has-text("Stop"), button[aria-label="Stop"]')
        copy_btns = page.locator('button:has-text("Copy"), button[aria-label="Copy"]')
        
        stop_visible = False
        try:
            if stop_btns.count() > 0:
                stop_visible = stop_btns.first.is_visible()
        except Exception:
            pass
        
        copy_visible = False
        try:
            if copy_btns.count() > 0:
                copy_visible = copy_btns.first.is_visible()
        except Exception:
            pass
        
        if not stop_visible and copy_visible:
            time.sleep(2)  # Let final render settle
            break
        
        if not stop_visible and time.time() - started > 10:
            # Stop button gone for a while, likely done
            time.sleep(2)
            try:
                if stop_btns.count() == 0 or not stop_btns.first.is_visible():
                    break
            except Exception:
                break
        
        time.sleep(2)
    else:
        log("Timeout — extracting partial response...")
    
    elapsed = time.time() - started
    time.sleep(1)
    return extract_response(page), elapsed

def run_browser_login():
    """Open visible browser for one-time login."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail("no-playwright", "Playwright not installed.")

    log("Opening browser for login. Sign in to Grok, then close the browser.")
    
    profile_dir = DIR / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        
        log("Waiting for login... (timeout: 10 minutes)")
        
        for _ in range(600):
            signin = page.locator('button:has-text("Sign in")').count()
            if signin == 0:
                cookies = context.cookies()
                auth = {"cookies": {}}
                for c in cookies:
                    if 'grok.com' in c.get('domain', ''):
                        auth["cookies"][c['name']] = c['value']
                
                AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
                AUTH_FILE.write_text(json.dumps(auth))
                log("Login successful! Auth saved.")
                context.close()
                return True
            time.sleep(1)
        
        context.close()
        fail("login-timeout", "Login timed out after 10 minutes.")

def grok_chat(prompt, model=MODEL_FAST, conv_file=None, fresh=False):
    """Main chat function. Returns (response_text, elapsed_seconds)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail("no-playwright", "Playwright not installed.")
    
    auth = load_auth()
    if not auth or not auth.get("cookies", {}).get("sso"):
        fail("no-auth", "Not logged in. Run: python grok.py --login")
    
    conv = {} if fresh else load_conv(conv_file)
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=UA,
        )
        
        setup_cookies(context, auth)
        page = context.new_page()
        
        log("Loading grok.com...")
        
        if conv.get("url") and not fresh:
            page.goto(conv["url"], wait_until="domcontentloaded", timeout=30000)
        else:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        
        time.sleep(8)
        dismiss_modals(page)
        time.sleep(2)
        
        # Verify logged in
        deadline = time.time() + 15
        logged_in = False
        while time.time() < deadline:
            signin = page.locator('button:has-text("Sign in")').count()
            signup = page.locator('button:has-text("Sign up")').count()
            if signin == 0 and signup == 0:
                logged_in = True
                break
            time.sleep(2)
        
        if not logged_in:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(8)
            dismiss_modals(page)
            time.sleep(2)
            signin = page.locator('button:has-text("Sign in")').count()
            signup = page.locator('button:has-text("Sign up")').count()
            if signin > 0 or signup > 0:
                browser.close()
                fail("auth-expired", "Session expired. Run: python grok.py --login")
        
        dismiss_modals(page)
        
        # Send prompt
        log(f"Sending (model={model})...")
        response_text, elapsed = send_prompt(page, prompt, model)
        
        # Save conversation URL for multi-turn
        conv["url"] = page.url
        
        browser.close()
    
    save_conv(conv_file, conv)
    return response_text, elapsed

def main():
    global _Q, _BRIEF
    
    parser = argparse.ArgumentParser(description="Grok CLI — browser-cookie auth for grok.com")
    parser.add_argument("prompt", nargs="?", help="Prompt to send")
    parser.add_argument("-m", "--model", default=MODEL_FAST, choices=MODELS,
                        help=f"Model: {', '.join(MODELS)} (default: {MODEL_FAST})")
    parser.add_argument("-c", "--conv", help="Conversation state file for multi-turn")
    parser.add_argument("--new", action="store_true", help="Start fresh conversation")
    parser.add_argument("-o", "--output", help="Write response to file (stdout gets JSON pointer)")
    parser.add_argument("--json", action="store_true", help="JSON output on stdout")
    parser.add_argument("--brief", action="store_true", help="Concise/terse response mode")
    parser.add_argument("-l", "--login", action="store_true", help="Browser login flow")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress stderr")
    parser.add_argument("--save-auth", action="store_true", help="Re-extract cookies from browser")
    
    args = parser.parse_args()
    _Q = args.quiet
    _BRIEF = args.brief
    
    if args.login:
        run_browser_login()
        print(json.dumps({"ok": True, "msg": "Login successful"}))
        return
    
    if args.save_auth:
        cookies = extract_firefox_cookies()
        if not cookies:
            cookies = check_chrome_cookies()
        if cookies:
            auth = {"cookies": cookies}
            AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTH_FILE.write_text(json.dumps(auth))
            print(json.dumps({"ok": True, "msg": f"Saved {len(cookies)} cookies"}))
        else:
            fail("no-auth", "No Grok cookies found in Firefox or Chrome")
        return
    
    if not args.prompt:
        parser.print_help()
        sys.exit(1)
    
    try:
        response_text, elapsed = grok_chat(
            args.prompt, model=args.model,
            conv_file=args.conv, fresh=args.new,
        )
    except SystemExit:
        raise
    except Exception as e:
        fail("error", str(e))
    
    code_blocks = response_text.count("```") // 2
    
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(response_text)
        size = out_path.stat().st_size
        print(json.dumps({
            "ok": True,
            "f": str(out_path),
            "s": size,
            "b": code_blocks,
            "m": args.model,
            "t": round(elapsed, 1),
        }, ensure_ascii=False))
    elif args.json:
        print(json.dumps({
            "ok": True,
            "text": response_text,
            "s": len(response_text),
            "b": code_blocks,
            "m": args.model,
            "t": round(elapsed, 1),
        }, ensure_ascii=False))
    else:
        print(response_text)

if __name__ == "__main__":
    main()
