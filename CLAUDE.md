# CLAUDE.md: coolify-watchtower

A Coolify-native replacement for Watchtower in a homelab running Coolify v4. Coolify performs the updates itself; this service only decides *when* to trigger them.

## How it works
1. Each Coolify **service** that should auto-update gets a Scheduled Task (in its own UI tab) named `auto-update`, command `true`. That task *is* the configuration: `frequency` is the schedule and the `enabled` toggle pauses updates.
   **Alternative opt-in:** a container label `coolify.auto-update` (env `AUTO_UPDATE_LABEL`) on any container of the service, set in the service's compose file next to Coolify's own `coolify.managed` / `coolify.serviceId` labels. Its value is the schedule (cron or alias). `true` or empty means `DEFAULT_SCHEDULE` (default `daily`), and `false`/`no`/`off`/`0` pauses. The label opts in the whole service, because restart is service-wide. If a service has both, the task wins. Labels on containers that don't belong to a service (e.g. applications) are logged as ignored.
2. `updater.py` runs as a central Coolify resource (compose: `updater` + `socket-proxy`). Every minute it:
   - calls `GET /api/v1/services` and `GET /api/v1/services/{uuid}/scheduled-tasks` to find `auto-update` tasks, and lists running containers to find `coolify.auto-update` labels
   - evaluates each task's cron against the current minute in the timezone of the server the service runs on (`GET /servers` → `settings.server_timezone`, matched through the service's `server_id`), or in `TZ` if set. Log timestamps use `TZ`, or the server timezone when all servers agree, else UTC
   - for due services, finds their running containers through the read-only Docker API and compares each image's local `RepoDigests` with the registry's `Docker-Content-Digest` (manifest HEAD request, anonymous bearer token flow)
   - calls `POST /api/v1/services/{uuid}/restart?latest=true` only if a digest changed, falling back to GET on 405 for Coolify < 4.2.0
3. Notifications go to `NOTIFY_URL`. A Discord webhook URL gets a JSON body (`content`, mentions disabled, max 2000 chars); anything else gets a plain-text POST with a `Title` header (ntfy style). Requests send a custom User-Agent, because Cloudflare in front of Discord blocks urllib's default one (error 1010). Coolify's API has no send endpoint: `/notifications/discord` only reads and changes settings, and reading the webhook URL needs `read:sensitive`, so we don't use it.
4. Version names: when an update is found, `describe_change()` maps the old and new digests to version tags. Docker Hub: one call to `hub.docker.com/v2/repositories/{repo}/tags` (100 most recent tags, with digests). Other registries: `tags/list` with Link pagination (up to 20 pages), then HEAD on the 30 newest version-looking tags. `best_version_tag()` picks the most specific one (`4.3.3` over `4`). Falls back to short digests.
5. Health checks: the updater touches `/tmp/heartbeat` every loop and is unhealthy if it's older than 180 s (in both the Dockerfile and the compose files). The socket proxy is checked with `wget` against `/version`, and the updater waits for it to be healthy. To run the compose files locally, create the external network first: `docker network create coolify`.
6. At startup it prints a table of all opted-in services and runs a report-only check, which never restarts anything.

## Distribution
The repo goes on GitHub (`KimTholstorf/coolify-watchtower`). CI builds a multi-arch image to `ghcr.io/kimtholstorf/coolify-watchtower`, which people pull through a compose file. Until then, people deploy from the repo through Coolify's GitHub App source, which builds it. Later goal: get it into Coolify's one-click service catalogue, whose templates reference a published image, so `docker-compose.yml` should stay usable without the repo.

## Files
- `updater.py`: the whole app. Standard library only.
- `Dockerfile`: `python:3.13-alpine` + `tzdata`, runs as `nobody`.
- `docker-compose.yml`: pulls the ghcr.io image. For pasting into Coolify and, later, the one-click template.
- `docker-compose.build.yml`: the same, with `build: .`. Used when Coolify deploys from the GitHub repo. Keep the two in sync.
- `assets/`: the pixel lighthouse logo. `make_logo.py` generates `logo.svg` (transparent) and `logo-icon.svg` (on a disc) from a text grid; `logo-icon.png` is a raster export.
- `.github/workflows/image.yml`: runs the tests on every push and pull request. Only for release tags `vX.Y.Z` does it build amd64+arm64 and push to ghcr.io as `X.Y.Z`, `X.Y`, `X` and `latest`, so `latest` is always the newest release. Releasing = pushing a tag.
- `.github/workflows/dockerhub.yml`: after `image` succeeds for a release tag `vX.Y.Z`, copies `ghcr.io/…:X.Y.Z` to Docker Hub by digest (`imagetools create`, no rebuild) as `X.Y.Z`, `X.Y`, `X` and `latest`. Both registries carry the same tags and digests. Can also be run by hand (Actions → dockerhub → Run workflow, with a tag) for older releases. Needs repository variable (not secret) `DOCKERHUB_USERNAME` and repository secret `DOCKERHUB_TOKEN`. Published as `docker.io/kimtholstorf/coolify-watchtower`; verified with 1.6.1 (same digest as ghcr.io).
- `tests/test_updater.py`: offline tests covering cron, image refs, and one full tick against mocked Coolify and Docker HTTP servers. Run with `python3 tests/test_updater.py`.
- `README.md`: user-facing setup.

## Hard constraints (deliberate, do not "fix")
- **Standard library only.** No dependencies, so the image is just `python:3.13-alpine` plus `tzdata`.
- **Docker access is read-only.** It goes through `tecnativa/docker-socket-proxy` with `CONTAINERS=1 IMAGES=1 POST=0`. The updater must never write to Docker; all changes go through the Coolify API.
- **Only the updater joins the `coolify` network** (external network in the compose files); the socket proxy stays on the stack's private network. Read-only access still exposes every container's env vars, so the proxy must not be reachable from other stacks. Never tell users to enable Connect to Predefined Network, which would attach both services.
- **Sensible defaults, only `COOLIFY_TOKEN` required.** `DRY_RUN` defaults to false (opting a service in is the consent; the startup report never restarts), `SELF_UPDATE` to `daily`.
- The per-service task runs `true` inside a container of that service, so the chosen container needs a shell. Failures show up in Coolify under Settings → Scheduled Jobs → Failures.

## Design decisions and rejected alternatives
- **Prefer the image over the compose file.** Coolify keeps each user's pasted compose file (and one-click services keep the template as it was when created), so compose changes only reach users who re-paste by hand, while image changes arrive with Pull Latest or self-update. Put behaviour in code or the `Dockerfile` (defaults, the updater's health check); change the compose files only when unavoidable and call it out in release notes.
- **Self-update** is done in code: `discover()` finds our own service (`own_service()`: hostname == container ID prefix) and opts it in with the `SELF_UPDATE` env var (default `daily`, `false` turns it off). A task on our service still wins; `coolify.auto-update` labels on our own containers are ignored. It was a label first, but **Coolify does not interpolate `${...}` inside compose `labels:`** (only in `environment:`), so the label arrived literally. Only works for the pasted-compose (service) install, not the Git/build (application) one. When several services are due in one minute, our service restarts last.
- **Watchtower (nicholas-fedor fork)** was rejected. It recreates containers behind Coolify's back, isn't compose-aware, needs a writable socket, and updates don't appear in Coolify's deployment history.
- **Per-service tasks that call the API themselves** were rejected: every container would need curl, network access to Coolify, and a token, and a task would restart its own container.
- **Host cron** was rejected because it isn't visible in the Coolify UI.
- **Task-as-config with a no-op `true`** was chosen: the schedule is editable per service in the native UI, execution is visible centrally in Settings → Scheduled Jobs, and all privileges stay in one central service. The trade-off is that Coolify's execution history shows the no-op, not the update; actual updates appear in the service's deployment history and in the updater's logs.

## Unverified assumptions (check against the real server first)
- **Token permissions:** per Coolify's source (`routes/api.php`, `ServicePolicy::deploy`, `ApiAbility`), restart needs the `deploy` ability and a token owned by a team admin/owner; `write` is not needed. A member's token with `deploy` gets 403 on every call. Confirmed by a real restart with a `read` + `deploy` token.
- Coolify's schedule words map as `daily` = `0 0 * * *`, `weekly` = `0 0 * * 0`, and so on (`CRON_ALIASES`).
- Coolify keeps user-defined labels such as `coolify.auto-update` from a service's compose file when it adds its own labels, and the compose project stays the service uuid.
- Coolify's Docker Compose build pack builds `docker-compose.build.yml` from the GitHub repo as expected.
- Coolify doesn't override the container hostname (else the updater can't find its own service: no self-update, no restart-last ordering).

## Status
It is published as `ghcr.io/kimtholstorf/coolify-watchtower` and runs on a real Coolify (v4.3.23) with `DRY_RUN=false`. It has updated one service opted in through a scheduled task.

Verified:
- Offline tests (cron, image refs, labels, notification payloads, one full tick against mocked Coolify and Docker).
- The image builds and runs as `nobody`, timezones work, and both compose files validate.
- Registry digest checks against the real Docker Hub and ghcr.io match Docker's local `RepoDigests`.
- End to end with a real socket proxy, labelled containers and a mock Coolify API: finds the service, ignores stray labels, reports an outdated image, and calls `restart?latest=true` when `DRY_RUN=false`.
- Discord webhook notifications arrive with the expected formatting.
- On a real Coolify: the compose `networks:` setup is respected (updater reaches `http://coolify:8080`, the proxy is only on the stack's own networks), Allowed API IPs with the `coolify` subnet works. A `read`-only token is enough for discovery, timezones and dry runs; restart with it returns 403, as expected. `/api/v1/version` answers with plain text, not JSON. `GET /servers` returns `settings.server_id` and `settings.server_timezone` (not hidden, so `read:sensitive` isn't needed) and the updater uses them: schedules fire in the server's timezone, at the same minute Coolify runs the task.
- The scheduled-tasks API endpoints exist (Coolify v4.3.23).
- A real update: `restart?latest=true` made Coolify restart the service and pull the new image, and the Discord notification arrived.
- A real dry run on an opted-in service: containers were matched to the service (compose project label or `-<uuid>` name suffix), the Docker Hub digest check found a real update, and the dry-run Discord notification arrived.

Not verified yet: everything under "Unverified assumptions" above.

Next step: test the label opt-in on a real server.

## Backlog (rough priority)
1. Fix whatever the first real dry run shows (mapping, registry errors).
2. **Applications support.** For Docker-image applications, `/applications/{uuid}/deploy` reportedly doesn't pull new images. A possible approach is to pull through Docker first and then deploy, but that breaks the read-only socket constraint, so discuss it before building. Currently these tasks are only reported at startup.
3. Private registry credentials (e.g. `REGISTRY_AUTH` as JSON: host → user/token).
4. Submit to Coolify's one-click service catalogue.
5. Cron month and day names (`MON`, `JAN`).
6. Missed-minute catch-up if a tick takes longer than 60 s (currently that minute is skipped).
7. Optional: stagger restarts when several services are due in the same minute.
