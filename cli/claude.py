#!/usr/bin/env python3
"""Claude.ai CLI — saved tokens + Firefox fallback. Multi-account with rate-limit auto-switch.

Setup: python claude.py --save-all
Usage:  python claude.py "prompt"
        python claude.py --brief "prompt"
        python claude.py --list-models
        python claude.py -o /tmp/out.md "prompt"
"""

import os, sys, json, time, argparse, sqlite3, shutil, uuid
from pathlib import Path

HOME = Path.home()
DIR = HOME / ".claude-cli"
ACCOUNTS_FILE = DIR / "accounts.json"
BASE = "https://claude.ai"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
RATE_COOLDOWN = 300
_Q = False

def fail(c,r): print(json.dumps({"ok":False,"err":c,"msg":r},ensure_ascii=False)); sys.exit(1)
def log(m): print(m,file=sys.stderr,flush=True)
def info(m):
    if not _Q and sys.stderr.isatty(): print(f"[claude] {m}",file=sys.stderr)

def load_accounts():
    if ACCOUNTS_FILE.exists():
        try: return json.loads(ACCOUNTS_FILE.read_text())
        except: pass
    return {}
def save_accounts(a):
    DIR.mkdir(parents=True,exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(a,indent=2))

def scan_profiles():
    """Scan Firefox profiles for fresh Claude sessions (includes cf_clearance)."""
    import httpx
    sessions = []; seen = set()
    for ud in Path("/mnt/c/Users").iterdir():
        if not ud.is_dir(): continue
        fp = ud / "AppData/Roaming/Mozilla/Firefox/Profiles"
        if not fp.exists(): continue
        for p in fp.iterdir():
            if not (p / "cookies.sqlite").exists(): continue
            try:
                t = Path(f"/tmp/cs_{os.getpid()}.sqlite")
                shutil.copy2(str(p/"cookies.sqlite"),str(t))
                c = sqlite3.connect(str(t)); cur = c.cursor()
                cur.execute("SELECT name,value FROM moz_cookies WHERE host LIKE '%claude%'")
                ck = {n:v.strip('"') for n,v in cur.fetchall()}
                c.close(); t.unlink(missing_ok=True)
                if "sessionKey" not in ck: continue
                cks = "; ".join(f"{n}={v}" for n,v in ck.items())
                r = httpx.get(f"{BASE}/api/organizations",
                    headers={"cookie":cks,"user-agent":UA,"accept":"application/json"})
                if r.status_code==200:
                    oid = r.json()[0]["uuid"]
                    if oid not in seen:
                        seen.add(oid)
                        sessions.append({"org_id":oid,"name":r.json()[0].get("name","?"),
                            "cookie_string":cks,"profile":p.name})
            except: pass
    return sessions

def save_all():
    sessions = scan_profiles()
    if not sessions: fail("no-auth","No Claude sessions. Log into claude.ai in Firefox.")
    accts = {}
    for s in sessions:
        label = s["profile"].replace(".default-release","").replace(".default","")
        if len(label)>15: label=label[:15]
        accts[label] = {"org_id":s["org_id"],"name":s["name"],"cookie_string":s["cookie_string"],"added":time.time()}
    save_accounts(accts)
    print(f"Saved {len(accts)} account(s):")
    for l,a in accts.items(): print(f"  {l}: {a['name']}")

def list_accounts():
    accts = load_accounts()
    if not accts: print("No accounts. Run: claude.py --save-all"); return []
    print(f"  {'LABEL':<15} {'ACCOUNT':<45} {'DEFAULT'}")
    print(f"  {'-'*15} {'-'*45} {'-'*7}")
    d = list(accts.keys())[0]
    for l,a in accts.items(): print(f"  {l:<15} {a.get('name','?')[:44]:<45} {'←' if l==d else ''}")
    return list(accts.keys())

def discover_models(auth):
    """Probe which model names the API accepts. Returns list of working model names."""
    import httpx
    oid = auth["org_id"]; cks = auth["cookie_string"]
    h = {"cookie":cks,"user-agent":UA,"content-type":"application/json","origin":BASE,"referer":f"{BASE}/"}
    
    # Create temp conversation for probing
    r = httpx.post(f"{BASE}/api/organizations/{oid}/chat_conversations",
        headers={**h,"accept":"application/json"},
        json={"uuid":str(uuid.uuid4()),"name":"model-probe"},timeout=15)
    if r.status_code not in (200,201): return []
    cuuid = r.json()["uuid"]
    
    # Probe known model names
    candidates = [
        "claude-sonnet-4", "claude-opus-4", "claude-haiku-3-5",
        "claude-sonnet-4-5", "claude-sonnet-4-20250514",
        "claude-4-sonnet", "claude-4-opus", "claude-4-haiku",
        "claude-3.5-sonnet", "claude-3-opus", "claude-3.5-haiku",
    ]
    working = []
    h2 = {**h,"accept":"text/event-stream"}
    
    # Test no-model first (always works)
    r = httpx.post(f"{BASE}/api/organizations/{oid}/chat_conversations/{cuuid}/completion",
        headers=h2,json={"prompt":"hi","timezone":"Asia/Hong_Kong"},timeout=15)
    if r.status_code == 200:
        # Try to figure out what model served us
        text = ""
        for line in r.iter_lines():
            if line and line.startswith("data: "):
                try:
                    d = json.loads(line[6:].strip())
                    if d.get("type")=="completion": text += d.get("completion","")
                    if d.get("model"): working.append(f"default: {d['model']}")
                except: pass
        if text and not any("default:" in w for w in working):
            working.append("default (auto)")
    else:
        working.append("default (auto)")
    
    for model in candidates:
        try:
            r2 = httpx.post(f"{BASE}/api/organizations/{oid}/chat_conversations/{cuuid}/completion",
                headers=h2,json={"prompt":"hi","timezone":"Asia/Hong_Kong","model":model},timeout=15)
            if r2.status_code == 200:
                working.append(model)
        except:
            pass
    return working

def claude_chat(prompt, model=None, conv=None, auth=None):
    import httpx
    oid = auth["org_id"]; cks = auth["cookie_string"]
    h = {"cookie":cks,"user-agent":UA,"content-type":"application/json","origin":BASE,"referer":f"{BASE}/"}
    cuuid = conv.get("conversation_uuid") if conv else None
    if not cuuid:
        r = httpx.post(f"{BASE}/api/organizations/{oid}/chat_conversations",
            headers={**h,"accept":"application/json"},
            json={"uuid":str(uuid.uuid4()),"name":prompt[:50].split("\n")[0].strip()},timeout=15)
        if r.status_code not in (200,201): raise Exception(f"Create: {r.status_code}")
        cuuid = r.json()["uuid"]
    log("[CLAUDE:LOADING]")
    h2 = {**h,"accept":"text/event-stream"}
    body = {"prompt":prompt,"timezone":"Asia/Hong_Kong"}
    if model: body["model"] = model
    r = httpx.post(f"{BASE}/api/organizations/{oid}/chat_conversations/{cuuid}/completion",
        headers=h2,json=body,timeout=300)
    if r.status_code == 403:
        # Could be auth-expired or model_not_available
        try:
            err = r.json().get("error", {})
            if err.get("details", {}).get("error_code") == "model_not_available" and model:
                # Retry without model parameter
                body.pop("model", None)
                r = httpx.post(f"{BASE}/api/organizations/{oid}/chat_conversations/{cuuid}/completion",
                    headers=h2,json=body,timeout=300)
                if r.status_code == 200: pass  # fall through to parsing
                else: raise Exception("auth-expired")
            else:
                raise Exception("auth-expired")
        except (json.JSONDecodeError, KeyError):
            raise Exception("auth-expired")
    if r.status_code == 429:
        raise Exception("rate-limit")
    if r.status_code != 200: raise Exception(f"API {r.status_code}")
    t = ""
    for line in r.iter_lines():
        if line and line.startswith("data: "):
            try:
                d = json.loads(line[6:].strip())
                if d.get("type")=="completion": t += d.get("completion","")
            except: pass
    t = t.strip()
    if not t: raise Exception("empty-response")
    return t, {"conversation_uuid":cuuid}

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
    p.add_argument("-m","--model",default=None)
    p.add_argument("-c","--conversation"); p.add_argument("--new",action="store_true")
    p.add_argument("-o","--output"); p.add_argument("--json",action="store_true")
    p.add_argument("--brief",action="store_true",help="Prepend 'Be concise.' to prompt")
    p.add_argument("--list-models",action="store_true",help="Probe available models and exit")
    p.add_argument("-q","--quiet",action="store_true")
    p.add_argument("--accounts",action="store_true")
    p.add_argument("--account",help="Account label")
    p.add_argument("--save-all",action="store_true")
    p.add_argument("--remove",help="Remove account")
    p.add_argument("--set-default",help="Set default")
    p.add_argument("--reset-limits",action="store_true")
    args = p.parse_args()
    if args.quiet: _Q = True
    
    if args.save_all: save_all(); return
    if args.reset_limits: (DIR/"rate_limits.json").unlink(missing_ok=True); print("Reset."); return
    if args.remove:
        a = load_accounts()
        if args.remove in a: del a[args.remove]; save_accounts(a); print(f"Removed")
        else: print("Not found")
        return
    if args.set_default:
        a = load_accounts()
        if args.set_default in a:
            v = a.pop(args.set_default); save_accounts({args.set_default:v,**a}); print("Default set")
        else: print("Not found")
        return
    if args.accounts: list_accounts(); return
    
    prompt = args.prompt_flag or (" ".join(args.prompt) if args.prompt else None)
    if not prompt and not sys.stdin.isatty(): prompt = sys.stdin.read().strip()
    
    # Handle --list-models (needs prompt only for auth init)
    if args.list_models:
        if not prompt:
            prompt = "hi"  # dummy prompt to trigger auth loading
    
    if not prompt: p.print_help(); sys.exit(1)
    
    # Load accounts — fall back to scanning profiles if saved tokens fail
    accts = load_accounts()
    if not accts:
        info("No saved accounts. Scanning Firefox profiles...")
        sessions = scan_profiles()
        if not sessions: fail("no-auth","No Claude sessions. Log into claude.ai in Firefox.")
        accts = {s["profile"][:15]: {"org_id":s["org_id"],"name":s["name"],"cookie_string":s["cookie_string"]} for s in sessions}
    
    conv = load_conv(args.conversation) if args.conversation else {}
    if args.new: conv = {}
    
    label = args.account or list(accts.keys())[0]
    if label not in accts: fail("no-auth",f"'{label}' not found")
    
    # --list-models: probe and exit
    if args.list_models:
        models = discover_models(accts[label])
        print(json.dumps({"ok":True,"models":models},ensure_ascii=False))
        return
    
    if args.brief and not prompt.startswith("Be concise"):
        prompt = "Be concise. " + prompt
    
    rl = DIR/"rate_limits.json"
    cd = json.loads(rl.read_text()) if rl.exists() else {}
    
    result = None
    labels = list(accts.keys())
    start = labels.index(label)
    
    for attempt in range(len(labels)):
        idx = (start+attempt)%len(labels); lbl = labels[idx]; acct = accts[lbl]
        last = cd.get(lbl,0)
        if time.time()-last < RATE_COOLDOWN:
            info(f"Skip '{lbl}' (cooldown)"); continue
        info(f"Try: {lbl}")
        try:
            t, cu = claude_chat(prompt, model=args.model, conv=conv, auth=acct)
            result = t; conv.update(cu); log("[CLAUDE:DONE]"); break
        except Exception as e:
            if "rate-limit" in str(e):
                cd[lbl]=time.time(); rl.parent.mkdir(parents=True,exist_ok=True)
                rl.write_text(json.dumps(cd)); info("Rate-limited → next"); continue
            if "auth-expired" in str(e) or "Create:" in str(e):
                # Token expired — try re-scanning profiles for this account
                info(f"Auth expired for {lbl}. Re-scanning...")
                sessions = scan_profiles()
                for s in sessions:
                    new_label = s["profile"].replace(".default-release","").replace(".default","")[:15]
                    accts[new_label] = {"org_id":s["org_id"],"name":s["name"],"cookie_string":s["cookie_string"]}
                save_accounts(accts)
                # Try again with refreshed token
                if lbl in accts:
                    acct = accts[lbl]
                    try:
                        t, cu = claude_chat(prompt, model=args.model, conv=conv, auth=acct)
                        result = t; conv.update(cu); log("[CLAUDE:DONE]"); break
                    except: pass
                continue
            raise
    
    if result is None: fail("rate-limit","All accounts unavailable.")
    if args.conversation: save_conv(args.conversation, conv)
    if args.output:
        op = Path(args.output); op.write_text(result,encoding="utf-8")
        print(json.dumps({"f":str(op),"s":op.stat().st_size,"b":result.count("```")//2},ensure_ascii=False))
    elif args.json:
        print(json.dumps({"ok":True,"text":result,"model":args.model},ensure_ascii=False))
    else:
        print(result)

if __name__=="__main__":
    try: main()
    except SystemExit: raise
    except Exception as e: fail("error",str(e))
