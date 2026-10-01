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
h, b = u.notify_request("https://discord.com/api/webhooks/1/abc", "Updating kuma", "nginx:1 -> 2", "success", "rename it")
payload = json.loads(b); e = payload["embeds"][0]
assert h["Content-Type"] == "application/json" and payload["allowed_mentions"] == {"parse": []}
assert e["title"] == "Updating kuma" and e["description"] == "```\nnginx:1 -> 2\n```" and e["color"] == u.LEVEL_COLORS["success"]
assert "url" not in e and e["fields"] == [{"name": "Note", "value": "rename it"}] and "coolify-watchtower" in e["footer"]["text"]
e = json.loads(u.notify_request("https://discord.com/api/webhooks/1/abc", "t", "x")[1])["embeds"][0]
assert "url" not in e and "fields" not in e and e["color"] == u.LEVEL_COLORS["info"]
assert u.notify_request("https://ptb.discordapp.com/api/webhooks/1/abc", "t", "x")[0]["Content-Type"] == "application/json"
assert len(json.loads(u.notify_request("https://discord.com/api/webhooks/1/a", "t", "x" * 5000)[1])["embeds"][0]["description"]) <= 4096
h, b = u.notify_request("https://ntfy.sh/topic", "t", "body", "error", "a note")
assert h["Title"] == "coolify-watchtower: t" and h["Content-Type"] == "text/plain"
assert b == b"body\na note"
assert u.plural(1, "restart") == "1 restart" and u.plural(2, "restart") == "2 restarts"
assert u.notify_request("https://example.com/?next=https://discord.com/api/webhooks/1/a", "t", "x")[0]["Content-Type"] == "text/plain"
# version tags
assert u.best_version_tag(["latest", "4", "4.3.3"]) == "4.3.3"
assert u.best_version_tag(["latest", "main"]) is None
assert u.best_version_tag(["v2.1.0", "v2.1"]) == "v2.1.0"
assert u.best_version_tag(["1.0.0-rc.1", "1.0.0"]) == "1.0.0"
print("unit ok")

restarts=[]; all_restarts=[]
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send(self, obj, code=200):
        b=json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p=self.path
        if p=="/api/v1/version":  # real Coolify answers with plain text, not JSON
            self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers(); return self.wfile.write(b"4.2.1")
        if p=="/api/v1/services": return self.send([{"uuid":"svc1","name":"uptime-kuma","server_id":0,"status":"running:healthy"},{"uuid":"svc2","name":"pocket-id","server_id":0},{"uuid":"svc3","name":"vaultwarden","server_id":1},{"uuid":"svc4","name":"gotify","server_id":0}])
        if p=="/api/v1/services/svc1/scheduled-tasks": return self.send([{"name":"auto-update","frequency":"30 4 * * *","enabled":True}])
        if p=="/api/v1/services/svc2/scheduled-tasks": return self.send([{"name":"Auto-Update","frequency":"hourly","enabled":True,"command":"true"},{"name":"Watchtower","frequency":"daily","enabled":False,"command":"true healthcheck=false"}])
        if p in ("/api/v1/services/svc3/scheduled-tasks","/api/v1/services/svc4/scheduled-tasks"): return self.send([])
        if p=="/api/v1/services/svc1": return self.send({"uuid":"svc1","applications":[{"uuid":"a1","name":"web","status":"running:unhealthy"},{"uuid":"a2","name":"worker","status":"running:healthy"}],"databases":[{"uuid":"d1","name":"db","status":"running:healthy"}]})
        if p=="/api/v1/services/svc4": return self.send({"uuid":"svc4","applications":[{"uuid":"g1","name":"gotify","status":"running:unhealthy"}],"databases":[]})
        if p=="/api/v1/deployments": return self.send([{"application_name":"busy-app","status":"in_progress"}])
        if p=="/api/v1/servers": return self.send([{"name":"localhost","settings":{"server_id":0,"server_timezone":"Europe/Copenhagen"}},{"name":"remote","settings":{"server_id":1,"server_timezone":"America/New_York"}}])
        if p=="/api/v1/applications": return self.send([{"uuid":"app1","name":"myapp","build_pack":"nixpacks"},{"uuid":"app2","name":"kuma-app","build_pack":"dockerimage"}])
        if p=="/api/v1/applications/app2/scheduled-tasks": return self.send([{"name":"auto-update","frequency":"30 4 * * *","enabled":True}])
        if p=="/api/v1/applications/app1/scheduled-tasks": return self.send([{"name":"auto-update","frequency":"daily"}])
        if p=="/version": return self.send({"Version":"28"})
        if p=="/containers/json": return self.send([{"Id":"c1","Names":["/uptime-kuma-svc1"],"Labels":{"com.docker.compose.project":"svc1","coolify.auto-update":"false"}},{"Id":"c2","Names":["/other"],"Labels":{}},
            {"Id":"c3","Names":["/vaultwarden-svc3"],"Labels":{"com.docker.compose.project":"svc3","coolify.watchtower":"*/5 * * * *","coolify.watchtower.healthcheck":"False"}},
            {"Id":"c4","Names":["/gotify-svc4"],"Labels":{"com.docker.compose.project":"svc4","coolify.auto-update":"true"}},
            {"Id":"c5","Names":["/myapp-app1"],"Labels":{"coolify.applicationId":"1","coolify.auto-update":"true"}},
            {"Id":"c6","Names":["/app2-20260928T111330"],"Labels":{"coolify.applicationId":"2","coolify.type":"application"}}])
        if p=="/containers/c6/json": return self.send({"Config":{"Image":"louislam/uptime-kuma:2"},"Image":"sha256:img1"})
        if p=="/containers/c1/json": return self.send({"Config":{"Image":"louislam/uptime-kuma:2"},"Image":"sha256:img1"})
        if p=="/containers/c3/json": return self.send({"Config":{"Image":"vaultwarden/server:latest"},"Image":"sha256:img3"})
        if p=="/images/sha256:img3/json": return self.send({"RepoDigests":["vaultwarden/server@sha256:cccccccccccccccc"]})
        if p=="/images/sha256:img1/json": return self.send({"RepoDigests":["louislam/uptime-kuma@sha256:aaaaaaaaaaaaaaaa"]})
        self.send({"message":"nf"},404)
    def do_POST(self):
        restarts.append(self.path); all_restarts.append(self.path); self.send({"message":"queued"})
for port in (18080,12375):
    threading.Thread(target=HTTPServer(("127.0.0.1",port),H).serve_forever,daemon=True).start()
u.remote_digest = lambda *a: "sha256:bbbbbbbbbbbbbbbbbb"
u.tag_versions = lambda registry, repo, digests: {"sha256:bbbbbbbbbbbbbbbbbb": "2.1.0"} if "uptime" in repo else {}
st={}
found, ignored = u.discover()
assert found["svc1"]["source"]=="task" and found["svc1"]["enabled"]  # task wins over label=false
assert found["svc3"]=={"name":"vaultwarden","frequency":"*/5 * * * *","enabled":True,"source":"label","timezone":"Europe/Copenhagen","kind":"service","healthcheck":False}  # TZ override
assert found["svc4"]["frequency"]=="daily" and "svc2" in found
assert ignored==["myapp-app1"]
CPH = u.zone("Europe/Copenhagen")
u.socket.gethostname = lambda: "c1"  # we run in svc1's container, so svc1 restarts last
u.tick(datetime(2026,9,27,4,30,tzinfo=CPH), st)
# svc1 task, svc3 label and app2 task are due; svc4 (daily) is not; svc1 is our own service, so it goes last
assert restarts==["/api/v1/deploy?uuid=app2","/api/v1/services/svc3/restart?latest=true","/api/v1/services/svc1/restart?latest=true"], restarts
print("restarts:", restarts)
assert len(restarts)==3
u.tick(datetime(2026,9,27,4,31,tzinfo=CPH), st)
assert len(restarts)==3  # nothing new due at 04:31
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
# Docker Image application: found via its task, matched by `<uuid>-<timestamp>` container name, updated via /deploy
found, _ = u.discover()
assert found["app2"]["kind"] == "application" and found["app2"]["source"] == "task", found["app2"]
assert "app1" not in found  # nixpacks application: not supported
assert u.owner_uuid({"Names": ["/app2-20260928T111330"]}, {"app2"}) == "app2"
assert u.discover_unsupported_apps() == ["myapp"]
# Log lines: services name the container's role, applications nothing extra
lines = []; orig_log = u.log; u.log = lines.append
u.check_service("app2", found["app2"], allow_restart=False)
u.check_service("svc1", found["svc1"], allow_restart=False)
u.log = orig_log
assert any(l.startswith("[kuma-app] louislam/uptime-kuma:2 UPDATE") for l in lines), lines
assert any(l.startswith("[uptime-kuma] uptime-kuma: louislam/uptime-kuma:2 UPDATE") for l in lines), lines
assert "/api/v1/deploy?uuid=app2" in all_restarts, all_restarts
assert u.describe_change("docker.io", "louislam/uptime-kuma", {"sha256:aaaaaaaaaaaaaaaa"}, "sha256:bbbbbbbbbbbbbbbbbb") == "aaaaaaaaaaaa -> 2.1.0"
# Self-update: we run in svc4 (label "true"). Our own label is ignored; SELF_UPDATE decides.
u.socket.gethostname = lambda: "c4"
found, _ = u.discover()
assert found["svc4"]["source"] == "self" and found["svc4"]["frequency"] == "daily" and found["svc4"]["self"], found["svc4"]
u.SELF_UPDATE = "false"
assert u.discover()[0]["svc4"]["enabled"] is False
u.SELF_UPDATE = "0 5 * * *"
assert u.discover()[0]["svc4"]["frequency"] == "0 5 * * *"
u.socket.gethostname = lambda: "c1"  # a task on our own service still wins
assert u.discover()[0]["svc1"]["source"] == "task"
# Opt-in change announcements
A = {"name": "kuma", "kind": "service", "enabled": True, "source": "task", "frequency": "daily", "timezone": "Europe/Copenhagen"}
B = {"name": "gotify", "kind": "application", "enabled": True, "source": "label", "frequency": "30 4 * * *", "timezone": "Europe/Copenhagen"}
assert u.opt_in_changes({"a": A}, {"a": A, "b": B}) == ["+ gotify (application): 30 4 * * * Europe/Copenhagen, via label"]
assert u.opt_in_changes({"a": A, "b": B}, {"a": A}) == ["- gotify (application): no longer opted in"]
assert u.opt_in_changes({"a": A}, {"a": dict(A, frequency="*/5 * * * *", enabled=False)}) == ["~ kuma: schedule daily -> */5 * * * *, paused"]
assert u.opt_in_changes({"a": A}, {"a": dict(A, source="label")}) == ["~ kuma: opted in via task -> label"]
assert u.opt_in_changes({"a": A}, {"a": A}) == []

sent = []; orig_notify = u.notify; u.notify = lambda title, body="", *a, **k: sent.append(body)
st2 = {}
u.DISCOVERY_COMPLETE = True
u.announce_changes({"a": A}, st2)                # startup: baseline only
u.announce_changes({"a": A}, st2)
assert sent == []
u.announce_changes({"a": A, "b": B}, st2)        # new service seen once: wait
assert sent == []
u.announce_changes({"a": A, "b": B}, st2)        # seen twice: announce
assert sent == ["+ gotify (application): 30 4 * * * Europe/Copenhagen, via label"], sent
u.announce_changes({"a": A}, st2)                # gone for one minute only (restarting): no announcement
u.announce_changes({"a": A, "b": B}, st2)
u.announce_changes({"a": A, "b": B}, st2)
assert len(sent) == 1, sent
u.DISCOVERY_COMPLETE = False                     # a failed API call must not look like a removal
u.announce_changes({}, st2); u.announce_changes({}, st2)
assert len(sent) == 1, sent
u.DISCOVERY_COMPLETE = True
u.notify = orig_notify
# Post-update health check
u.socket.gethostname = lambda: "not-a-container"  # earlier tests made svc1 our own service
found, _ = u.discover()
assert found["svc3"]["healthcheck"] is False and "legacy" not in found["svc3"]  # coolify.watchtower.healthcheck=false
assert found["svc1"]["healthcheck"] is True and found["svc1"]["legacy"] == "task"  # legacy task name "auto-update"
assert found["svc4"]["legacy"] == "label"                                          # legacy label coolify.auto-update
# svc2 has both a legacy and a current task: the current one wins, with its command option
assert found["svc2"]["frequency"] == "daily" and "legacy" not in found["svc2"] and found["svc2"]["healthcheck"] is False, found["svc2"]
assert u.STATUSES["svc1"] == "running:healthy"
assert u.status_ok("running:healthy") and u.status_ok("running:unknown") and u.status_ok("running:healthy:excluded")
assert not u.status_ok("running:unhealthy") and not u.status_ok("exited") and not u.status_ok("degraded:unhealthy") and not u.status_ok("")

from datetime import timedelta
sent = []; orig_notify = u.notify; u.notify = lambda title, body="", *a, **k: sent.append((title, body))
T0 = datetime(2026, 9, 29, 4, 30, tzinfo=u.zone("UTC"))
hs = {}
u.watch_update("svc3", found["svc3"], "x 1 -> 2", T0, hs)                  # opted out
u.watch_update("me", {"name": "cw", "self": True}, "x", T0, hs)            # ourselves
assert hs.get("pending", {}) == {}
u.watch_update("svc1", found["svc1"], "louislam/uptime-kuma:2 2.5.4 -> 2.5.5", T0, hs)
u.STATUSES.clear(); u.STATUSES.update({"svc1": "running:healthy"})      # stale pre-restart status
u.follow_up_updates(T0 + timedelta(minutes=1), hs)                        # within the grace period: ignored
assert sent == [] and "svc1" in hs["pending"], (sent, hs)
u.follow_up_updates(T0 + timedelta(minutes=2), hs)                        # first ok
assert sent == []
u.follow_up_updates(T0 + timedelta(minutes=3), hs)                        # second ok in a row: healthy
assert len(sent) == 1 and "healthy after the update" in sent[0][0] and "2.5.5" in sent[0][1], sent
assert "svc1" not in hs["pending"]

sent.clear()
u.watch_update("svc1", found["svc1"], "louislam/uptime-kuma:2 2.5.4 -> 2.5.5", T0, hs)
u.STATUSES.update({"svc1": "running:unhealthy"})
for m in range(2, 10):
    u.follow_up_updates(T0 + timedelta(minutes=m), hs)
assert sent == []
u.follow_up_updates(T0 + timedelta(minutes=10), hs)                       # timeout: unhealthy
assert len(sent) == 1 and "unhealthy after the update" in sent[0][0] and "running:unhealthy" in sent[0][1], sent

sent.clear()
u.watch_update("svc1", found["svc1"], "x", T0, hs)
u.STATUSES.update({"svc1": "running:unknown"})                            # no health check defined
u.follow_up_updates(T0 + timedelta(minutes=2), hs); u.follow_up_updates(T0 + timedelta(minutes=3), hs)
assert "no health check" in sent[0][1], sent
u.notify = orig_notify
# Names and options
assert u.command_options("true healthcheck=false") == {"healthcheck": "false"}
assert u.command_options("true") == {} and u.command_options("") == {}
assert u.command_options("true Healthcheck=OFF foo") == {"healthcheck": "OFF"}
assert u.option_bool("false") is False and u.option_bool("OFF") is False and u.option_bool("true") is True and u.option_bool("") is True
assert "rename the scheduled task 'auto-update' to 'watchtower'" in u.legacy_note({"legacy": "task"})
assert "rename the label 'coolify.auto-update' to 'coolify.watchtower'" in u.legacy_note({"legacy": "label"})
assert u.legacy_note({}) == ""
assert {"watchtower", "auto-update"} <= u.TASK_NAMES and u.OPT_IN_LABELS[0] == "coolify.watchtower"
assert u.opt_in_changes({"a": A}, {"a": dict(A, healthcheck=False)}) == ["~ kuma: health check off"]
# Self-healing
assert u.status_broken("running:unhealthy") and u.status_broken("degraded:unhealthy")
for ok in ("running:healthy", "running:unknown", "exited", "paused:unknown", "starting:unknown", "starting:unhealthy", "running:unhealthy:excluded", ""):
    assert not u.status_broken(ok), ok
assert u.part_broken("running:unhealthy") and u.part_broken("restarting:unknown") and u.part_broken("exited") and not u.part_broken("running:healthy")

sent = []; orig_notify = u.notify; u.notify = lambda title, body="", *a, **k: sent.append(title)
svc = lambda name, **kw: dict({"name": name, "kind": "service", "enabled": True, "source": "task", "frequency": "daily", "timezone": "UTC", "healthcheck": True}, **kw)
hfound = {"svc1": svc("immich"), "svc4": svc("gotify"), "svc2": svc("off", healthcheck=False), "me": svc("cw", self=True)}
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=u.zone("UTC"))
def minute(m, statuses, st):
    u.STATUSES.clear(); u.STATUSES.update(statuses); u.DISCOVERY_COMPLETE = True
    all_restarts.clear(); u.heal(hfound, T0 + timedelta(minutes=m), st)
    return list(all_restarts)

hs = {}
bad = {"svc1": "running:unhealthy", "svc4": "running:healthy", "svc2": "degraded:unhealthy", "me": "degraded:unhealthy"}
for m in range(4):
    assert minute(m, bad, hs) == []                                  # not yet HEAL_AFTER (5) minutes
assert minute(4, bad, hs) == ["/api/v1/services/svc1/applications/a1/restart"], all_restarts  # only the failing part
assert "unhealthy, restarting" in sent[-1]
for m in range(5, 19):
    assert minute(m, bad, hs) == []                                  # waiting HEAL_RETRY_AFTER (15) minutes
assert minute(19, bad, hs) == ["/api/v1/services/svc1/applications/a1/restart"]   # attempt 2 of 2
for m in range(20, 34):
    minute(m, bad, hs)
minute(34, bad, hs)
assert "still unhealthy after 2 restarts" in sent[-1], sent
assert minute(60, bad, hs) == []                                     # given up: no more restarts
ok = dict(bad, svc1="running:healthy")
minute(61, ok, hs); minute(62, ok, hs)
assert "recovered" in sent[-1] and hs["heal"]["svc1"]["restarts"] == 0, sent
bodies = []; u.notify = lambda title, body="", *a, **k: (sent.append(title), bodies.append(body))
hs = {}                                                              # a new release fixed it: credit the update
for m in range(5):
    minute(m, bad, hs)
assert hs["heal"]["svc1"]["restarts"] == 1
hs["heal"]["svc1"]["updated"] = "immich: 1.0 -> 1.1"
minute(10, ok, hs); minute(11, ok, hs)
assert "recovered" in sent[-1] and bodies[-1].startswith("Healthy again after the update.\nimmich: 1.0 -> 1.1"), bodies
hs = {}                                                              # one restart fixed it: singular
for m in range(5):
    minute(m, bad, hs)
minute(10, ok, hs); minute(11, ok, hs)
assert bodies[-1].startswith("Healthy again after 1 restart."), bodies
u.notify = lambda title, body="", *a, **k: sent.append(title)

sent.clear(); hs = {}                                                # starting/exited are never restarted
for m in range(10):
    assert minute(m, dict(bad, svc1="starting:unknown"), hs) == [] and minute(m, dict(bad, svc1="exited"), hs) == []
assert sent == []

hs = {"pending": {"svc1": {}}}                                       # a post-update check is running: wait for it
for m in range(10):
    assert minute(m, bad, hs) == []

sent.clear(); hs = {}                                                # three at once: server problem, no restarts
hfound["svc5"] = svc("vault")
outage = dict(bad, svc4="running:unhealthy", svc5="degraded:unhealthy")
for m in range(10):
    assert minute(m, outage, hs) == []
assert len(sent) == 1 and "3 services unhealthy at once" in sent[0], sent
del hfound["svc5"]

hs = {}; u.SERVER_DOWN.add("svc1")                                   # unreachable server: stale status, hands off
for m in range(10):
    assert minute(m, bad, hs) == []
u.SERVER_DOWN.clear()

hs = {}; hfound["app9"] = dict(svc("busy-app"), kind="application")  # an application being deployed: wait
for m in range(10):
    r = minute(m, {"app9": "running:unhealthy"}, hs)
    assert "/api/v1/applications/app9/restart" not in r
del hfound["app9"]
# every part of the service is broken: restart the whole service (without pulling)
assert u.heal_restart("svc4", svc("gotify"))[0] == "the whole service" and all_restarts[-1] == "/api/v1/services/svc4/restart"
# a Docker Image application: its own restart endpoint (no pull)
assert u.heal_restart("app2", dict(svc("kuma-app"), kind="application"))[0] == "the application" and all_restarts[-1] == "/api/v1/applications/app2/restart"
u.notify = orig_notify
# Startup settings overview and leftover warnings
lines = u.settings_report()
row = lambda name: next(l for l in lines if l.split()[:1] == [name])
assert row("COOLIFY_URL").endswith("env") and row("HEAL_AFTER").endswith("default")   # env set by this test / image default
assert "set" in row("COOLIFY_TOKEN") and "t" not in row("COOLIFY_TOKEN").split()[1:2]  # the token itself is never shown
assert row("TZ").split()[-1] == "env"
os.environ["TASK_NAME"] = "auto-update"
assert any("TASK_NAME=auto-update looks like a leftover" in w for w in u.leftover_warnings())
del os.environ["TASK_NAME"]
assert u.leftover_warnings() == []
# Regression (seen on a real server): during the health-check start period Coolify reports
# "running:healthy", which gave a false "healthy after the update" and false "recovered" resets.
sent = []; orig_notify = u.notify; u.notify = lambda title, body="", *a, **k: sent.append(title)
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=u.zone("UTC"))
cfg = {"name": "testbed", "kind": "service", "healthcheck": True}
def at(minute, status, docker, st, f):
    u.STATUSES.clear(); u.STATUSES["tb"] = status
    u.DOCKER_HEALTH.clear(); u.DOCKER_HEALTH["tb"] = {True: "starting", False: "ok"}.get(docker, docker)
    u.DISCOVERY_COMPLETE = True; all_restarts.clear()
    f(T0 + timedelta(minutes=minute), st)
# post-update check: "healthy" while starting must not count
st = {}; u.watch_update("tb", cfg, "testbed 1.0.2 -> 1.0.3", T0, st)
for m in range(0, 3):
    at(m, "running:healthy", True, st, u.follow_up_updates)       # start period: looks healthy, isn't trusted
assert not any("healthy after the update" in x for x in sent), sent
for m in range(3, 11):
    at(m, "running:unhealthy", False, st, u.follow_up_updates)
assert any("looks unhealthy after the update" in x for x in sent), sent
# self-healing: a restart followed by a start period must not count as "recovered"
sent.clear(); st = {}; hf = {"tb": dict(cfg, enabled=True, source="task", frequency="daily", timezone="UTC")}
heal = lambda now, state: u.heal(hf, now, state)
for m in range(0, 5):
    at(m, "running:unhealthy", False, st, heal)                    # 5 bad minutes -> restart 1
for m in range(5, 8):
    at(m, "running:healthy", True, st, heal)                       # restarted: starting, "healthy"
for m in range(8, 11):
    at(m, "running:healthy", "unhealthy", st, heal)                # Docker: unhealthy, Coolify still stale "healthy"
for m in range(11, 25):
    at(m, "running:unhealthy", False, st, heal)                    # still broken -> restart 2 after 15 min
for m in range(25, 28):
    at(m, "running:healthy", True, st, heal)
for m in range(28, 31):
    at(m, "running:healthy", "unhealthy", st, heal)
for m in range(31, 45):
    at(m, "running:unhealthy", False, st, heal)
assert not any("recovered" in x for x in sent), sent
assert sum("restarting" in x for x in sent) == 2 and any("still unhealthy after 2 restarts" in x for x in sent), sent
u.notify = orig_notify
# Docker health from the container list
dh = u.docker_health([{"Names": ["/web-x1"], "Status": "Up 3 minutes (healthy)"},
                      {"Names": ["/worker-x1"], "Status": "Up 3 minutes (unhealthy)"},
                      {"Names": ["/web-x2"], "Status": "Up 9 seconds (health: starting)"},
                      {"Names": ["/worker-x2"], "Status": "Up 9 seconds (unhealthy)"},
                      {"Names": ["/app-x3"], "Status": "Up 2 hours"}], {"x1", "x2", "x3"})
assert dh == {"x1": "unhealthy", "x2": "starting", "x3": "ok"}, dh
print("apps:", u.discover_unsupported_apps())
print("harness ok")
