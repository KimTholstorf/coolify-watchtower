#!/usr/bin/env python3
"""coolify-watchtower - Coolify-native image auto-updater.

A Coolify service or Docker Image application opts in either with a Scheduled Task named
"watchtower" (command `true`, options as arguments: `true healthcheck=false`), or with the
container label "coolify.watchtower", whose value is the schedule. The task wins if both exist.
The legacy names "auto-update" and "coolify.auto-update" keep working.
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

VERSION = "1.8.8"


def env_bool(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


COOLIFY_URL = os.environ.get("COOLIFY_URL", "http://coolify:8080").rstrip("/")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN", "").strip()
DOCKER_URL = os.environ.get("DOCKER_HOST", "tcp://socket-proxy:2375").replace("tcp://", "http://").rstrip("/")
# Opt-in names: a scheduled task named "watchtower" or the label "coolify.watchtower". The legacy
# names "auto-update" / "coolify.auto-update" keep working, whatever TASK_NAME / AUTO_UPDATE_LABEL
# say (older compose files set those to the legacy names). The two settings add a custom name.
LEGACY_TASK, LEGACY_LABEL = "auto-update", "coolify.auto-update"
TASK_NAME = os.environ.get("TASK_NAME", "watchtower").strip().lower()
AUTO_UPDATE_LABEL = os.environ.get("AUTO_UPDATE_LABEL", "coolify.watchtower").strip()
TASK_NAMES = {"watchtower", TASK_NAME, LEGACY_TASK}
OPT_IN_LABELS = list(dict.fromkeys(["coolify.watchtower", AUTO_UPDATE_LABEL, LEGACY_LABEL]))  # first match wins
# The check after an update is on unless this label (or `healthcheck=false` in the task command) turns it off.
HEALTH_LABEL = "coolify.watchtower.healthcheck"
# After an update: wait this long before trusting Coolify's status, and give up on "healthy" after this long.
HEALTH_GRACE = 120
HEALTH_TIMEOUT = 600


def env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return int(default)


# Self-healing: restart opted-in resources that stay unhealthy or degraded (defaults shipped in the image;
# override in the compose file's environment). HEAL_AFTER and HEAL_RETRY_AFTER are minutes.
AUTO_HEAL = env_bool("AUTO_HEAL", "true")
HEAL_AFTER = env_int("HEAL_AFTER", "5")
HEAL_RETRY_AFTER = env_int("HEAL_RETRY_AFTER", "15")
HEAL_MAX_RESTARTS = env_int("HEAL_MAX_RESTARTS", "2")
HEAL_OUTAGE_THRESHOLD = env_int("HEAL_OUTAGE_THRESHOLD", "3")
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


# Coolify's status per service/application uuid from the last discover(), e.g. "running:healthy",
# and the resources whose server Coolify can't reach (their status is stale then).
STATUSES = {}
SERVER_DOWN = set()
# Docker's own health per resource, from the container list's Status ("(healthy)", "(unhealthy)",
# "(health: starting)"): "starting" if any container is starting, else "unhealthy" if any is
# unhealthy, else "ok". It's current, unlike Coolify's stored status, which lags behind and also
# reports a container in its start period ("running:starting") as "running:healthy".
DOCKER_HEALTH = {}


def docker_health(containers, uuids):
    health = {}
    for c in containers:
        uuid = owner_uuid(c, uuids)
        if not uuid:
            continue
        text = c.get("Status") or ""
        state = "starting" if "health: starting" in text else ("unhealthy" if "(unhealthy)" in text else "ok")
        rank = {"ok": 0, "unhealthy": 1, "starting": 2}
        if rank[state] > rank[health.get(uuid, "ok")]:
            health[uuid] = state
        else:
            health.setdefault(uuid, "ok")
    return health


def resource_ok(uuid):
    """Healthy by both Coolify's status and Docker's current health."""
    return status_ok(STATUSES.get(uuid, "")) and DOCKER_HEALTH.get(uuid, "ok") == "ok"

# False when the last discover() missed data (a failed API or Docker call), so its result
# mustn't be compared with earlier ones: a failed call would look like services disappearing.
DISCOVERY_COMPLETE = True


def option_bool(value):
    """'false'/'no'/'off'/'0' turn an option off; anything else (including empty) leaves it on."""
    return (value or "").strip().lower() not in ("false", "no", "off", "0")


def command_options(command):
    """Options passed as key=value arguments to the task's no-op command: `true healthcheck=false`."""
    return {k.strip().lower(): v for k, _, v in (a.partition("=") for a in (command or "").split()[1:]) if _}


def legacy_note(cfg):
    """A rename hint for resources opted in with a legacy name, or ''."""
    if cfg.get("legacy") == "task":
        return f"Note: rename the scheduled task '{LEGACY_TASK}' to 'watchtower'. The old name still works."
    if cfg.get("legacy") == "label":
        return f"Note: rename the label '{LEGACY_LABEL}' to 'coolify.watchtower'. The old name still works."
    return ""


def discover():
    """Return ({service_uuid: {name, frequency, enabled, source, timezone}}, [ignored container names]).

    Opt-in is a scheduled task named in TASK_NAMES or a container carrying one of OPT_IN_LABELS.
    The task wins over the label. Labelled containers outside any service are ignored.
    Schedules use TZ if set, else the timezone of the server the service runs on."""
    global DISCOVERY_COMPLETE
    DISCOVERY_COMPLETE = False
    complete = True
    st, services = coolify("GET", "/services")
    if st != 200 or not isinstance(services, list):
        log(f"ERROR listing services: HTTP {st} {services}")
        return None
    names = {s["uuid"]: s.get("name") or s["uuid"] for s in services if s.get("uuid")}
    server_of = {s["uuid"]: s.get("server_id") for s in services if s.get("uuid")}
    statuses = {s["uuid"]: s.get("status") or "" for s in services if s.get("uuid")}
    down = {s["uuid"] for s in services if s.get("uuid") and s.get("server_status") is False}
    kind = {uuid: "service" for uuid in names}
    # Docker Image applications: Coolify's /deploy pulls their image (other build packs would rebuild).
    st, apps = coolify("GET", "/applications")
    if st != 200 or not isinstance(apps, list):
        complete = False
    for a in apps if st == 200 and isinstance(apps, list) else []:
        if a.get("uuid") and a.get("build_pack") == "dockerimage":
            names[a["uuid"]] = a.get("name") or a["uuid"]
            kind[a["uuid"]] = "application"
            statuses[a["uuid"]] = a.get("status") or ""
            if a.get("server_status") is False:
                down.add(a["uuid"])

    tzs = {}
    if not TZ_OVERRIDE:
        tzs = server_timezones()
        if tzs is None:
            warn_once("servers", "cannot read server timezones from Coolify (GET /servers) - using UTC")
            tzs = {}
            complete = False
        set_log_tz(tzs)

    single_tz = next(iter(set(tzs.values()))) if len(set(tzs.values())) == 1 else None

    def tz_for(uuid):
        # Applications don't expose their server to a read token; fall back to the one shared timezone.
        return TZ_OVERRIDE or tzs.get(server_of.get(uuid)) or single_tz or "UTC"

    found = {}
    for uuid, name in names.items():
        st, tasks = coolify("GET", f"/{kind[uuid]}s/{uuid}/scheduled-tasks")
        if st != 200 or not isinstance(tasks, list):
            complete = False
            continue
        ours = [t for t in tasks if (t.get("name") or "").strip().lower() in TASK_NAMES]
        ours.sort(key=lambda t: (t.get("name") or "").strip().lower() == LEGACY_TASK)  # current name first
        if ours:
            t = ours[0]
            found[uuid] = {
                "name": name,
                "frequency": t.get("frequency") or "",
                "enabled": bool(t.get("enabled", True)),
                "source": "task",
                "timezone": tz_for(uuid),
                "kind": kind[uuid],
                "task_options": command_options(t.get("command")),
            }
            if (t.get("name") or "").strip().lower() == LEGACY_TASK:
                found[uuid]["legacy"] = "task"

    try:
        containers = docker_get("/containers/json")
    except Exception as e:
        log(f"WARN container discovery: {e}")
        containers = []
        complete = False
    me = own_service(containers, names)

    ignored = []
    for c in containers:
        labels = c.get("Labels") or {}
        label = next((l for l in OPT_IN_LABELS if l in labels), None)
        if not label:
            continue
        uuid = owner_uuid(c, names)
        if not uuid:
            ignored.append((c.get("Names") or ["?"])[0].lstrip("/"))
            continue
        if uuid == me:  # our own schedule comes from SELF_UPDATE
            continue
        if uuid in found and not (found[uuid]["source"] == "label" and found[uuid].get("legacy") and label != LEGACY_LABEL):
            continue  # a task wins; a current label wins over a legacy one on another container
        frequency, enabled = label_schedule(labels[label])
        found[uuid] = {"name": names[uuid], "frequency": frequency, "enabled": enabled, "source": "label",
                       "timezone": tz_for(uuid), "kind": kind[uuid]}
        if label == LEGACY_LABEL:
            found[uuid]["legacy"] = "label"

    if me and me not in found:
        frequency, enabled = label_schedule(SELF_UPDATE)
        found[me] = {"name": names[me], "frequency": frequency, "enabled": enabled, "source": "self",
                     "timezone": tz_for(me), "kind": kind[me]}
    if me in found:
        found[me]["self"] = True

    # Health check after updates: the task command's `healthcheck=` wins, then the label, else on.
    health_labels = {}
    for c in containers:
        value = (c.get("Labels") or {}).get(HEALTH_LABEL)
        uuid = owner_uuid(c, names) if value is not None else None
        if uuid:
            health_labels[uuid] = option_bool(value)
    for uuid, cfg in found.items():
        task_value = cfg.pop("task_options", {}).get("healthcheck")
        cfg["healthcheck"] = option_bool(task_value) if task_value is not None else health_labels.get(uuid, True)
        if cfg.get("legacy"):
            warn_once(("legacy", uuid, cfg["legacy"]), f"[{cfg['name']}] {legacy_note(cfg)}")

    STATUSES.clear()
    STATUSES.update(statuses)
    SERVER_DOWN.clear()
    SERVER_DOWN.update(down)
    DOCKER_HEALTH.clear()
    DOCKER_HEALTH.update(docker_health(containers, names))
    DISCOVERY_COMPLETE = complete
    return found, sorted(ignored)


def discover_unsupported_apps():
    """Names of non-Docker-Image applications that carry a watchtower task (reported, not updated)."""
    st, apps = coolify("GET", "/applications")
    if st != 200 or not isinstance(apps, list):
        return []
    names = []
    for a in apps:
        if a.get("build_pack") == "dockerimage":
            continue  # supported
        st, tasks = coolify("GET", f"/applications/{a.get('uuid')}/scheduled-tasks")
        if st == 200 and isinstance(tasks, list):
            if any((t.get("name") or "").strip().lower() in TASK_NAMES for t in tasks):
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
        note = legacy_note(cfg)
        notify(f"coolify-watchtower: updating {name}", summary + (f"\n{note}" if note else ""))
        return summary
    else:
        log(f"[{name}] ERROR {action} HTTP {st} {data}")
        reason = data.get("message") if isinstance(data, dict) else data
        notify(f"coolify-watchtower: FAILED {name}", f"HTTP {st}: {reason}\n{summary}")


def print_table(found, ignored):
    if ignored:
        log(f"WARN watchtower label on containers outside services and Docker Image "
            f"applications - ignored: {', '.join(ignored)}")
    if not found:
        log("Nothing opted in: no scheduled task named 'watchtower' or label 'coolify.watchtower'.")
        return
    log("Opted in with a 'watchtower' task or 'coolify.watchtower' label:")
    width = max(len(v["name"]) for v in found.values())

    def row(*cols):
        name, kind, status, via, schedule, tz, health, uuid = cols
        print(f"    {name.ljust(width)}  {kind:<4}  {status:<7}  {via:<5}  {schedule:<14}  {tz:<18}  {health:<6}  {uuid}",
              flush=True)

    # TYPE: svc = service, app = Docker Image application. VIA: task, label, or self (SELF_UPDATE).
    # HEALTH: whether the status is checked after an update.
    row("NAME", "TYPE", "STATUS", "VIA", "SCHEDULE", "TIMEZONE", "HEALTH", "UUID")
    for uuid, v in sorted(found.items(), key=lambda kv: kv[1]["name"].lower()):
        health = "-" if v.get("self") else ("on" if v.get("healthcheck", True) else "off")
        row(v["name"], "app" if v.get("kind") == "application" else "svc",
            "enabled" if v["enabled"] else "paused", v["source"], v["frequency"], v["timezone"], health, uuid)


# ---------------------------------------------------------------- opt-in change announcements

WATCHED = ("name", "kind", "enabled", "source", "frequency", "timezone", "healthcheck")


def snapshot(found):
    return {uuid: {k: cfg.get(k) for k in WATCHED} for uuid, cfg in found.items()}


def opt_in_changes(old, new):
    """Human-readable lines for what differs between two snapshots."""
    by_name = lambda snap: (lambda uuid: snap[uuid]["name"].lower())
    lines = []
    for uuid in sorted(new.keys() - old.keys(), key=by_name(new)):
        n = new[uuid]
        lines.append(f"+ {n['name']} ({n['kind']}): {n['frequency']} {n['timezone']}, via {n['source']}"
                     + ("" if n["enabled"] else ", paused") + ("" if n.get("healthcheck") is not False else ", no health check"))
    for uuid in sorted(old.keys() - new.keys(), key=by_name(old)):
        lines.append(f"- {old[uuid]['name']} ({old[uuid]['kind']}): no longer opted in")
    for uuid in sorted(old.keys() & new.keys(), key=by_name(new)):
        o, n = old[uuid], new[uuid]
        diffs = []
        if o["name"] != n["name"]:
            diffs.append(f"renamed from {o['name']}")
        if (o["frequency"], o["timezone"]) != (n["frequency"], n["timezone"]):
            before = o["frequency"] if o["timezone"] == n["timezone"] else f"{o['frequency']} {o['timezone']}"
            after = n["frequency"] if o["timezone"] == n["timezone"] else f"{n['frequency']} {n['timezone']}"
            diffs.append(f"schedule {before} -> {after}")
        if o["enabled"] != n["enabled"]:
            diffs.append("resumed" if n["enabled"] else "paused")
        if o["source"] != n["source"]:
            diffs.append(f"opted in via {o['source']} -> {n['source']}")
        if (o.get("healthcheck") is not False) != (n.get("healthcheck") is not False):
            diffs.append("health check " + ("off" if n.get("healthcheck") is False else "on"))
        if diffs:
            lines.append(f"~ {n['name']}: " + ", ".join(diffs))
    return lines


def announce_changes(found, state):
    """Log and notify opt-in changes (added, removed, schedule, paused/resumed).

    The first complete discovery is only the baseline. A change is announced once it has looked
    the same in two complete discoveries in a row, so a service whose containers are briefly gone
    during a restart isn't reported as removed and re-added. Incomplete discoveries are skipped."""
    if not DISCOVERY_COMPLETE:
        return
    snap = snapshot(found)
    if "announced" not in state:
        state["announced"] = snap
    elif snap != state["announced"] and snap == state.get("last_snapshot"):
        lines = opt_in_changes(state["announced"], snap)
        for line in lines:
            log(f"Opt-in change: {line}")
        notify("coolify-watchtower: opt-ins changed", "\n".join(lines))
        state["announced"] = snap
    state["last_snapshot"] = snap


# ---------------------------------------------------------------- post-update health check

def status_ok(status):
    """Running and not unhealthy. 'running:unknown' (no health check) counts as ok."""
    return status.startswith("running") and "unhealthy" not in status


def follow_up_updates(now, state):
    """After an update, watch the resource's Coolify status and report once: healthy (running and not
    unhealthy in two checks in a row, after a grace period so the pre-restart status isn't trusted),
    or unhealthy when HEALTH_TIMEOUT passes without that."""
    if not DISCOVERY_COMPLETE:
        return  # no container list, so a starting container could look healthy
    pending = state.setdefault("pending", {})
    for uuid, p in list(pending.items()):
        elapsed = (now - p["since"]).total_seconds()
        if elapsed < HEALTH_GRACE:
            continue
        status = STATUSES.get(uuid, "")
        p["ok"] = p.get("ok", 0) + 1 if resource_ok(uuid) else 0
        p["last"] = status or "not found"
        if p["ok"] >= 2:
            note = " (no health check, so only running is known)" if "unknown" in status else ""
            log(f"[{p['name']}] healthy after the update: {status}{note}")
            notify(f"coolify-watchtower: \u2713 {p['name']} is healthy after the update", p["summary"] + note)
            del pending[uuid]
        elif elapsed >= HEALTH_TIMEOUT:
            minutes = round(elapsed / 60)
            log(f"[{p['name']}] WARN still not healthy {minutes} min after the update: {p['last']}")
            notify(f"coolify-watchtower: \u26a0 {p['name']} looks unhealthy after the update",
                   f"Coolify status after {minutes} min: {p['last']}\n{p['summary']}")
            del pending[uuid]


def watch_update(uuid, cfg, summary, now, state):
    """Register a queued update for the health follow-up, unless opted out or it's ourselves."""
    if cfg.get("self"):
        return  # we're the one being restarted
    if not cfg.get("healthcheck", True):
        log(f"[{cfg['name']}] no health check after the update (turned off for this service)")
        return
    state.setdefault("pending", {})[uuid] = {"name": cfg["name"], "summary": summary, "since": now}


# ---------------------------------------------------------------- self-healing

def status_broken(status):
    """Unhealthy or degraded (partly exited, crash-looping, dead). Never exited/paused/starting/unknown,
    which may be deliberate or just can't be judged, and never resources excluded from monitoring."""
    return not status.endswith(":excluded") and (status.startswith("running:unhealthy") or status.startswith("degraded"))


def part_broken(status):
    """A single container of a service: failing its health check, crash-looping, or exited."""
    return "unhealthy" in status or status.startswith(("restarting", "exited", "dead"))


def deployment_running(cfg):
    """True if Coolify has a deployment queued or in progress for this application (by name)."""
    if cfg.get("kind") != "application":
        return False
    st, deployments = coolify("GET", "/deployments")
    return st == 200 and isinstance(deployments, list) and any(d.get("application_name") == cfg["name"] for d in deployments)


def heal_restart(uuid, cfg):
    """Restart without pulling new images, only the failing parts of a service where they can be told apart.
    Returns (what was restarted, [(HTTP status, response)])."""
    if cfg.get("kind") == "application":
        return "the application", [coolify("POST", f"/applications/{uuid}/restart")]
    st, svc = coolify("GET", f"/services/{uuid}")
    parts = []
    if st == 200 and isinstance(svc, dict):
        for key in ("applications", "databases"):
            parts += [(key, p) for p in svc.get(key) or [] if p.get("uuid")]
    broken = [(key, p) for key, p in parts if part_broken(p.get("status") or "")]
    if not broken or len(broken) == len(parts):
        return "the whole service", [coolify("POST", f"/services/{uuid}/restart")]
    results = [coolify("POST", f"/services/{uuid}/{key}/{p['uuid']}/restart") for key, p in broken]
    return ", ".join(p.get("name") or p["uuid"] for _, p in broken), results


def heal(found, now, state):
    """Restart opted-in resources that have been unhealthy or degraded for HEAL_AFTER minutes.

    Skips our own service, resources with the health check turned off, anything with a post-update
    check still running, resources on an unreachable server, and applications being deployed.
    At most HEAL_MAX_RESTARTS per incident, HEAL_RETRY_AFTER minutes apart, then it gives up until
    the resource has been healthy again. If HEAL_OUTAGE_THRESHOLD or more are broken at once, it only
    notifies: that points to the server, and mass restarts would make it worse. One restart per minute."""
    if not AUTO_HEAL or not DISCOVERY_COMPLETE:
        return
    incidents = state.setdefault("heal", {})
    watched = {u: c for u, c in found.items()
               if not c.get("self") and c.get("healthcheck") is not False and u not in SERVER_DOWN}
    for uuid in list(incidents):
        if uuid not in watched:
            del incidents[uuid]

    for uuid, cfg in watched.items():
        status = STATUSES.get(uuid, "")
        inc = incidents.setdefault(uuid, {"bad": 0, "ok": 0, "restarts": 0, "last": None, "gave_up": False})
        docker = DOCKER_HEALTH.get(uuid, "ok")
        if docker == "starting":
            inc["bad"] = inc["ok"] = 0  # still starting: neither healthy nor broken yet
        elif status_broken(status) or docker == "unhealthy":
            if docker == "unhealthy" and not status_broken(status):
                status = "running:unhealthy"  # Docker already knows; Coolify hasn't caught up
            inc["bad"] += 1
            inc["ok"] = 0
            inc["status"] = status
        elif resource_ok(uuid):
            inc["bad"] = 0
            inc["ok"] += 1
            if inc["ok"] >= 2 and (inc["restarts"] or inc["gave_up"]):
                log(f"[{cfg['name']}] recovered: {status}")
                notify(f"coolify-watchtower: \u2713 {cfg['name']} recovered",
                       f"Healthy again after {inc['restarts']} restart(s). Coolify status: {status}")
            if inc["ok"] >= 2:
                incidents[uuid] = {"bad": 0, "ok": inc["ok"], "restarts": 0, "last": None, "gave_up": False}
        else:
            inc["bad"] = 0  # starting, exited, paused or unknown: not broken, but not proof of health either
            inc["ok"] = 0

    broken = [u for u in watched if incidents[u]["bad"] > 0]
    if len(broken) >= HEAL_OUTAGE_THRESHOLD:
        if not state.get("outage"):
            state["outage"] = True
            lines = [f"{watched[u]['name']}: {incidents[u]['status']}" for u in sorted(broken, key=lambda u: watched[u]["name"].lower())]
            log(f"WARN {len(broken)} resources unhealthy at once - not restarting anything")
            notify(f"coolify-watchtower: \u26a0 {len(broken)} services unhealthy at once",
                   "\n".join(lines) + "\nNot restarting anything. This usually points to the server (disk, memory, network).")
        return
    state["outage"] = False

    pending = state.get("pending", {})
    for uuid in sorted(broken, key=lambda u: -incidents[u]["bad"]):
        cfg, inc = watched[uuid], incidents[uuid]
        if inc["gave_up"] or uuid in pending or inc["bad"] < HEAL_AFTER:
            continue
        if inc["last"] and (now - inc["last"]).total_seconds() < HEAL_RETRY_AFTER * 60:
            continue
        if inc["restarts"] >= HEAL_MAX_RESTARTS:
            inc["gave_up"] = True
            log(f"[{cfg['name']}] WARN still {inc['status']} after {inc['restarts']} restart(s) - giving up until it's healthy")
            notify(f"coolify-watchtower: \u2717 {cfg['name']} still unhealthy after {inc['restarts']} restarts",
                   f"Coolify status: {inc['status']}\nNot restarting it again until it has been healthy. It needs you.")
            continue
        if deployment_running(cfg):
            continue
        inc["restarts"] += 1
        inc["last"] = now
        attempt = f"attempt {inc['restarts']} of {HEAL_MAX_RESTARTS}"
        if DRY_RUN:
            log(f"[{cfg['name']}] DRY_RUN - {inc['status']} for {inc['bad']} min, would restart ({attempt})")
            notify(f"coolify-watchtower (dry run): {cfg['name']} unhealthy", f"Coolify status: {inc['status']} for {inc['bad']} min\nWould restart it ({attempt}).")
            break
        what, results = heal_restart(uuid, cfg)
        failed = [(st, data) for st, data in results if st not in (200, 201, 202)]
        if failed:
            st, data = failed[0]
            reason = data.get("message") if isinstance(data, dict) else data
            log(f"[{cfg['name']}] ERROR restart HTTP {st} {data}")
            notify(f"coolify-watchtower: FAILED to restart {cfg['name']}", f"HTTP {st}: {reason}")
        else:
            log(f"[{cfg['name']}] {inc['status']} for {inc['bad']} min - restarting {what} ({attempt})")
            notify(f"coolify-watchtower: \u26a0 {cfg['name']} unhealthy, restarting",
                   f"Coolify status: {inc['status']} for {inc['bad']} min\nRestarting {what} ({attempt}).")
        break  # one restart per minute


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
    announce_changes(found, state)
    follow_up_updates(now, state)
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
        summary = check_service(uuid, cfg, allow_restart=True)
        if summary:
            watch_update(uuid, cfg, summary, now, state)
    heal(found, now, state)


def settings_report():
    """Startup lines: every setting with its effective value and where it comes from.
    'env' = set in the environment (compose file or Coolify), 'default' = built into the image.
    Secrets only show whether they're set."""
    def source(name):
        return "env" if os.environ.get(name, "").strip() else "default"

    rows = [
        ("COOLIFY_URL", COOLIFY_URL),
        ("COOLIFY_TOKEN", "set" if COOLIFY_TOKEN else "missing"),
        ("NOTIFY_URL", "set" if NOTIFY_URL else "off"),
        ("DRY_RUN", str(DRY_RUN).lower()),
        ("SELF_UPDATE", SELF_UPDATE),
        ("TZ", TZ_OVERRIDE or "from Coolify servers"),
        ("DEFAULT_SCHEDULE", DEFAULT_SCHEDULE),
        ("REPORT_ON_START", str(REPORT_ON_START).lower()),
        ("TASK_NAME", TASK_NAME),
        ("AUTO_UPDATE_LABEL", AUTO_UPDATE_LABEL),
        ("AUTO_HEAL", str(AUTO_HEAL).lower()),
        ("HEAL_AFTER", f"{HEAL_AFTER} min"),
        ("HEAL_RETRY_AFTER", f"{HEAL_RETRY_AFTER} min"),
        ("HEAL_MAX_RESTARTS", str(HEAL_MAX_RESTARTS)),
        ("HEAL_OUTAGE_THRESHOLD", str(HEAL_OUTAGE_THRESHOLD)),
        ("DOCKER_HOST", DOCKER_URL),
    ]
    lines = ["Settings (env = from the environment, default = built into the image):"]
    lines += [f"    {name:<22} {value:<32} {'-' if name == 'COOLIFY_TOKEN' and not COOLIFY_TOKEN else source(name)}"
              for name, value in rows]
    extras = []
    if TASK_NAME not in ("watchtower", LEGACY_TASK):
        extras.append(f"task '{TASK_NAME}'")
    if AUTO_UPDATE_LABEL not in ("coolify.watchtower", LEGACY_LABEL):
        extras.append(f"label '{AUTO_UPDATE_LABEL}'")
    lines.append("Opt-in: a scheduled task 'watchtower' or the label 'coolify.watchtower' (the old 'auto-update' and "
                 "'coolify.auto-update' still work)" + (f", plus {' and '.join(extras)}" if extras else ""))
    return lines


def leftover_warnings():
    """Old names set explicitly: almost always variables left over in Coolify from an older compose file."""
    warnings = []
    for name, legacy in (("TASK_NAME", LEGACY_TASK), ("AUTO_UPDATE_LABEL", LEGACY_LABEL)):
        if os.environ.get(name, "").strip() == legacy:
            warnings.append(f"WARN {name}={legacy} looks like a leftover from an older compose file. Delete it in "
                            f"Coolify's Environment Variables tab and restart. Everything works meanwhile.")
    return warnings


def main():
    if COOLIFY_TOKEN and not TZ_OVERRIDE:
        try:  # so even the first log lines use the server's timezone
            set_log_tz(server_timezones() or {})
        except Exception:
            pass
    log(f"coolify-watchtower {VERSION}")
    for line in settings_report():
        log(line)
    for line in leftover_warnings():
        log(line)
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
        announce_changes(found, state)  # sets the baseline, announces nothing
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
