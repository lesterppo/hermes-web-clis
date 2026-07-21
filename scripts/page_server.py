#!/usr/bin/env python3
"""
Generic Page Server — persistent Chromium per AI web platform.
CLIs talk HTTP to this server instead of launching Playwright per call.

Usage:
  python page_server.py --platform chatgpt --port 9870 [--headed]
  python page_server.py --platform qwen --port 9873 [--headed]
  python page_server.py --platform minimax --port 9871 [--headed]
  python page_server.py --platform mimo --port 9872 [--headed]

HTTP API:
  GET  /health              → {"ok": true, "uptime": N}
  POST /query               → {"ok": true, "text": "..."}  (body: {"prompt": "..."})
  POST /query/new           → start fresh conversation then query
  GET  /status              → {"ok": true, "logged_in": true, "model": "..."}

Architecture:
  - Daemonizes (double-fork) to survive terminal disconnection
  - One persistent Chromium per server, keeps auth session alive
  - Bypasses Cloudflare/WAF by using headed Chromium with anti-detection flags
  - Platform-specific drivers handle DOM interaction
"""

import os, sys, json, time, signal, argparse, threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Platform Drivers ──────────────────────────────────────

class BaseDriver:
    """Base class for platform-specific DOM interaction."""
    name = "base"
    url = ""
    port = 9870
    profile_dir = None
    
    def __init__(self, profile_dir, headless=False):
        self.profile_dir = profile_dir
        self.headless = headless
        self.page = None
        self.context = None
        self.pw = None
        self._start_time = time.time()
    
    def uptime(self):
        return int(time.time() - self._start_time)
    
    def launch(self):
        """Launch Chromium and navigate to platform."""
        from playwright.sync_api import sync_playwright
        # Auto-clean stale SingletonLock from previous crashed instances
        for lock in ['SingletonLock', 'SingletonCookie', 'SingletonSocket']:
            lp = self.profile_dir / lock
            if lp.exists():
                try: lp.unlink()
                except: pass
        self.pw = sync_playwright().start()
        self.context = self.pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            viewport={'width': 1280, 'height': 800},
            args=[
                '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ],
            ignore_default_args=['--enable-automation'],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(self.url, wait_until='commit', timeout=30000)
        # Wait for the page to be fully interactive
        return self._wait_ready()
    
    def _wait_ready(self, timeout=30):
        """Wait for chat UI to be ready after navigation."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                body = self.page.locator('body').inner_text()
                if len(body) > 50 and 'Just a moment' not in self.page.title():
                    # Check for chat input
                    if self._find_input():
                        return True
            except: pass
            time.sleep(1)
        return len(self.page.locator('body').inner_text()) > 10
    
    def _find_input(self):
        """Try to find the chat input. Returns True if found."""
        for sel in self._input_selectors():
            try:
                el = self.page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=2000):
                    return True
            except: pass
        return False
    
    def _input_selectors(self):
        """Override in subclasses."""
        return ['textarea', '[contenteditable="true"]', '[role="textbox"]']
    
    def is_logged_in(self):
        """Check if the session is still authenticated."""
        try:
            body = self.page.locator('body').inner_text()
            for phrase in self._logout_phrases():
                if phrase.lower() in body.lower():
                    return False
            return True
        except:
            return False
    
    def _logout_phrases(self):
        """Phrases that indicate the user is logged out."""
        return ['sign in', 'log in', 'sign up', 'create account']
    
    def send_prompt(self, prompt, new_conv=False):
        """Send a prompt and return the response text."""
        raise NotImplementedError
    
    def get_model(self):
        """Return the currently active model name."""
        return "unknown"
    
    def close(self):
        """EPIPE-safe teardown."""
        _err_fd = None
        try:
            _err_fd = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2)
            os.close(devnull)
        except: pass
        try:
            if self.context: self.context.close()
        except: pass
        try:
            if self.pw: self.pw.stop()
        except: pass
        if _err_fd is not None:
            try:
                os.dup2(_err_fd, 2); os.close(_err_fd)
            except: pass
    
    def ensure_alive(self):
        """Check if the browser is still alive, restart if needed."""
        try:
            self.page.title()
            return True
        except:
            try:
                self.close()
                self.launch()
                return True
            except:
                return False


class ChatGPTDriver(BaseDriver):
    """ChatGPT (chatgpt.com) — Cloudflare Turnstile bypass via headed Chromium."""
    name = "chatgpt"
    url = "https://chatgpt.com"
    
    def _input_selectors(self):
        return ['#prompt-textarea', 'textarea[placeholder*="Ask"]', 
                'textarea[placeholder*="Message"]', '[contenteditable="true"]']
    
    def _logout_phrases(self):
        return ['log in', 'sign up', 'get started']
    
    def _wait_ready(self, timeout=45):
        """ChatGPT needs extra time for Cloudflare Turnstile + textarea render."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                title = self.page.title()
                if 'Just a moment' in title:
                    time.sleep(2)
                    continue
                # ChatGPT's textarea is .wcDTda_fallbackTextarea with placeholder="Ask anything"
                body = self.page.locator('body').inner_text()
                if len(body) > 200:
                    ta = self.page.locator('textarea').first
                    if ta.count() > 0 and ta.is_visible(timeout=2000):
                        return True
            except: pass
            time.sleep(1)
        try:
            return len(self.page.locator('body').inner_text()) > 100
        except:
            return False
    
    def send_prompt(self, prompt, new_conv=False):
        pg = self.page
        
        # Check for Cloudflare
        title = pg.title()
        if 'Just a moment' in title:
            deadline = time.time() + 30
            while time.time() < deadline:
                time.sleep(1)
                title = pg.title()
                if 'Just a moment' not in title:
                    break
            if 'Just a moment' in pg.title():
                return {"ok": False, "err": "cloudflare", "msg": "Cloudflare Turnstile stuck"}
        
        # ChatGPT: textarea often fails Playwright visibility check.
        # Use mouse click + keyboard — proven to work in headed mode.
        pg.keyboard.press('Escape')
        time.sleep(0.5)
        pg.mouse.click(640, 600)
        time.sleep(2)
        pg.keyboard.type(prompt, delay=30)
        time.sleep(0.5)
        pg.keyboard.press('Enter')
        
        # Wait for response
        time.sleep(2)
        deadline = time.time() + 120
        pre_len = len(pg.locator('body').inner_text())
        last_len = pre_len
        stable = 0
        
        while time.time() < deadline:
            body = pg.locator('body').inner_text()
            cur_len = len(body)
            if cur_len > last_len:
                last_len = cur_len
                stable = 0
            else:
                stable += 1
            
            if cur_len > pre_len + 50 and stable >= 5:
                # Extract response after the prompt
                idx = body.find(prompt[:30])
                if idx >= 0:
                    after = body[idx + min(len(prompt), 30):]
                    lines = []
                    skip = True
                    for line in after.split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        if skip and len(line) > 3:
                            skip = False
                        if not skip:
                            if line in ('Regenerate', 'Copy', 'Read aloud', 'ChatGPT'):
                                break
                            if line.startswith('4o') or line.startswith('o1') or line.startswith('o3'):
                                break
                            if line.startswith('Search') or line.startswith('Deep research'):
                                break
                            lines.append(line)
                    text = '\n'.join(lines).strip()
                    if text:
                        return {"ok": True, "text": text, "model": self.get_model()}
            time.sleep(0.5)
        
        return {"ok": False, "err": "timeout", "msg": "Response timeout"}


class QwenDriver(BaseDriver):
    """Qwen (chat.qwen.ai) — Alibaba WAF bypass via headed Chromium."""
    name = "qwen"
    url = "https://chat.qwen.ai"
    
    def _logout_phrases(self):
        return ['sign in', 'log in', '请登录']
    
    def send_prompt(self, prompt, new_conv=False):
        pg = self.page
        
        if new_conv:
            pg.goto(self.url, wait_until='commit', timeout=30000)
            time.sleep(4)
        
        # Wait for model to finish loading
        for i in range(15):
            try:
                body = pg.locator('body').inner_text()
                if 'Model loading' not in body and len(body) > 100:
                    break
            except: pass
            time.sleep(1)
        
        # Find textarea
        textbox = pg.locator('textarea').first
        if textbox.count() == 0 or not textbox.is_visible(timeout=5000):
            return {"ok": False, "err": "no-input", "msg": "Input not found. Auth may have expired."}
        
        textbox.click(force=True, timeout=3000)
        time.sleep(0.3)
        textbox.fill(prompt)
        time.sleep(0.3)
        textbox.press('Enter')
        
        # Wait for response — assistant message element appears
        deadline = time.time() + 120
        response_text = ""
        while time.time() < deadline:
            assistant = pg.locator('[class*="assistant"]')
            if assistant.count() > 0:
                time.sleep(2)
                text = assistant.last.inner_text().strip()
                if len(text) > 5:
                    response_text = text
                    break
            time.sleep(1)
        
        if response_text:
            # Strip "Thinking completed" prefix and any status text before the actual answer
            parts = response_text.split('Thinking completed')
            response_text = parts[-1].strip() if len(parts) > 1 else response_text.strip()
            # Also strip any leading status lines
            lines = response_text.split('\n')
            response_text = '\n'.join(l for l in lines if not l.startswith('Considering') and not l.startswith('Skip'))
            return {"ok": True, "text": response_text.strip(), "model": self.get_model()}
        
        return {"ok": False, "err": "empty-response", "msg": "No response extracted"}
    
    def _extract_qwen_response(self, page):
        """Extract Qwen's last assistant response from the DOM."""
        try:
            # Qwen wraps assistant messages in specific containers
            msgs = page.locator('[class*="assistant"], [class*="bot"], [class*="response"]')
            if msgs.count() > 0:
                return msgs.last.inner_text().strip()
            
            # Fallback: body text after the prompt
            body = page.locator('body').inner_text()
            # Try to find the response after "Copy" button
            lines = body.split('\n')
            copy_idxs = [i for i, l in enumerate(lines) if l.strip() in ('Copy', '复制')]
            if copy_idxs:
                start = copy_idxs[-1] + 1
                end = len(lines)
                for i in range(start, len(lines)):
                    if lines[i].strip() in ('Ask anything', 'Enter prompt', '输入问题'):
                        end = i
                        break
                return '\n'.join(lines[start:end]).strip()
            
            return ""
        except:
            return ""


class MinimaxDriver(BaseDriver):
    """MiniMax (agent.minimax.io) — virtualized DOM, requires headed Chromium."""
    name = "minimax"
    url = "https://agent.minimax.io"
    
    def _logout_phrases(self):
        return ['sign in', 'log in', '登录']
    
    def send_prompt(self, prompt, new_conv=False):
        pg = self.page
        
        if new_conv:
            pg.goto(self.url, wait_until='commit', timeout=30000)
            time.sleep(4)
        
        # MiniMax uses TipTap/ProseMirror editor
        editor = pg.locator('.ProseMirror, [contenteditable="true"], textarea').first
        if editor.count() == 0:
            return {"ok": False, "err": "no-input", "msg": "Editor not found"}
        
        try:
            editor.click(force=True, timeout=5000)
            time.sleep(0.5)
            pg.keyboard.type(prompt, delay=20)
            time.sleep(0.5)
            pg.keyboard.press('Enter')
        except Exception as e:
            return {"ok": False, "err": "input-error", "msg": str(e)[:200]}
        
        # Wait for response — MiniMax uses [class*="message-animate-in"] divs
        deadline = time.time() + 120
        response_text = ""
        while time.time() < deadline:
            try:
                msgs = pg.locator('[class*="message-animate-in"]')
                if msgs.count() >= 2:  # At least user + assistant messages
                    time.sleep(2)
                    # Last message-animate-in should be the assistant response
                    text = msgs.last.inner_text().strip()
                    # Filter out "Thought N time(s)" prefix and trailing timestamp
                    import re
                    text = re.sub(r'^Thought\s+\d+\s+time\(s\)\s*', '', text)
                    text = re.sub(r'\n+\d{2}:\d{2}\s*$', '', text)  # Timestamp
                    if len(text) > 3:
                        response_text = text
                        break
            except: pass
            time.sleep(1)
        
        if response_text:
            return {"ok": True, "text": response_text, "model": self.get_model()}
        
        return {"ok": False, "err": "timeout", "msg": "Response timeout"}


class MiMoDriver(BaseDriver):
    """MiMo Studio (aistudio.xiaomimimo.com) — JS SPA rendering."""
    name = "mimo"
    url = "https://aistudio.xiaomimimo.com"
    
    def _logout_phrases(self):
        return ['sign in', 'log in', '登录', '手机号登录']
    
    def send_prompt(self, prompt, new_conv=False):
        pg = self.page
        
        if new_conv:
            pg.goto(self.url, wait_until='commit', timeout=30000)
            time.sleep(5)
        
        # MiMo uses a chat input area
        editor = pg.locator('textarea, [contenteditable="true"], input[type="text"]').first
        if editor.count() == 0:
            return {"ok": False, "err": "no-input", "msg": "Chat input not found"}
        
        try:
            editor.click(force=True, timeout=5000)
            time.sleep(0.5)
            editor.fill(prompt)
            time.sleep(0.5)
            pg.keyboard.press('Enter')
        except Exception as e:
            return {"ok": False, "err": "input-error", "msg": str(e)[:200]}
        
        # Wait for response
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                # MiMo wraps responses in message containers
                msgs = pg.locator('[class*="message"], [class*="bubble"], [class*="response"], [class*="chat-item"]')
                if msgs.count() > 1:
                    time.sleep(2)
                    text = msgs.last.inner_text().strip()
                    if len(text) > 10:
                        return {"ok": True, "text": text, "model": self.get_model()}
            except: pass
            time.sleep(1)
        
        return {"ok": False, "err": "timeout", "msg": "Response timeout"}


DRIVERS = {
    "chatgpt": ChatGPTDriver,
    "qwen": QwenDriver,
    "minimax": MinimaxDriver,
    "mimo": MiMoDriver,
}

# ── HTTP Server ───────────────────────────────────────────

_driver = None

class QueryHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silence HTTP logs
    
    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def do_GET(self):
        if self.path == '/health':
            global _driver
            alive = _driver and _driver.ensure_alive()
            self._json_response({"ok": alive, "uptime": _driver.uptime() if _driver else 0})
        elif self.path == '/status':
            if not _driver:
                self._json_response({"ok": False, "err": "no-driver"})
                return
            self._json_response({
                "ok": True,
                "logged_in": _driver.is_logged_in(),
                "model": _driver.get_model(),
                "uptime": _driver.uptime(),
            })
        else:
            self._json_response({"ok": False, "err": "not-found"}, 404)
    
    def do_POST(self):
        if self.path == '/query' or self.path == '/query/new':
            new_conv = self.path == '/query/new'
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                prompt = body.get('prompt', '')
            except:
                self._json_response({"ok": False, "err": "invalid-json"}, 400)
                return
            
            if not prompt:
                self._json_response({"ok": False, "err": "no-prompt"}, 400)
                return
            
            if not _driver:
                self._json_response({"ok": False, "err": "no-driver"}, 500)
                return
            
            if not _driver.is_logged_in():
                self._json_response({"ok": False, "err": "auth-expired", "msg": "Session expired. Re-run --login."})
                return
            
            result = _driver.send_prompt(prompt, new_conv=new_conv)
            self._json_response(result)
        else:
            self._json_response({"ok": False, "err": "not-found"}, 404)

# ── Daemon Lifecycle ──────────────────────────────────────

def daemonize():
    """Double-fork to detach from terminal."""
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    fd = os.open(os.devnull, os.O_RDWR)
    for f in (0, 1, 2):
        os.dup2(fd, f)
    os.close(fd)

def is_running(pid_file):
    if not os.path.exists(pid_file):
        return False
    try:
        pid = int(open(pid_file).read().strip())
        os.kill(pid, 0)
        return True
    except:
        return False

def main():
    parser = argparse.ArgumentParser(description="Page Server for AI web platforms")
    parser.add_argument('--platform', required=True, choices=list(DRIVERS.keys()))
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--headed', action='store_true', default=False,
                        help='Use headed Chromium (needed for Cloudflare/WAF bypass)')
    parser.add_argument('--start', action='store_true', help='Start the daemon')
    parser.add_argument('--stop', action='store_true', help='Stop the daemon')
    parser.add_argument('--status', action='store_true', help='Check server status')
    args = parser.parse_args()
    
    home = Path.home()
    pid_file = home / f'.{args.platform}-cli' / 'server.pid'
    profile_dir = home / f'.{args.platform}-cli' / 'browser-profile'
    profile_dir.mkdir(parents=True, exist_ok=True)
    
    if args.stop:
        if is_running(pid_file):
            pid = int(open(pid_file).read().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try: os.unlink(pid_file)
            except: pass
            print(f"Stopped {args.platform} server (PID {pid})")
        else:
            print(f"{args.platform} server not running")
        return
    
    if args.status:
        if is_running(pid_file):
            import urllib.request
            try:
                resp = urllib.request.urlopen(f'http://127.0.0.1:{args.port}/status', timeout=3)
                data = json.loads(resp.read())
                print(json.dumps(data, indent=2))
            except:
                print(f"Server running but not responding on port {args.port}")
        else:
            print(f"{args.platform} server not running")
        return
    
    if args.start:
        if is_running(pid_file):
            print(f"{args.platform} server already running (PID {open(pid_file).read().strip()})")
            return
        
        daemonize()
        
        # Write PID
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))
        
        global _driver
        DriverClass = DRIVERS[args.platform]
        _driver = DriverClass(profile_dir, headless=not args.headed)
        
        try:
            _driver.launch()
        except Exception as e:
            try: os.unlink(pid_file)
            except: pass
            sys.exit(1)
        
        server = HTTPServer(('127.0.0.1', args.port), QueryHandler)
        
        def cleanup(*args):
            try: os.unlink(pid_file)
            except: pass
            if _driver:
                _driver.close()
            os._exit(0)
        
        signal.signal(signal.SIGTERM, cleanup)
        signal.signal(signal.SIGINT, cleanup)
        
        try:
            server.serve_forever()
        except:
            cleanup()

if __name__ == '__main__':
    main()
