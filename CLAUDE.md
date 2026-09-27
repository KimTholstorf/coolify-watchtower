# CLAUDE.md: coolify-watchtower

A Coolify-native replacement for Watchtower in a homelab running Coolify v4. Coolify performs the updates itself; this service only decides *when* to trigger them.

## How it works
1. Each Coolify **service** that should auto-update gets a Scheduled Task (in its own UI tab) named `auto-update`, command `true`. That task *is* the configuration: `frequency` is the schedule and the `enabled` toggle pauses updates.
   **Alternative opt-in:** a container label `coolify.auto-update` (env `AUTO_UPDATE_LABEL`) on any container of the service, set in the service's compose file next to Coolify's own `coolify.managed` / `coolify.serviceId` labels. Its value is the schedule (cron or alias). `true` or empty means `DEFAULT_SCHEDULE` (default `daily`), and `false`/`no`/`off`/`0` pauses. The label opts in the whole service, because restart is service-wide. If a service has both, the task wins. Labels on containers that don't belong to a service (e.g. applications) are logged as ignored.
2. `updater.py` runs as a central Coolify resource (compose: `updater` + `socket-proxy`). Every minute it:
   - calls `GET /api/v1/services` and `GET /api/v1/services/{uuid}/scheduled-tasks` to find `auto-update` tasks, and lists running containers to find `coolify.auto-update` labels
   - evaluates each task's cron against the current minute, in `TZ`
   - for due services, finds their running containers through the read-only Docker API and compares each image's local `RepoDigests` with the registry's `Docker-Content-Digest` (manifest HEAD request, anonymous bearer token flow)
   - calls `POST /api/v1/services/{uuid}/restart?latest=true` only if a digest changed, falling back to GET on 405 for Coolify < 4.2.0
3. At startup it prints a table of all opted-in services and runs a report-only check, which never restarts anything.

## Distribution
The repo goes on GitHub (`KimTholstorf/coolify-watchtower`). CI builds a multi-arch image to `ghcr.io/kimtholstorf/coolify-watchtower`, which people pull through a compose file. Until then, people deploy from the repo through Coolify's GitHub App source, which builds it. Later goal: get it into Coolify's one-click service catalogue, whose templates reference a published image, so `docker-compose.yml` should stay usable without the repo.

## Files
- `updater.py`: the whole app. Standard library only.
- `Dockerfile`: `python:3.13-alpine` + `tzdata`, runs as `nobody`.
- `docker-compose.yml`: pulls the ghcr.io image. For pasting into Coolify and, later, the one-click template.
- `docker-compose.build.yml`: the same, with `build: .`. Used when Coolify deploys from the GitHub repo. Keep the two in sync.
- `.github/workflows/image.yml`: runs the tests, then builds amd64+arm64 and pushes to ghcr.io on `main` (`latest`) and `v*` tags (semver). Pull requests build without pushing.
- `tests/test_updater.py`: offline tests covering cron, image refs, and one full tick against mocked Coolify and Docker HTTP servers. Run with `python3 tests/test_updater.py`.
- `README.md`: user-facing setup.

## Hard constraints (deliberate, do not "fix")
- **Standard library only.** No dependencies, so the image is just `python:3.13-alpine` plus `tzdata`.
- **Docker access is read-only.** It goes through `tecnativa/docker-socket-proxy` with `CONTAINERS=1 IMAGES=1 POST=0`. The updater must never write to Docker; all changes go through the Coolify API.
- **`DRY_RUN` defaults to true.**
- The per-service task runs `true` inside a container of that service, so the chosen container needs a shell. Failures show up in Coolify under Settings → Scheduled Jobs → Failures.

## Design decisions and rejected alternatives
- **Watchtower (nicholas-fedor fork)** was rejected. It recreates containers behind Coolify's back, isn't compose-aware, needs a writable socket, and updates don't appear in Coolify's deployment history.
- **Per-service tasks that call the API themselves** were rejected: every container would need curl, network access to Coolify, and a token, and a task would restart its own container.
- **Host cron** was rejected because it isn't visible in the Coolify UI.
- **Task-as-config with a no-op `true`** was chosen: the schedule is editable per service in the native UI, execution is visible centrally in Settings → Scheduled Jobs, and all privileges stay in one central service. The trade-off is that Coolify's execution history shows the no-op, not the update; actual updates appear in the service's deployment history and in the updater's logs.

## Unverified assumptions (check against the real server first)
- **Container → service mapping:** a container belongs to a service if the label `com.docker.compose.project == <service uuid>` or its name ends in `-<uuid>`. A startup log line "no running containers found" means this assumption is wrong. Verify with `docker inspect`.
- **Token permissions:** `read` + `deploy` should be enough for restart; `write` may be needed.
- The scheduled-tasks API endpoints must exist in the installed Coolify version.
- Coolify's schedule words map as `daily` = `0 0 * * *`, `weekly` = `0 0 * * 0`, and so on (`CRON_ALIASES`).
- Coolify keeps user-defined labels such as `coolify.auto-update` from a service's compose file when it adds its own labels, and the compose project stays the service uuid.
- Coolify's Docker Compose build pack builds `docker-compose.build.yml` from the GitHub repo as expected.
- The registry digest check hasn't been run against the real Docker Hub or ghcr.io, only a mock.

## Status
v1 is written and passes the offline tests. It has not yet been deployed, and neither the Docker image build nor the CI workflow has been run. Next step: push to GitHub, deploy with `DRY_RUN=true` on the homelab Coolify server, add an `auto-update` task (or label) to one low-risk service, and read the startup report.

## Backlog (rough priority)
1. Fix whatever the first real dry run shows (mapping, registry errors).
2. **Applications support.** For Docker-image applications, `/applications/{uuid}/deploy` reportedly doesn't pull new images. A possible approach is to pull through Docker first and then deploy, but that breaks the read-only socket constraint, so discuss it before building. Currently these tasks are only reported at startup.
3. Private registry credentials (e.g. `REGISTRY_AUTH` as JSON: host → user/token).
4. Submit to Coolify's one-click service catalogue.
5. Cron month and day names (`MON`, `JAN`).
6. Missed-minute catch-up if a tick takes longer than 60 s (currently that minute is skipped).
7. Optional: stagger restarts when several services are due in the same minute.
