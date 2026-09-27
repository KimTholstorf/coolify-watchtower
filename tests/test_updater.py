"""Offline tests: cron, image refs, and one full tick against mocked Coolify + Docker APIs.
Run: python3 tests/test_updater.py (registry is monkeypatched; no network needed)."""
import json, threading, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
os.environ.update(COOLIFY_URL="http://127.0.0.1:18080", COOLIFY_TOKEN="t", DOCKER_HOST="tcp://127.0.0.1:12375", DRY_RUN="false", TZ="Europe/Copenhagen")
import updater as u

# cron tests
d = datetime(2026,9,27,4,30)  # Sunday
assert u.cron_match("30 4 * * *", d)
assert u.cron_match("30 4 * * 0", d) and u.cron_match("30 4 * * 7", d)
assert not u.cron_match("30 4 * * 1-5", d)
assert u.cron_match("*/15 * * * *", d) and not u.cron_match("*/7 * * * *", d)
assert u.cron_match("weekly", datetime(2026,9,27,0,0)) and u.cron_match("daily", datetime(2026,9,28,0,0))
assert u.cron_match("0,30 3-5 * * *", d)
assert u.cron_match("30 4 1 * 0", d)  # dom OR dow
# ref tests
assert u.parse_ref("nginx") == ("docker.io","library/nginx","latest")
assert u.parse_ref("ghcr.io/pocket-id/pocket-id:v1") == ("ghcr.io","pocket-id/pocket-id","v1")
assert u.parse_ref("louislam/uptime-kuma:2") == ("docker.io","louislam/uptime-kuma","2")
assert u.parse_ref("localhost:5000/app:dev") == ("localhost:5000","app","dev")
assert u.parse_ref("nginx@sha256:abc") is None
# label value tests
assert u.label_schedule("true") == ("daily", True) and u.label_schedule("") == ("daily", True)
assert u.label_schedule("*/5 * * * *") == ("*/5 * * * *", True)
assert u.label_schedule("false")[1] is False
# notification payloads
h, b = u.notify_request("https://discord.com/api/webhooks/1/abc", "t", "nginx:1 -> 2")
assert h["Content-Type"] == "application/json" and json.loads(b) == {"content": "**t**\n```\nnginx:1 -> 2\n```", "allowed_mentions": {"parse": []}}
assert u.notify_request("https://ptb.discordapp.com/api/webhooks/1/abc", "t", "x")[0]["Content-Type"] == "application/json"
assert len(json.loads(u.notify_request("https://discord.com/api/webhooks/1/a", "t", "x" * 5000)[1])["content"]) <= 2000
h, b = u.notify_request("https://ntfy.sh/topic", "t", "body")
assert h["Title"] == "t" and h["Content-Type"] == "text/plain" and b == b"body"
assert u.notify_request("https://example.com/?next=https://discord.com/api/webhooks/1/a", "t", "x")[0]["Content-Type"] == "text/plain"
print("unit ok")

restarts=[]
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send(self, obj, code=200):
        b=json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p=self.path
        if p=="/api/v1/version":  # real Coolify answers with plain text, not JSON
            self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers(); return self.wfile.write(b"4.2.1")
        if p=="/api/v1/services": return self.send([{"uuid":"svc1","name":"uptime-kuma","server_id":0},{"uuid":"svc2","name":"pocket-id","server_id":0},{"uuid":"svc3","name":"vaultwarden","server_id":1},{"uuid":"svc4","name":"gotify","server_id":0}])
        if p=="/api/v1/services/svc1/scheduled-tasks": return self.send([{"name":"auto-update","frequency":"30 4 * * *","enabled":True}])
        if p=="/api/v1/services/svc2/scheduled-tasks": return self.send([{"name":"Auto-Update","frequency":"daily","enabled":False}])
        if p in ("/api/v1/services/svc3/scheduled-tasks","/api/v1/services/svc4/scheduled-tasks"): return self.send([])
        if p=="/api/v1/servers": return self.send([{"name":"localhost","settings":{"server_id":0,"server_timezone":"Europe/Copenhagen"}},{"name":"remote","settings":{"server_id":1,"server_timezone":"America/New_York"}}])
        if p=="/api/v1/applications": return self.send([{"uuid":"app1","name":"myapp"}])
        if p=="/api/v1/applications/app1/scheduled-tasks": return self.send([{"name":"auto-update","frequency":"daily"}])
        if p=="/version": return self.send({"Version":"28"})
        if p=="/containers/json": return self.send([{"Id":"c1","Names":["/uptime-kuma-svc1"],"Labels":{"com.docker.compose.project":"svc1","coolify.auto-update":"false"}},{"Id":"c2","Names":["/other"],"Labels":{}},
            {"Id":"c3","Names":["/vaultwarden-svc3"],"Labels":{"com.docker.compose.project":"svc3","coolify.auto-update":"*/5 * * * *"}},
            {"Id":"c4","Names":["/gotify-svc4"],"Labels":{"com.docker.compose.project":"svc4","coolify.auto-update":"true"}},
            {"Id":"c5","Names":["/myapp-app1"],"Labels":{"coolify.applicationId":"1","coolify.auto-update":"true"}}])
        if p=="/containers/c1/json": return self.send({"Config":{"Image":"louislam/uptime-kuma:2"},"Image":"sha256:img1"})
        if p=="/containers/c3/json": return self.send({"Config":{"Image":"vaultwarden/server:latest"},"Image":"sha256:img3"})
        if p=="/images/sha256:img3/json": return self.send({"RepoDigests":["vaultwarden/server@sha256:cccccccccccccccc"]})
        if p=="/images/sha256:img1/json": return self.send({"RepoDigests":["louislam/uptime-kuma@sha256:aaaaaaaaaaaaaaaa"]})
        self.send({"message":"nf"},404)
    def do_POST(self):
        restarts.append(self.path); self.send({"message":"queued"})
for port in (18080,12375):
    threading.Thread(target=HTTPServer(("127.0.0.1",port),H).serve_forever,daemon=True).start()
u.remote_digest = lambda *a: "sha256:bbbbbbbbbbbbbbbbbb"
st={}
found, ignored = u.discover()
assert found["svc1"]["source"]=="task" and found["svc1"]["enabled"]  # task wins over label=false
assert found["svc3"]=={"name":"vaultwarden","frequency":"*/5 * * * *","enabled":True,"source":"label","timezone":"Europe/Copenhagen"}  # TZ override
assert found["svc4"]["frequency"]=="daily" and "svc2" in found
assert ignored==["myapp-app1"]
CPH = u.zone("Europe/Copenhagen")
u.tick(datetime(2026,9,27,4,30,tzinfo=CPH), st)  # svc1 task + svc3 label due; svc4 (daily) not
print("restarts:", restarts)
assert sorted(restarts)==["/api/v1/services/svc1/restart?latest=true","/api/v1/services/svc3/restart?latest=true"]
u.tick(datetime(2026,9,27,4,31,tzinfo=CPH), st)
assert len(restarts)==2
assert u.coolify("GET", "/version") == (200, "4.2.1")
# TZ unset: each service follows its server's timezone from Coolify.
u.TZ_OVERRIDE = ""
found, _ = u.discover()
assert found["svc1"]["timezone"]=="Europe/Copenhagen" and found["svc3"]["timezone"]=="America/New_York"
assert u.LOG_TZ is u.zone("UTC")  # servers disagree -> UTC log stamps
UTC = u.zone("UTC")
restarts.clear()
u.tick(datetime(2026,9,27,2,30,tzinfo=UTC), {})  # 04:30 in Copenhagen: svc1 due
assert "/api/v1/services/svc1/restart?latest=true" in restarts
restarts.clear()
u.tick(datetime(2026,9,27,4,30,tzinfo=UTC), {})  # 06:30 in Copenhagen: svc1 not due
assert "/api/v1/services/svc1/restart?latest=true" not in restarts
print("apps:", u.discover_unsupported_apps())
print("harness ok")
