#!/usr/bin/env python3
"""coolify-watchtower - Coolify-native image auto-updater.

A Coolify service or Docker Image application opts in either with a Scheduled Task named
TASK_NAME (default "auto-update", command `true`), or with the container label
AUTO_UPDATE_LABEL (default "coolify.auto-update") in its compose file, whose
value is the schedule. The task wins if both exist.
This service reads tasks via the Coolify API and labels via Docker, and when one is due it
compares local image digests (read-only Docker via socket proxy) with the
registry. Only if something changed does it call Coolify's
`restart?latest=true` (services) or `/deploy` (Docker Image applications), so Coolify
itself performs the update.

Stdlib only.
"""
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

VERSION = "1.7.1"


def env_bool(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


COOLIFY_URL = os.environ.get("COOLIFY_URL", "http://coolify:8080").rstrip("/")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN", "").strip()
DOCKER_URL = os.environ.get("DOCKER_HOST", "tcp://socket-proxy:2375").replace("tcp://", "http://").rstrip("/")
TASK_NAME = os.environ.get("TASK_NAME", "auto-update").strip().lower()
AUTO_UPDATE_LABEL = os.environ.get("AUTO_UPDATE_LABEL", "coolify.auto-update").strip()
DEFAULT_SCHEDULE = os.environ.get("DEFAULT_SCHEDULE", "daily").strip()
DRY_RUN = env_bool("DRY_RUN", "false")
REPORT_ON_START = env_bool("REPORT_ON_START", "true")
NOTIFY_URL = os.environ.get("NOTIFY_URL", "").strip()
# Schedule for updating the updater's own service ("false" turns it off). Handled in code, not
# with a label, because Coolify doesn't interpolate variables inside compose labels.
SELF_UPDATE = os.environ.get("SELF_UPDATE", "daily").strip()
# Empty TZ: each service follows its server's timezone from Coolify (Servers -> General).
TZ_OVERRIDE = os.environ.get("TZ", "").strip()
# Touched every loop; the container health check fails if it goes stale.
HEARTBEAT = os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat")

ACCEPT_MANIFESTS = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])

# Coolify accepts these words as schedules in addition to 5-field cron.
CRON_ALIASES = {
    "every_minute": "* * * * *",
    "hourly": "0 * * * *",
    "daily": "0 0 * * *",
    "weekly": "0 0 * * 0",
    "monthly": "0 0 1 * *",
    "yearly": "0 0 1 1 *",
    "annually": "0 0 1 1 *",
}


# ---------------------------------------------------------------- utilities

_zones = {}
_warned = set()


def zone(name):
    """ZoneInfo for a timezone name. Empty or unknown names fall back to UTC (warned once)."""
    name = (name or "").strip() or "UTC"
    if name not in _zones:
        try:
            _zones[name] = ZoneInfo(name) if ZoneInfo else timezone.utc
        except Exception:
            print(f"WARN unknown timezone '{name}', using UTC", flush=True)
            _zones[name] = timezone.utc
    return _zones[name]


# Log timestamps: TZ if set, else the server's timezone when all servers agree, else UTC.
LOG_TZ = zone(TZ_OVERRIDE)


def log(msg):
    stamp = datetime.now(LOG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {msg}", flush=True)


def beat():
    try:
        with open(HEARTBEAT, "w") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        warn_once("heartbeat", f"cannot write heartbeat {HEARTBEAT}: {e}")


def warn_once(key, msg):
    if key not in _warned:
        _warned.add(key)
        log(f"WARN {msg}")


def http(method, url, headers=None, data=None, timeout=20):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


DISCORD_WEBHOOK = re.compile(r"https://([a-z]+\.)?discord(app)?\.com/api/webhooks/")


def notify_request(url, title, body):
    """Headers and payload for NOTIFY_URL: JSON for a Discord webhook, plain text otherwise (ntfy style)."""
    # Discord sits behind Cloudflare, which rejects urllib's default User-Agent.
    headers = {"User-Agent": f"coolify-watchtower/{VERSION}"}
    if DISCORD_WEBHOOK.match(url):
        content = f"**{title}**\n```\n{body}\n```"
        if len(content) > 2000:  # Discord's message limit
            content = content[:1990] + "\n...```"
        headers["Content-Type"] = "application/json"
        return headers, json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    headers.update({"Title": title, "Content-Type": "text/plain"})
    return headers, body.encode()


def notify(title, body):
    if not NOTIFY_URL:
        return
    try:
        headers, data = notify_request(NOTIFY_URL, title, body)
        st, _, resp = http("POST", NOTIFY_URL, headers, data)
        if st >= 300:
            log(f"WARN notify HTTP {st}: {resp[:200]!r}")
    except Exception as e:
        log(f"WARN notify failed: {e}")


def short(digest):
    return digest.split(":", 1)[-1][:12] if digest else "?"


# ---------------------------------------------------------------- cron

def _field_match(field, value, lo, hi, is_dow=False):
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = int(part)
            b = hi if step > 1 else a
        for x in range(a, b + 1, step):
            if x == value or (is_dow and x == 7 and value == 0):
                return True
    return False


def cron_match(expr, dt):
    """Match a 5-field cron expression (numbers, *, ranges, lists, steps) or a Coolify alias."""
    e = (expr or "").strip().lower().lstrip("@")
    e = CRON_ALIASES.get(e, e)
    f = e.split()
    if len(f) != 5:
        raise ValueError(f"unsupported schedule '{expr}'")
    minute = _field_match(f[0], dt.minute, 0, 59)
    hour = _field_match(f[1], dt.hour, 0, 23)
    dom = _field_match(f[2], dt.day, 1, 31)
    month = _field_match(f[3], dt.month, 1, 12)
    dow = _field_match(f[4], dt.isoweekday() % 7, 0, 7, is_dow=True)
    # Standard cron: if both day-of-month and day-of-week are restricted, either may match.
    if f[2] != "*" and f[4] != "*":
        day = dom or dow
    else:
        day = dom and dow
    return minute and hour and month and day


# ---------------------------------------------------------------- coolify api

def coolify(method, path):
    st, _, body = http(method, f"{COOLIFY_URL}/api/v1{path}", {
        "Authorization": f"Bearer {COOLIFY_TOKEN}",
        "Accept": "application/json",
    })
    try:
        data = json.loads(body) if body else None
    except ValueError:  # e.g. /version answers with plain text
        data = body.decode(errors="replace").strip() or None
    return st, data


def label_schedule(value):
    """Label value -> (frequency, enabled). Empty/'true' means DEFAULT_SCHEDULE, 'false' pauses."""
    v = (value or "").strip()
    if v.lower() in ("", "true", "1", "yes", "on"):
        return DEFAULT_SCHEDULE, True
    if v.lower() in ("false", "0", "no", "off"):
        return v, False
    return v, True


def server_timezones():
    """{server_id: timezone name} from Coolify's server settings, or None if unavailable."""
    st, servers = coolify("GET", "/servers")
    if st != 200 or not isinstance(servers, list):
        return None
    result = {}
    for s in servers:
        settings = s.get("settings") or {}
        if settings.get("server_id") is not None:
            result[settings["server_id"]] = settings.get("server_timezone") or "UTC"
    return result


def set_log_tz(tzs):
    """Log in the servers' timezone when they all agree, else UTC."""
    global LOG_TZ
    LOG_TZ = zone(next(iter(set(tzs.values()))) if len(set(tzs.values())) == 1 else "UTC")


def discover():
    """Return ({service_uuid: {name, frequency, enabled, source, timezone}}, [ignored container names]).

    Opt-in is a scheduled task named TASK_NAME or a container carrying AUTO_UPDATE_LABEL.
    The task wins over the label. Labelled containers outside any service are ignored.
    Schedules use TZ if set, else the timezone of the server the service runs on."""
    st, services = coolify("GET", "/services")
    if st != 200 or not isinstance(services, list):
        log(f"ERROR listing services: HTTP {st} {services}")
        return None
    names = {s["uuid"]: s.get("name") or s["uuid"] for s in services if s.get("uuid")}
    server_of = {s["uuid"]: s.get("server_id") for s in services if s.get("uuid")}
    kind = {uuid: "service" for uuid in names}
    # Docker Image applications: Coolify's /deploy pulls their image (other build packs would rebuild).
    st, apps = coolify("GET", "/applications")
    for a in apps if st == 200 and isinstance(apps, list) else []:
        if a.get("uuid") and a.get("build_pack") == "dockerimage":
            names[a["uuid"]] = a.get("name") or a["uuid"]
            kind[a["uuid"]] = "application"

    tzs = {}
    if not TZ_OVERRIDE:
        tzs = server_timezones()
        if tzs is None:
            warn_once("servers", "cannot read server timezones from Coolify (GET /servers) - using UTC")
            tzs = {}
        set_log_tz(tzs)

    single_tz = next(iter(set(tzs.values()))) if len(set(tzs.values())) == 1 else None

    def tz_for(uuid):
        # Applications don't expose their server to a read token; fall back to the one shared timezone.
        return TZ_OVERRIDE or tzs.get(server_of.get(uuid)) or single_tz or "UTC"

    found = {}
    for uuid, name in names.items():
        st, tasks = coolify("GET", f"/{kind[uuid]}s/{uuid}/scheduled-tasks")
        if st != 200 or not isinstance(tasks, list):
            continue
        for t in tasks:
            if (t.get("name") or "").strip().lower() == TASK_NAME:
                found[uuid] = {
                    "name": name,
                    "frequency": t.get("frequency") or "",
                    "enabled": bool(t.get("enabled", True)),
                    "source": "task",
                    "timezone": tz_for(uuid),
                    "kind": kind[uuid],
                }

    try:
        containers = docker_get("/containers/json")
    except Exception as e:
        log(f"WARN container discovery: {e}")
        containers = []
    me = own_service(containers, names)

    ignored = []
    if AUTO_UPDATE_LABEL:
        for c in containers:
            labels = c.get("Labels") or {}
            if AUTO_UPDATE_LABEL not in labels:
                continue
            uuid = owner_uuid(c, names)
            if not uuid:
                ignored.append((c.get("Names") or ["?"])[0].lstrip("/"))
                continue
            if uuid in found or uuid == me:  # our own schedule comes from SELF_UPDATE
                continue
            frequency, enabled = label_schedule(labels[AUTO_UPDATE_LABEL])
            found[uuid] = {"name": names[uuid], "frequency": frequency, "enabled": enabled, "source": "label",
                           "timezone": tz_for(uuid), "kind": kind[uuid]}

    if me and me not in found:
        frequency, enabled = label_schedule(SELF_UPDATE)
        found[me] = {"name": names[me], "frequency": frequency, "enabled": enabled, "source": "self",
                     "timezone": tz_for(me), "kind": kind[me]}
    if me in found:
        found[me]["self"] = True
    return found, sorted(ignored)


def discover_unsupported_apps():
    """Names of non-Docker-Image applications that carry an auto-update task (reported, not updated)."""
    st, apps = coolify("GET", "/applications")
    if st != 200 or not isinstance(apps, list):
        return []
    names = []
    for a in apps:
        if a.get("build_pack") == "dockerimage":
            continue  # supported
        st, tasks = coolify("GET", f"/applications/{a.get('uuid')}/scheduled-tasks")
        if st == 200 and isinstance(tasks, list):
            if any((t.get("name") or "").strip().lower() == TASK_NAME for t in tasks):
                names.append(a.get("name") or a.get("uuid"))
    return names


def restart_resource(uuid, kind):
    """Have Coolify pull the latest images and restart: services via restart?latest=true,
    Docker Image applications via /deploy (which pulls, and does a rolling update)."""
    path = f"/services/{uuid}/restart?latest=true" if kind == "service" else f"/deploy?uuid={uuid}"
    st, data = coolify("POST", path)
    if st == 405:  # Coolify < 4.2.0 used GET
        st, data = coolify("GET", path)
    return st, data


# ---------------------------------------------------------------- docker (read-only)

def docker_get(path):
    st, _, body = http("GET", f"{DOCKER_URL}{path}")
    if st != 200:
        raise RuntimeError(f"docker GET {path} -> HTTP {st}")
    return json.loads(body)


def own_service(containers, uuids):
    """The service uuid this updater runs in (Docker sets the hostname to the container ID), or None."""
    host = socket.gethostname()
    for c in containers:
        if host and (c.get("Id") or "").startswith(host):
            return owner_uuid(c, uuids)
    return None


def owner_uuid(container, uuids):
    """The resource uuid (from uuids) a container belongs to: compose project == uuid, a service
    container named `<name>-<uuid>`, or an application container named `<uuid>` / `<uuid>-<timestamp>`."""
    project = (container.get("Labels") or {}).get("com.docker.compose.project")
    if project in uuids:
        return project
    for n in container.get("Names") or []:
        n = n.lstrip("/")
        for uuid in uuids:
            if n == uuid or n.endswith("-" + uuid) or n.startswith(uuid + "-"):
                return uuid
    return None


def service_containers(uuid):
    """Running containers belonging to a Coolify service."""
    return [c for c in docker_get("/containers/json") if owner_uuid(c, {uuid}) == uuid]


# ---------------------------------------------------------------- registry

def parse_ref(ref):
    """'nginx' -> ('docker.io', 'library/nginx', 'latest'). None if pinned by digest/ID."""
    if not ref or "@" in ref or ref.startswith("sha256:"):
        return None
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
    if registry == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return registry, repo, tag


def _registry_token(challenge):
    if not challenge or not challenge.lower().startswith("bearer"):
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", None)
    if not realm:
        return None
    query = urllib.parse.urlencode(params)
    st, _, body = http("GET", realm + ("?" + query if query else ""))
    if st != 200:
        return None
    data = json.loads(body)
    return data.get("token") or data.get("access_token")


def registry_request(method, registry, path, headers=None):
    """Request against a registry's /v2 API, doing the anonymous bearer-token flow on 401."""
    host = "registry-1.docker.io" if registry == "docker.io" else registry
    url = f"https://{host}/v2/{path}"
    headers = dict(headers or {})
    st, h, body = http(method, url, headers)
    if st == 401:
        token = _registry_token(h.get("WWW-Authenticate", ""))
        if token:
            headers["Authorization"] = f"Bearer {token}"
            st, h, body = http(method, url, headers)
    return st, h, body


def remote_digest(registry, repo, tag):
    st, h, _ = registry_request("HEAD", registry, f"{repo}/manifests/{tag}", {"Accept": ACCEPT_MANIFESTS})
    if st != 200:
        raise RuntimeError(f"registry HTTP {st} (private registry or missing tag?)")
    digest = h.get("Docker-Content-Digest")
    if not digest:
        raise RuntimeError("registry returned no Docker-Content-Digest")
    return digest


VERSION_TAG = re.compile(r"v?\d+(\.\d+)*([-+][0-9A-Za-z.-]+)?")


def best_version_tag(tags):
    """The most specific version-looking tag: '4.3.3' over '4', and never 'latest'."""
    candidates = [t for t in tags if VERSION_TAG.fullmatch(t)]
    if not candidates:
        return None
    def key(t):
        core = t.lstrip("v").split("-", 1)[0].split("+", 1)[0]
        nums = [int(x) for x in core.split(".")]
        return (len(nums), "-" not in t, nums)  # more parts first, releases over pre-releases
    return max(candidates, key=key)


def tag_versions(registry, repo, digests):
    """{digest: version tag} for the given digests, found by matching tags to digests. Best effort."""
    by_digest = {}
    if registry == "docker.io":
        # Docker Hub's own API lists tags with their digests in one request.
        url = f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&ordering=last_updated"
        st, _, body = http("GET", url, {"User-Agent": f"coolify-watchtower/{VERSION}"})
        if st == 200:
            for t in json.loads(body).get("results") or []:
                if t.get("digest"):
                    by_digest.setdefault(t["digest"], []).append(t["name"])
    else:
        tags, path = [], f"{repo}/tags/list?n=1000"
        for _ in range(20):  # follow Link pagination; signature tags can fill many pages
            st, h, body = registry_request("GET", registry, path)
            if st != 200:
                break
            tags += [t for t in json.loads(body).get("tags") or [] if VERSION_TAG.fullmatch(t)]
            link = re.search(r"<(?:https?://[^/]+)?/v2/([^>]+)>;\s*rel=\"?next", h.get("Link") or "")
            if not link:
                break
            path = link.group(1)
        if tags:
            tags.sort(key=lambda t: [int(x) for x in re.findall(r"\d+", t)], reverse=True)
            for t in tags[:30]:  # newest versions only, one HEAD request each
                try:
                    by_digest.setdefault(remote_digest(registry, repo, t), []).append(t)
                except Exception:
                    continue
    result = {}
    for d in digests:
        tag = best_version_tag(by_digest.get(d, []))
        if tag:
            result[d] = tag
    return result


def describe_change(registry, repo, local, remote):
    """'4.3.1 -> 4.3.3' when versions can be found, else short digests."""
    try:
        names = tag_versions(registry, repo, set(local) | {remote})
    except Exception:
        names = {}
    old = next((names[d] for d in sorted(local) if d in names), None) or short(sorted(local)[0])
    new = names.get(remote) or short(remote)
    return f"{old} -> {new}"


# ---------------------------------------------------------------- core

def check_service(uuid, cfg, allow_restart):
    name = cfg["name"]
    try:
        containers = service_containers(uuid)
    except Exception as e:
        log(f"[{name}] ERROR docker: {e}")
        return
    if not containers:
        log(f"[{name}] no running containers found for uuid {uuid} - skipped")
        return

    changed, seen = [], set()
    for c in containers:
        cname = (c.get("Names") or ["?"])[0].lstrip("/")
        # Normal lines name the container's role: its compose service name for services (the part
        # before -<uuid>), nothing for an application (one container). Errors keep the full name.
        role = "" if cfg.get("kind") == "application" else cname.removesuffix("-" + uuid)
        who = f"{role}: " if role else ""
        try:
            inspect = docker_get(f"/containers/{c['Id']}/json")
            ref = (inspect.get("Config") or {}).get("Image", "")
            parsed = parse_ref(ref)
            if not parsed:
                log(f"[{name}] {who}'{ref}' pinned by digest/ID - skipped")
                continue
            if ref in seen:
                continue
            seen.add(ref)
            image = docker_get(f"/images/{inspect['Image']}/json")
            local = {d.split("@", 1)[1] for d in image.get("RepoDigests") or [] if "@" in d}
            if not local:
                log(f"[{name}] {who}{ref} has no registry digest (locally built?) - skipped")
                continue
            remote = remote_digest(*parsed)
            if remote in local:
                log(f"[{name}] {who}{ref} up to date ({short(remote)})")
            else:
                change = describe_change(parsed[0], parsed[1], local, remote)
                digests = f"{short(sorted(local)[0])} -> {short(remote)}"
                log(f"[{name}] {who}{ref} UPDATE {change}" + (f" ({digests})" if change != digests else ""))
                changed.append(f"{ref} {change}")
        except Exception as e:
            log(f"[{name}] {cname}: ERROR {e}")

    if not changed:
        return
    summary = "\n".join(changed)
    if not allow_restart:
        log(f"[{name}] update available (report only, no restart)")
        return
    kind = cfg.get("kind", "service")
    action = "restart?latest=true" if kind == "service" else "deploy"
    if DRY_RUN:
        log(f"[{name}] DRY_RUN - would call {action}")
        notify(f"coolify-watchtower (dry run): {name}", summary)
        return
    st, data = restart_resource(uuid, kind)
    if st in (200, 201, 202):
        log(f"[{name}] {action} queued")
        notify(f"coolify-watchtower: updating {name}", summary)
    else:
        log(f"[{name}] ERROR {action} HTTP {st} {data}")
        reason = data.get("message") if isinstance(data, dict) else data
        notify(f"coolify-watchtower: FAILED {name}", f"HTTP {st}: {reason}\n{summary}")


def print_table(found, ignored):
    if ignored:
        log(f"WARN label '{AUTO_UPDATE_LABEL}' on containers outside services and Docker Image "
            f"applications - ignored: {', '.join(ignored)}")
    if not found:
        log(f"Nothing opted in: no scheduled task named '{TASK_NAME}' or label '{AUTO_UPDATE_LABEL}'.")
        return
    log(f"Opted in with '{TASK_NAME}' task or '{AUTO_UPDATE_LABEL}' label:")
    width = max(len(v["name"]) for v in found.values())
    for uuid, v in sorted(found.items(), key=lambda kv: kv[1]["name"].lower()):
        state = "enabled " if v["enabled"] else "DISABLED"
        kind = "app" if v.get("kind") == "application" else "svc"
        print(f"    {v['name'].ljust(width)}  {kind}  {state}  {v['source']:<5}  {v['frequency']:<14}  "
              f"{v['timezone']:<18}  {uuid}", flush=True)


def tick(now, state):
    """Run one minute. `now` must be timezone-aware; each schedule is evaluated in its own timezone."""
    result = discover()
    if result is None:
        return
    found, ignored = result
    fingerprint = json.dumps([found, ignored], sort_keys=True)
    if fingerprint != state.get("fingerprint"):
        state["fingerprint"] = fingerprint
        print_table(found, ignored)
    due = []
    for uuid, cfg in found.items():
        if not cfg["enabled"]:
            continue
        try:
            if cron_match(cfg["frequency"], now.astimezone(zone(cfg["timezone"]))):
                due.append(uuid)
        except ValueError as e:
            key = (uuid, cfg["frequency"])
            if key not in state.setdefault("bad", set()):
                state["bad"].add(key)
                log(f"[{cfg['name']}] WARN {e} - ignored")
    # Restarting our own service kills this process, so do it last.
    due.sort(key=lambda uuid: found[uuid].get("self", False))
    for uuid in due:
        cfg = found[uuid]
        log(f"[{cfg['name']}] schedule '{cfg['frequency']}' ({cfg['timezone']}) due - checking")
        check_service(uuid, cfg, allow_restart=True)


def main():
    if COOLIFY_TOKEN and not TZ_OVERRIDE:
        try:  # so even the first log lines use the server's timezone
            set_log_tz(server_timezones() or {})
        except Exception:
            pass
    log(f"coolify-watchtower {VERSION} | coolify={COOLIFY_URL} docker={DOCKER_URL} "
        f"task='{TASK_NAME}' label='{AUTO_UPDATE_LABEL}' tz={TZ_OVERRIDE or 'from Coolify servers'} dry_run={DRY_RUN}")
    if not COOLIFY_TOKEN:
        log("ERROR COOLIFY_TOKEN is not set")
        raise SystemExit(1)
    # Retry instead of exiting, so a bad token or unreachable Coolify doesn't become a
    # restart loop hammering the API. No heartbeat meanwhile, so the container turns unhealthy.
    while True:
        try:
            st, ver = coolify("GET", "/version")
        except Exception as e:
            st, ver = None, e
        if st == 200:
            break
        hint = ""
        if st in (401, 403):
            hint = " - COOLIFY_TOKEN needs both 'read' and 'deploy', and this container's IP must be in Allowed API IPs"
        log(f"ERROR cannot reach Coolify API: HTTP {st} {ver}{hint}. Retrying in 60 s.")
        time.sleep(60)
    log(f"Coolify API OK (version {ver})")
    try:
        docker_get("/version")
        log("Docker (socket proxy) OK")
    except Exception as e:
        log(f"ERROR docker: {e}")
        raise SystemExit(1)

    apps = discover_unsupported_apps()
    if apps:
        log(f"WARN '{TASK_NAME}' tasks are only supported on services and Docker Image applications, "
            f"ignored: {', '.join(apps)}")

    state = {}
    beat()
    if REPORT_ON_START:
        found, ignored = discover() or ({}, [])
        state["fingerprint"] = json.dumps([found, ignored], sort_keys=True)
        print_table(found, ignored)
        for uuid, cfg in found.items():
            if cfg["enabled"]:
                check_service(uuid, cfg, allow_restart=False)
        log("Startup report done. Waiting for schedules.")

    last = None
    while True:
        beat()
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if now != last:
            last = now
            try:
                tick(now, state)
            except Exception as e:
                log(f"ERROR tick: {e}")
        time.sleep(max(1.0, 60 - (time.time() % 60) + 1))


if __name__ == "__main__":
    main()
