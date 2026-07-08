#!/usr/bin/env python3
"""Daemonized Chromium CDP server for Hermes web CLIs.
Launch once, then CLIs connect via http://127.0.0.1:9223
Keeps auth cookies alive across queries."""
import os, sys, time, signal, json

PID_FILE = os.path.expanduser('~/.hermes/chrome-cdp-profile/server.pid')
PROFILE_DIR = os.path.expanduser('~/.hermes/chrome-cdp-profile/profile')
CDP_PORT = 9223

def daemonize():
    """Double-fork to detach from terminal, redirect stdio to /dev/null."""
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    fd = os.open(os.devnull, os.O_RDWR)
    for f in (0, 1, 2):
        os.dup2(fd, f)
    os.close(fd)

def is_running():
    if not os.path.exists(PID_FILE):
        return False
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, 0)
        return True
    except:
        return False

def start(headless=True):
    if is_running():
        print(f'Server already running (PID {open(PID_FILE).read().strip()})')
        return

    daemonize()
    
    # Write PID
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    os.makedirs(PROFILE_DIR, exist_ok=True)
    
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    
    try:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            viewport={'width': 1280, 'height': 800},
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                f'--remote-debugging-port={CDP_PORT}',
            ],
            ignore_default_args=['--enable-automation'],
        )
        
        # Keep alive
        while True:
            time.sleep(30)
            # Health check: verify context still alive
            try:
                ctx.pages
            except:
                break
    finally:
        try: ctx.close()
        except: pass
        try: pw.stop()
        except: pass
        os.unlink(PID_FILE)

def stop():
    if not is_running():
        print('Server not running')
        return
    pid = int(open(PID_FILE).read().strip())
    os.kill(pid, signal.SIGTERM)
    print(f'Sent SIGTERM to {pid}')

def status():
    if is_running():
        pid = int(open(PID_FILE).read().strip())
        print(f'Running (PID {pid})')
        # Check CDP
        import urllib.request
        try:
            resp = urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/version', timeout=3)
            data = json.loads(resp.read())
            print(f'  Browser: {data.get("Browser", "unknown")}')
        except:
            print('  CDP: not responding')
    else:
        print('Not running')

def login():
    """Open visible browser for initial login."""
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE_DIR, exist_ok=True)
    pw = sync_playwright().start()
    print('Opening browser. Log in to your AI platforms, then close this window.')
    ctx = pw.chromium.launch_persistent_context(
        PROFILE_DIR, headless=False,
        viewport={'width': 1280, 'height': 800},
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled', f'--remote-debugging-port={CDP_PORT}'],
        ignore_default_args=['--enable-automation'],
    )
    # Open all platforms
    for url in ['https://chat.qwen.ai', 'https://agent.minimax.io', 'https://kimi.com', 'https://chatgpt.com']:
        page = ctx.new_page()
        page.goto(url, wait_until='domcontentloaded', timeout=30000)
        print(f'  Opened: {url}')
    
    print('Waiting for you to log in... (close browser when done)')
    try:
        while True:
            if len(ctx.pages) == 0:
                break
            time.sleep(1)
    except:
        pass
    finally:
        try: ctx.close()
        except: pass
        try: pw.stop()
        except: pass

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    headed = '--headed' in sys.argv or '--visible' in sys.argv
    if cmd == 'start':
        start(headless=not headed)
    elif cmd == 'stop':
        stop()
    elif cmd == 'status':
        status()
    elif cmd == 'login':
        login()
    elif cmd == 'restart':
        stop()
        time.sleep(2)
        start(headless=not headed)
    else:
        print(f'Usage: {sys.argv[0]} start|stop|status|login|restart [--headed]')
