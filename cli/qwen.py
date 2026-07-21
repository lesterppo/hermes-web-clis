#!/usr/bin/env python3
"""
CLI for Qwen Chat (chat.qwen.ai) via Playwright browser automation.

Features:
  - Text prompts, multi-turn conversations
  - Image upload for analysis (--image)
  - Image generation extraction (--extract-images)
  - Model selection (qwen3.7-plus, qwen3-max, qwen3-coder)
  - JS-based DOM extraction, progress markers, persistent browser

Qwen uses Alibaba WAF + JS/WASM request signing that blocks direct HTTP.
Instead, we launch headless Chromium, type into the chat UI, and extract
the response from the DOM. Auth via browser cookies (Firefox/Chrome/Edge).

Usage:
  python qwen.py "Hello"
  python qwen.py -m qwen3-max "Complex task"
  python qwen.py --image photo.jpg "Describe this image"
  python qwen.py --extract-images /tmp/imgs "Generate a cat picture"
  python qwen.py -c chat.json "Multi-turn message"
"""

import os
import sys
import json
import time
import argparse
import re
import textwrap
import base64
from datetime import datetime, timezone
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
QWEN_HOME = Path.home() / ".qwen-cli"
QWEN_AUTH_FILE = QWEN_HOME / "auth.json"
QWEN_BROWSER_PROFILE = QWEN_HOME / "browser-profile"
QWEN_BASE_URL = "https://chat.qwen.ai"
QWEN_DEFAULT_MODEL = "qwen3.7-plus"

# Page server integration
_QWEN_SERVER_PORT = 9873
_QWEN_PID_FILE = QWEN_HOME / "server.pid"

def _qwen_server_running() -> bool:
    if not _QWEN_PID_FILE.exists(): return False
    try:
        os.kill(int(_QWEN_PID_FILE.read_text().strip()), 0)
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{_QWEN_SERVER_PORT}/health", timeout=1)
        return True
    except: return False

def _try_qwen_server(prompt: str) -> dict | None:
    if not _qwen_server_running():
        from pathlib import Path
        server_script = Path(__file__).resolve().parent.parent / "scripts" / "page_server.py"
        if not server_script.exists():
            return None
        import subprocess
        log_path = Path.home() / ".chrome-daemon" / "qwen_server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [sys.executable, str(server_script), '--platform', 'qwen', '--port', str(_QWEN_SERVER_PORT), '--headed', '--start'],
            stdout=open(log_path, "a"), stderr=open(log_path, "a"),
            start_new_session=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if _qwen_server_running():
                break
            time.sleep(0.5)
        if not _qwen_server_running():
            return None
    try:
        import urllib.request
        data = json.dumps({"prompt": prompt}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{_QWEN_SERVER_PORT}/query",
                                      data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            if result.get("ok"): return result
    except: pass
    return None

# Financial terms that Qwen/Kimi block — fail fast instead of hanging
_FINANCE_KEYWORDS = [
    'stock price', 'share price', 'market cap', 'trading at', 'dividend yield',
    'earnings report', 'quarterly revenue', 'p/e ratio', 'balance sheet',
    'cash flow statement', 'income statement', 'ebitda', 'eps ', 'pe ratio',
    'nyse', 'nasdaq', 'ticker', 'etf price', 'index fund', 's&p 500',
    'dow jones', 'ftse', 'hang seng', 'nikkei', 'stock market',
]
_FINANCE_TICKER_RE = re.compile(r'\$[A-Z]{1,5}\b|\b[A-Z]{1,5}\s+(?:stock|share|ticker)\b', re.IGNORECASE)

def _is_finance_query(prompt: str) -> bool:
    """Detect if a prompt is a financial query that Qwen will block."""
    pl = prompt.lower()
    if any(kw in pl for kw in _FINANCE_KEYWORDS):
        return True
    if _FINANCE_TICKER_RE.search(prompt):
        return True
    return False

# ── helpers ──────────────────────────────────────────────

def fail(code: str, reason: str):
    print(json.dumps({"ok": False, "err": code, "msg": reason}, ensure_ascii=False))
    sys.exit(1)

def log(msg: str):
    print(msg, file=sys.stderr, flush=True)

def info(msg: str):
    if sys.stderr.isatty():
        print(f"[qwen] {msg}", file=sys.stderr)

# ── auth ─────────────────────────────────────────────────

def read_auth():
    if QWEN_AUTH_FILE.exists():
        try:
            return json.loads(QWEN_AUTH_FILE.read_text())
        except Exception:
            pass
    return None

def write_auth(data: dict):
    QWEN_HOME.mkdir(parents=True, exist_ok=True)
    QWEN_AUTH_FILE.write_text(json.dumps(data, indent=2))


def extract_cookies_from_browser():
    try:
        import browser_cookie3
        browsers = [
            ("chrome", browser_cookie3.chrome),
            ("firefox", browser_cookie3.firefox),
            ("edge", browser_cookie3.edge),
        ]
        for name, fetch_func in browsers:
            try:
                cj = fetch_func(domain_name=".qwen.ai")
                token = lswusea = cnaui = None
                for c in cj:
                    if c.name == "token":
                        token = c.value
                    elif c.name == "lswusea":
                        lswusea = c.value
                    elif c.name == "cnaui":
                        cnaui = c.value
                if token:
                    info(f"Cookies extracted from {name}")
                    return {"token": token, "lswusea": lswusea, "cnaui": cnaui}
            except Exception:
                continue
    except ImportError:
        pass

    if sys.platform == "linux":
        result = _extract_firefox_wsl()
        if result and result.get("token"):
            return result

    return None


def _extract_firefox_wsl():
    import sqlite3
    import shutil
    firefox_base = Path("/mnt/c/Users")
    profiles = []
    try:
        for user_dir in firefox_base.iterdir():
            if not user_dir.is_dir():
                continue
            ff_profiles = user_dir / "AppData/Roaming/Mozilla/Firefox/Profiles"
            if ff_profiles.exists():
                for p in ff_profiles.iterdir():
                    if p.is_dir() and (p / "cookies.sqlite").exists():
                        profiles.append(p / "cookies.sqlite")
    except PermissionError:
        pass
    for sqlite_path in profiles:
        try:
            tmp = Path("/tmp/qwen_cookies.sqlite")
            shutil.copy2(str(sqlite_path), str(tmp))
            conn = sqlite3.connect(str(tmp))
            cur = conn.cursor()
            cur.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE host LIKE '%qwen.ai%' AND (name='token' OR name='lswusea' OR name='cnaui')"
            )
            rows = cur.fetchall()
            conn.close()
            tmp.unlink(missing_ok=True)
            result = {}
            for name, value in rows:
                result[name] = value
            if result.get("token"):
                info(f"Cookies extracted from Firefox (WSL, {sqlite_path.parent.name})")
                return result
        except Exception:
            continue
    return None


def import_cookies_from_json(json_path: str):
    p = Path(json_path)
    if not p.exists():
        fail("no-file", f"File not found: {json_path}")
    try:
        cookies = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        fail("bad-json", f"Invalid JSON: {e}")
    if not isinstance(cookies, list):
        fail("bad-format", f"Expected array of cookies, got {type(cookies).__name__}")
    qwen_cookies = [c for c in cookies if isinstance(c, dict) and "qwen.ai" in str(c.get("domain", ""))]
    if not qwen_cookies:
        fail("no-qwen-cookies", "No qwen.ai cookies found in file")
    token = lswusea = cnaui = None
    for c in qwen_cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name == "token": token = value
        elif name == "lswusea": lswusea = value
        elif name == "cnaui": cnaui = value
    if not token:
        fail("no-token", "No 'token' cookie found")
    if not re.match(r'^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$', token):
        info("Warning: token doesn't look like a JWT")
    cookie_parts = [f"token={token}"]
    if lswusea: cookie_parts.append(f"lswusea={lswusea}")
    if cnaui: cookie_parts.append(f"cnaui={cnaui}")
    auth_data = {
        "token": token, "cookie_header": "; ".join(cookie_parts),
        "lswusea": lswusea, "cnaui": cnaui,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    write_auth(auth_data)
    info(f"Imported cookies: token present")
    print(json.dumps({"ok": True, "msg": "Cookies imported"}, ensure_ascii=False))


def browser_login():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail("no-playwright", "pip install playwright && python -m playwright install chromium")
    info("Launching browser for Qwen login...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(QWEN_BROWSER_PROFILE), headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--no-sandbox", "--disable-gpu", "--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://chat.qwen.ai/", wait_until="commit")
        info("Waiting for login...")
        for i in range(300):
            cookies = context.cookies()
            token_cookie = lswusea_cookie = cnaui_cookie = None
            for c in cookies:
                if c["name"] == "token": token_cookie = c["value"]
                elif c["name"] == "lswusea": lswusea_cookie = c["value"]
                elif c["name"] == "cnaui": cnaui_cookie = c["value"]
            if token_cookie:
                cookie_parts = [f"token={token_cookie}"]
                if lswusea_cookie: cookie_parts.append(f"lswusea={lswusea_cookie}")
                if cnaui_cookie: cookie_parts.append(f"cnaui={cnaui_cookie}")
                auth_data = {
                    "token": token_cookie, "cookie_header": "; ".join(cookie_parts),
                    "lswusea": lswusea_cookie, "cnaui": cnaui_cookie,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }
                write_auth(auth_data)
                info("Login successful!")
                context.close()
                return auth_data
            if i % 30 == 0 and i > 0:
                info(f"Still waiting... ({i}s)")
            time.sleep(1)
        context.close()
        fail("login-timeout", "Login not detected within 5 minutes.")


def get_auth():
    token = os.environ.get("QWEN_TOKEN")
    cookie_header = os.environ.get("QWEN_COOKIE_HEADER")
    if token and cookie_header:
        return {"token": token, "cookie_header": cookie_header}
    if token:
        ch = f"token={token}"
        lsw = os.environ.get("QWEN_LSWUSEA")
        if lsw: ch += f"; lswusea={lsw}"
        return {"token": token, "cookie_header": ch}
    auth = read_auth()
    if auth and auth.get("token") and auth.get("cookie_header"):
        return auth
    # New format: cookies dict with optional cookies_by_domain
    if auth and auth.get("cookies"):
        return auth
    browser_auth = extract_cookies_from_browser()
    if browser_auth and browser_auth.get("token"):
        cookie_parts = [f"token={browser_auth['token']}"]
        if browser_auth.get("lswusea"): cookie_parts.append(f"lswusea={browser_auth['lswusea']}")
        if browser_auth.get("cnaui"): cookie_parts.append(f"cnaui={browser_auth['cnaui']}")
        result = {"token": browser_auth["token"], "cookie_header": "; ".join(cookie_parts)}
        write_auth(result)
        return result
    fail("no-auth", "No Qwen auth found. Log into https://chat.qwen.ai in Windows Firefox first.")


# ── conversation state ───────────────────────────────────

def load_conversation(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}

def save_conversation(path: str, state: dict):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── JS-based extraction ──────────────────────────────────

EXTRACT_JS = """
() => {
    // Qwen's current DOM (June 2026):
    // - .qwen-chat-message-assistant = assistant message wrapper
    // - .chat-response-message = response content (inside wrapper)
    // Try these first, then fall back to body-text parsing.
    const selectors = [
        '.chat-response-message',
        '.qwen-chat-message-assistant',
        '[class*="assistant"] [class*="content"]',
        '[class*="message"][class*="bot"] [class*="text"]',
    ];
    for (const sel of selectors) {
        const els = document.querySelectorAll(sel);
        if (els.length > 0) {
            // Get the LAST element (most recent response)
            const el = els[els.length - 1];
            const text = el.innerText?.trim() || '';
            if (text && text.length > 5 && text !== 'New Chat') {
                return text
                    .replace(/^Thinking completed\\n*/g, '')
                    .replace(/^Thinking\\.\\.\\.?\\n*/g, '')
                    .replace(/^深度思考完成\\n*/g, '')
                    .replace(/^已完成思考\\n*/g, '')
                    .trim();
            }
        }
    }
    // Fallback: parse full body text after Qwen's completion markers
    const allText = document.body.innerText;
    for (const marker of ['Thinking completed', '已完成思考', '深度思考完成']) {
        const idx = allText.indexOf(marker);
        if (idx >= 0) {
            const after = allText.slice(idx + marker.length).trim();
            const lines = after.split('\\n').filter(l => {
                const s = l.trim();
                if (!s) return false;
                if (['Auto', 'Skip', 'AI-generated', 'AI generated content', 'New Chat', 'New chat'].includes(s)) return false;
                if (s.startsWith('Qwen') && s.length < 20) return false;
                return true;
            });
            const result = lines.join('\\n').trim();
            if (result && result.length > 10) return result;
        }
    }
    return '';
}
"""

EXTRACT_IMAGES_JS = """
() => {
    // Find all images in the last assistant message
    const selectors = [
        '[class*="assistant"] img',
        '[class*="message"][class*="bot"] img',
        '[data-role="assistant"] img',
    ];
    for (const sel of selectors) {
        const imgs = document.querySelectorAll(sel);
        if (imgs.length > 0) {
            return Array.from(imgs).map(img => ({
                src: img.src,
                alt: img.alt || '',
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0,
            })).filter(i => i.src && !i.src.includes('favicon') && !i.src.includes('logo'));
        }
    }
    // Fallback: all large images in the page that appeared recently
    const allImgs = document.querySelectorAll('img');
    const results = [];
    for (const img of allImgs) {
        const w = img.naturalWidth || img.width || 0;
        const h = img.naturalHeight || img.height || 0;
        if (w > 100 && h > 100 && img.src && !img.src.includes('favicon') && !img.src.includes('logo')) {
            // Check if this img is inside or near a chat message
            let parent = img.parentElement;
            let inChat = false;
            for (let i = 0; i < 10; i++) {
                if (!parent) break;
                const cls = parent.className || '';
                if (cls.includes('message') || cls.includes('chat') || cls.includes('assistant') || cls.includes('bot') || cls.includes('bubble') || cls.includes('content')) {
                    inChat = true;
                    break;
                }
                parent = parent.parentElement;
            }
            if (inChat) {
                results.push({src: img.src, alt: img.alt || '', width: w, height: h});
            }
        }
    }
    return results;
}
"""

DONE_JS = """
() => {
    const body = document.body.innerText;
    // Thinking mode markers
    if (body.includes('Thinking completed')) return true;
    if (body.includes('已完成思考')) return true;
    if (body.includes('深度思考完成')) return true;
    // No-thinking mode: check for assistant response with content
    const assistantMsgs = document.querySelectorAll('[class*="assistant"], [class*="message"][class*="bot"], .qwen-chat-message-assistant');
    for (const el of assistantMsgs) {
        const text = el.innerText || '';
        if (text.length > 10 && !text.includes('Thinking') && !text.includes('思考')) return true;
    }
    // Check response containers
    const responseEls = document.querySelectorAll('.chat-response-message, [class*="response"][class*="content"]');
    for (const el of responseEls) {
        if ((el.innerText || '').trim().length > 5) return true;
    }
    // Copy button means response complete
    if (document.querySelector('button[aria-label*="Copy"], button[aria-label*="copy"], [class*="copy"]')) return true;
    // Image generation: check if images appeared in assistant message
    const imgs = document.querySelectorAll('[class*="assistant"] img, [class*="message"][class*="bot"] img');
    for (const img of imgs) {
        if (img.naturalWidth > 100 && img.naturalHeight > 100) return true;
    }
    return false;
}
"""

ERROR_JS = """
() => {
    const body = document.body.innerText;
    if (body.includes('Something went wrong') || body.includes('try again')) return 'error';
    if (body.includes('Login') && body.includes('expired')) return 'auth-expired';
    return null;
}
"""


def extract_response(page, prompt: str, debug: bool = False) -> tuple[str, str | None, list]:
    """Poll for response completion. Returns (text, chat_id, images)."""
    response_text = ""
    chat_id = None
    images = []
    deadline = time.time() + 300  # 5 min (image gen can be slow)
    thinking_seen = False

    while time.time() < deadline:
        if not chat_id:
            m = re.search(r'/c/([a-f0-9-]{20,})', page.url)
            if m:
                chat_id = m.group(1)
                if debug:
                    info(f"Chat ID: {chat_id}")

        # Error check
        try:
            err = page.evaluate(ERROR_JS)
            if err == "auth-expired":
                fail("auth-expired", "Qwen login expired.")
            elif err == "error":
                fail("qwen-error", "Qwen returned an error.")
        except Exception:
            pass

        # Check thinking
        try:
            done = page.evaluate(DONE_JS)
        except Exception:
            done = False

        if done and not thinking_seen:
            thinking_seen = True
            log("[QWEN:THINKING]")

        if done:
            try:
                text = page.evaluate(EXTRACT_JS)
            except Exception:
                text = ""
                body = page.locator("body").inner_text()
                if prompt in body:
                    after = body[body.find(prompt) + len(prompt):]
                    if "Thinking completed" in after:
                        final = after.split("Thinking completed", 1)[1].strip()
                        lines = final.split("\n")
                        answer_lines = []
                        for line in lines:
                            s = line.strip()
                            if not s: continue
                            if s in ("Auto", "Skip", "New Chat", "New chat") or s.startswith("Qwen") or s.startswith("AI-generated"):
                                continue
                            answer_lines.append(s)
                        text = "\n".join(answer_lines).strip()

            # Also extract any images generated in the response
            try:
                extracted = page.evaluate(EXTRACT_IMAGES_JS)
                if extracted:
                    images = extracted
            except Exception:
                pass

            if text and len(text) > 1:
                for prefix in ["Qwen3.7-Plus", "Qwen3-Max", "Qwen3-Coder"]:
                    if text.startswith(prefix):
                        text = text[len(prefix):].strip()
                if text:
                    response_text = text
                    break

        time.sleep(0.3)

    return response_text, chat_id, images

def download_images(page, images: list, output_dir: Path) -> list[str]:
    """Download images from URLs using the page's cookies/context. Returns list of local paths."""
    saved = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for i, img in enumerate(images):
        src = img.get("src", "")
        if not src:
            continue
        try:
            # Use fetch in the browser context to get the image (bypasses CORS)
            img_data_js = """
            async (url) => {
                try {
                    const resp = await fetch(url);
                    if (!resp.ok) return null;
                    const blob = await resp.blob();
                    return new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve({
                            mime: blob.type,
                            data: reader.result.split(',')[1]
                        });
                        reader.readAsDataURL(blob);
                    });
                } catch(e) { return null; }
            }
            """
            result = page.evaluate(img_data_js, src)
            if result and result.get("data"):
                ext = result.get("mime", "image/png").split("/")[-1]
                if ext == "jpeg": ext = "jpg"
                local_path = output_dir / f"qwen_img_{i+1}.{ext}"
                local_path.write_bytes(base64.b64decode(result["data"]))
                saved.append(str(local_path))
                log(f"[QWEN:IMG] {local_path.name}")
        except Exception as e:
            info(f"Failed to download image {i}: {e}")

    return saved


# ── browser helpers ───────────────────────────────────────

def setup_cookies(context, auth: dict):
    # Use per-cookie domains if available (from cookies_by_domain)
    cookies_by_domain = auth.get("cookies_by_domain", {})
    if cookies_by_domain:
        for domain, cks in cookies_by_domain.items():
            for name, value in cks.items():
                context.add_cookies([{
                    "name": name, "value": value,
                    "domain": domain, "path": "/",
                    "httpOnly": name == "token", "secure": True, "sameSite": "Lax",
                }])
    else:
        # Fallback: cookie_header string
        cookies_to_set = []
        for cookie_str in auth.get("cookie_header", "").split("; "):
            if "=" in cookie_str:
                name, _, value = cookie_str.partition("=")
                cookies_to_set.append({
                    "name": name, "value": value,
                    "domain": ".qwen.ai", "path": "/",
                    "httpOnly": name == "token", "secure": True, "sameSite": "Lax",
                })
        context.add_cookies(cookies_to_set)


def switch_model(page, model: str):
    """Click the model selector dropdown and choose the specified model."""
    if model == QWEN_DEFAULT_MODEL:
        return

    log(f"[QWEN:MODEL] {model}")

    model_selectors = [
        '[class*="model-selector"]',
        '[class*="model"] button',
        'button:has-text("Qwen")',
        '.model-switch',
    ]
    for sel in model_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                time.sleep(1)
                break
        except Exception:
            continue

    model_option_selectors = [
        f'[role="option"]:has-text("{model}")',
        f'li:has-text("{model}")',
        f'div:has-text("{model}")',
    ]
    for sel in model_option_selectors:
        try:
            opt = page.locator(sel).first
            if opt.count() > 0 and opt.is_visible():
                opt.click()
                time.sleep(1)
                info(f"Switched model to {model}")
                return
        except Exception:
            continue

    info(f"Could not find {model} in dropdown — using default model")


def upload_image_to_page(page, image_path: str):
    img_path = Path(image_path)
    if not img_path.exists():
        fail("no-image", f"Image not found: {image_path}")

    log("[QWEN:UPLOAD]")

    # Qwen uses a hidden file input #filesUpload triggered by an upload button.
    # Set files directly on the hidden input (Playwright supports this).
    fi = page.locator("#filesUpload")
    if fi.count() == 0:
        # Fallback: try generic file input
        fi = page.locator('input[type="file"]').first

    if fi.count() > 0:
        fi.set_input_files(str(img_path))
        time.sleep(2)  # wait for upload to complete
        return

    # Last resort: try file chooser dialog via clicking upload triggers
    trigger_selectors = [
        'button[aria-label*="upload" i]',
        'button[aria-label*="attach" i]',
        '[class*="upload"] button',
        '[class*="attach"] button',
        'label[for="filesUpload"]',
    ]
    for sel in trigger_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                with page.expect_file_chooser() as fc_info:
                    btn.click()
                fc_info.value.set_files(str(img_path))
                time.sleep(2)
                return
        except Exception:
            continue

    fail("no-upload", "Could not find image upload element on Qwen page.")


def send_prompt(page, prompt: str, chat_id: str | None = None,
                model: str = QWEN_DEFAULT_MODEL,
                image: str = "", extract_images_dir: str = "",
                debug: bool = False):
    """Navigate to Qwen, type prompt, optionally upload image, wait for response."""
    log("[QWEN:LOADING]")

    # Navigate
    if chat_id:
        page.goto(f"{QWEN_BASE_URL}/c/{chat_id}", wait_until="commit", timeout=30000)
    else:
        page.goto(QWEN_BASE_URL, wait_until="commit", timeout=30000)
    
    # Wait for model to finish loading (Model loading... in sidebar)
    for _ in range(15):
        body = page.locator("body").inner_text()
        if "Model loading" not in body:
            break
        time.sleep(1)
    
    # Smart wait for textarea instead of fixed sleep
    try:
        page.wait_for_selector("textarea", timeout=8000)
    except Exception:
        time.sleep(2)

    # Switch model if not default
    if model != QWEN_DEFAULT_MODEL:
        switch_model(page, model)

    # Upload image if provided
    if image:
        upload_image_to_page(page, image)

    # Find textarea
    textbox = page.locator("textarea").first
    if not textbox.is_visible(timeout=5000):
        fail("no-input", "Could not find chat input. Auth may have expired.")

    if debug:
        info(f"Sending prompt ({len(prompt)} chars)")

    textbox.fill(prompt)
    time.sleep(0.3)
    textbox.press("Enter")

    if debug:
        info("Waiting for response...")

    response_text, new_chat_id, images = extract_response(page, prompt, debug=debug)

    # Download generated images if requested
    saved_images = []
    if extract_images_dir and images:
        saved_images = download_images(page, images, Path(extract_images_dir))

    return response_text, new_chat_id, saved_images


# ── main CLI ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CLI for Qwen Chat (chat.qwen.ai) via browser cookie auth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          python qwen.py "Hello"
          python qwen.py -m qwen3-max "Complex task"
          python qwen.py --image photo.jpg "Describe this image"
          python qwen.py --extract-images /tmp/imgs "Generate a cat"
          python qwen.py -c chat.json "Multi-turn message"
        """),
    )
    parser.add_argument("prompt", nargs="*", help="Prompt text (or pipe via stdin)")
    parser.add_argument("-p", "--prompt-flag", help="Prompt via flag")
    parser.add_argument("-m", "--model", default=QWEN_DEFAULT_MODEL,
                        help=f"Model name (default: {QWEN_DEFAULT_MODEL})")
    parser.add_argument("-c", "--conversation", help="Conversation state file")
    parser.add_argument("--new", action="store_true", help="Start fresh conversation")
    parser.add_argument("-o", "--output", help="Write response to file")
    parser.add_argument("--json", action="store_true", help="JSON output on stdout")
    parser.add_argument("-l", "--login", action="store_true", help="Browser login flow")
    parser.add_argument("--import-cookies", metavar="FILE", help="Import cookies from JSON")
    parser.add_argument("--no-thinking", action="store_true", help="Disable thinking")
    parser.add_argument("--no-search", action="store_true", help="Disable web search")
    parser.add_argument("--image", metavar="FILE", help="Image file to upload for analysis")
    parser.add_argument("--extract-images", metavar="DIR",
                        help="Download images generated by Qwen to this directory")
    parser.add_argument("--persist", action="store_true", help="Use persistent browser profile")
    parser.add_argument("--debug", action="store_true", help="Debug output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress stderr logs")

    args = parser.parse_args()

    if args.login:
        browser_login()
        print(json.dumps({"ok": True, "msg": "Login saved"}, ensure_ascii=False))
        return

    if args.import_cookies:
        import_cookies_from_json(args.import_cookies)
        return

    # Resolve prompt
    prompt = None
    if args.prompt_flag:
        prompt = args.prompt_flag
    elif args.prompt:
        prompt = " ".join(args.prompt)
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if not prompt:
        parser.print_help()
        sys.exit(1)

    # Pre-check: Qwen blocks financial queries — fail fast
    if _is_finance_query(prompt):
        fail("content-filter",
            "Qwen blocks financial/stock queries. Use fin-agent-cli for stock data, "
            "or Gemini/DeepSeek/MiniMax for financial analysis.")

    # Server path disabled — unreliable with persistent context.
    # Always use direct Playwright for reliability.
    model = args.model
    conv = {}
    chat_id = None
    if args.conversation:
        conv = load_conversation(args.conversation)
        if args.new:
            conv = {}
    chat_id = conv.get("chat_id")
    if conv.get("model"):
        model = conv["model"]

    auth = get_auth()

    browser = context = page = None
    pw_instance = None
    try:
        from playwright.sync_api import sync_playwright

        pw_instance = sync_playwright().start()
        try:
            if args.persist:
                profile_dir = str(QWEN_BROWSER_PROFILE)
                QWEN_HOME.mkdir(parents=True, exist_ok=True)
                context = pw_instance.chromium.launch_persistent_context(
                    profile_dir, headless=True,
                    viewport={"width": 1280, "height": 800},
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
            else:
                browser = pw_instance.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                context = browser.new_context(viewport={"width": 1280, "height": 800})

            setup_cookies(context, auth)
            page = context.pages[0] if context.pages else context.new_page()

            response_text, new_chat_id, saved_images = send_prompt(
                page, prompt, chat_id=chat_id,
                model=model,
                image=args.image,
                extract_images_dir=args.extract_images,
                debug=args.debug,
            )
        finally:
            # Clean shutdown to avoid Node.js EPIPE crashes
            if page:
                try: page.close()
                except: pass
            if context:
                try: context.close()
                except: pass
            if browser:
                try: browser.close()
                except: pass
            if pw_instance:
                try: pw_instance.stop()
                except: pass

        if not response_text:
            fail("empty-response", "No response received. Auth may have expired.")

        log("[QWEN:DONE]")

        if args.conversation:
            conv["chat_id"] = new_chat_id or chat_id
            conv["model"] = model
            save_conversation(args.conversation, conv)

        # Output
        if args.output:
            out_path = Path(args.output)
            out_path.write_text(response_text, encoding="utf-8")
            size = out_path.stat().st_size
            code_blocks = response_text.count("```")
            result = {
                "ok": True,
                "f": str(out_path),
                "s": size,
                "b": code_blocks // 2,
            }
            if args.conversation:
                result["c"] = new_chat_id or chat_id
            if saved_images:
                result["img"] = saved_images
            print(json.dumps(result, ensure_ascii=False))
        elif args.json:
            out = {
                "ok": True,
                "text": response_text,
                "chat_id": new_chat_id or chat_id,
                "model": model,
            }
            if saved_images:
                out["images"] = saved_images
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(response_text)

    except SystemExit:
        raise
    except Exception as e:
        fail("error", str(e))


if __name__ == "__main__":
    main()
