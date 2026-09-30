#!/usr/bin/env python3
"""coolify-watchtower testbed: a small service you can break on purpose.

Deploy it in Coolify, opt it in to coolify-watchtower, then use the web page to make it unhealthy,
crash, hang or start slowly, and publish new (or broken) versions with the testbed GitHub workflow.

ROLE=web serves the control page on :8080 and forwards worker actions; ROLE=worker only has /health.
State that must survive restarts (persistent failure, crash loop, slow start, boot count) lives in /data.
Stdlib only.
"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = os.environ.get("TESTBED_VERSION", "dev")
BROKEN = os.environ.get("TESTBED_BROKEN", "false").lower() == "true"  # a deliberately broken release
ROLE = os.environ.get("ROLE", "web")
WORKER_URL = os.environ.get("WORKER_URL", "http://worker:8080")
DATA = os.environ.get("DATA_DIR", "/data")
SLOW_START_SECONDS = int(os.environ.get("SLOW_START_SECONDS", "100"))
CRASH_LOOP_BOOTS = 5

STARTED = time.time()
state = {"unhealthy": False, "hang": False}  # in memory: a restart clears these


def path(name):
    return os.path.join(DATA, name)


def read(name, default=""):
    try:
        with open(path(name)) as f:
            return f.read().strip()
    except OSError:
        return default


def write(name, value):
    os.makedirs(DATA, exist_ok=True)
    with open(path(name), "w") as f:
        f.write(str(value))


def remove(name):
    try:
        os.remove(path(name))
    except OSError:
        pass


def boot():
    """Count boots, run down a crash loop, and arm a slow start."""
    boots = int(read("boots", "0") or 0) + 1
    write("boots", boots)
    loop = int(read("crash-loop", "0") or 0)
    if loop > 0:
        write("crash-loop", loop - 1)
        print(f"crash loop: exiting ({loop - 1} crashes left)", flush=True)
        time.sleep(3)
        sys.exit(1)
    if read("slow-start") == "armed":
        write("slow-start", str(time.time() + SLOW_START_SECONDS))
    return boots


def health():
    """(HTTP status, reason) for /health."""
    if BROKEN:
        return 500, "this release is broken on purpose"
    if read("unhealthy") == "yes":
        return 500, "unhealthy (persistent, survives restarts)"
    if state["unhealthy"]:
        return 500, "unhealthy (until the next restart)"
    until = float(read("slow-start", "0") or 0) if read("slow-start") not in ("", "armed") else 0
    if until > time.time():
        return 503, f"slow start: {int(until - time.time())} s left"
    return 200, "healthy"


def status():
    code, reason = health()
    return {
        "role": ROLE, "version": VERSION, "broken_release": BROKEN, "healthy": code == 200, "reason": reason,
        "hang": state["hang"], "uptime": int(time.time() - STARTED), "boots": int(read("boots", "0") or 0),
        "persistent_unhealthy": read("unhealthy") == "yes",
    }


def act(name):
    """Apply an action to this container. Returns a message."""
    if name == "unhealthy":
        state["unhealthy"] = True
        return "Unhealthy until the next restart."
    if name == "unhealthy-persistent":
        write("unhealthy", "yes")
        return "Unhealthy, and it survives restarts. Use Recover to undo."
    if name == "hang":
        state["hang"] = True
        return "The health check now hangs until the next restart."
    if name == "slow-start":
        write("slow-start", "armed")
        return f"The next start stays in 'starting' for {SLOW_START_SECONDS} s."
    if name == "crash":
        threading.Timer(0.5, lambda: os._exit(1)).start()
        return "Crashing now."
    if name == "crash-loop":
        write("crash-loop", CRASH_LOOP_BOOTS)
        threading.Timer(0.5, lambda: os._exit(1)).start()
        return f"Crashing now, and on the next {CRASH_LOOP_BOOTS} starts."
    if name == "recover":
        state.update(unhealthy=False, hang=False)
        for f in ("unhealthy", "slow-start", "crash-loop"):
            remove(f)
        return "Recovered: all failure modes cleared."
    return None


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>testbed</title>
<style>
:root{--ink:#111;--paper:#fbf7ee;--green:#4ade80;--yellow:#ffd23f;--red:#ff8a80;--purple:#b69cff}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px 16px}h1{font-size:34px;margin:0 0 4px}
.card{background:#fff;border:3px solid var(--ink);border-radius:10px;box-shadow:6px 6px 0 var(--ink);padding:18px;margin:18px 0}
.state{font-size:22px;font-weight:700;padding:14px 18px}.ok{background:var(--green)}.bad{background:var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
button{font:700 15px system-ui;padding:12px 14px;border:3px solid var(--ink);border-radius:10px;background:#fff;
box-shadow:4px 4px 0 var(--ink);cursor:pointer;text-align:left;width:100%}
button:active{transform:translate(4px,4px);box-shadow:none}button small{display:block;font-weight:400;margin-top:4px}
.warn{background:var(--yellow)}.calm{background:var(--green)}h2{margin:0 0 12px;font-size:20px}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 16px;margin:0}dt{font-weight:700}
#msg{min-height:1.5em;font-weight:700}
</style></head><body><div class="wrap">
<h1>testbed</h1><p>A service for testing coolify-watchtower. Break it on purpose.</p>
<div id="state" class="card state">loading...</div>
<div class="card"><dl id="info"></dl></div>
<p id="msg" role="status"></p>
<div class="card"><h2>This container (web)</h2><div class="grid">
<button data-a="unhealthy">Make unhealthy<small>until the next restart: self-healing fixes it</small></button>
<button data-a="unhealthy-persistent" class="warn">Make unhealthy, permanently<small>survives restarts: self-healing gives up</small></button>
<button data-a="hang">Hang the health check<small>running, but the check times out</small></button>
<button data-a="crash">Crash once<small>exits, Docker restarts it</small></button>
<button data-a="crash-loop" class="warn">Crash loop<small>crashes on the next 5 starts: degraded</small></button>
<button data-a="slow-start">Slow start next time<small>stays 'starting' for a few minutes</small></button>
<button data-a="recover" class="calm">Recover<small>clear every failure mode</small></button>
</div></div>
<div class="card"><h2>Worker container</h2><div class="grid">
<button data-a="unhealthy" data-t="worker">Make the worker unhealthy<small>only the worker should be restarted</small></button>
<button data-a="unhealthy-persistent" data-t="worker" class="warn">Worker unhealthy, permanently<small>survives restarts</small></button>
<button data-a="crash-loop" data-t="worker" class="warn">Worker crash loop<small>crashes on its next 5 starts</small></button>
<button data-a="recover" data-t="worker" class="calm">Recover the worker<small>clear its failure modes</small></button>
</div></div>
</div><script>
const $=id=>document.getElementById(id);
async function refresh(){try{const s=await (await fetch('status')).json();
const w=s.worker||{};const el=$('state');el.className='card state '+(s.healthy&&w.healthy!==false?'ok':'bad');
el.textContent='web: '+s.reason+(s.hang?' (health check hanging)':'')+'  |  worker: '+(w.reason||'unreachable');
$('info').innerHTML=[['Version',s.version+(s.broken_release?' (broken release)':'')],['Uptime',s.uptime+' s'],
['Starts (web)',s.boots],['Starts (worker)',w.boots??'-']].map(([k,v])=>'<dt>'+k+'</dt><dd>'+v+'</dd>').join('');}
catch(e){$('state').className='card state bad';$('state').textContent='No answer: restarting?';}}
document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{try{const r=await fetch('action/'+b.dataset.a+
(b.dataset.t?'?target=worker':''),{method:'POST'});$('msg').textContent=(await r.json()).message}
catch(e){$('msg').textContent='Sent. It is restarting.'}setTimeout(refresh,800)});
refresh();setInterval(refresh,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            if state["hang"]:
                time.sleep(60)
            code, reason = health()
            return self.send(code, {"status": reason})
        if self.path == "/status":
            s = status()
            if ROLE == "web":
                try:
                    with urllib.request.urlopen(f"{WORKER_URL}/status", timeout=2) as r:
                        s["worker"] = json.loads(r.read())
                except Exception:
                    s["worker"] = None
            return self.send(200, s)
        if self.path in ("/", "/index.html") and ROLE == "web":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        return self.send(404, {"message": "not found"})

    def do_POST(self):
        if not self.path.startswith("/action/"):
            return self.send(404, {"message": "not found"})
        name, _, query = self.path[len("/action/"):].partition("?")
        if ROLE == "web" and query == "target=worker":
            try:
                req = urllib.request.Request(f"{WORKER_URL}/action/{name}", method="POST")
                with urllib.request.urlopen(req, timeout=3) as r:
                    return self.send(200, json.loads(r.read()))
            except Exception as e:
                return self.send(502, {"message": f"worker unreachable: {e}"})
        message = act(name)
        if message is None:
            return self.send(400, {"message": f"unknown action {name}"})
        print(f"action {name}: {message}", flush=True)
        return self.send(200, {"message": message})


if __name__ == "__main__":
    boots = boot()
    print(f"testbed {VERSION} role={ROLE} boot={boots} broken_release={BROKEN}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
