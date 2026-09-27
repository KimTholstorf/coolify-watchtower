# coolify-watchtower v1

Watchtower-style auto-updates for Coolify services, done the Coolify way: Coolify performs the update via its API. Not based on Watchtower and not affiliated with Coolify.

> Working on this code? See CLAUDE.md for design decisions, constraints and backlog.

Coolify-native image auto-updater. Coolify performs the update; this service only decides when to trigger it.

- **Schedule per service**: a Scheduled Task named `auto-update`, command `true`, on the service itself, **or** a `coolify.auto-update` label in the service's compose file (next to Coolify's own `coolify.managed`, `coolify.serviceId`, … labels).
- **Central service**: reads those tasks via the Coolify API, checks image digests through a read-only Docker socket proxy, and calls `POST /services/{uuid}/restart?latest=true` only when an image changed.
- **Visibility**: each service's Scheduled Tasks tab (schedule, enable/disable), Settings → Scheduled Jobs (runs/failures of the `true` no-op), the service's deployment history (actual updates), and this service's Logs (table of all auto-update services and check results).

## Setup

1. **API token**: Keys & Tokens → API tokens, with `read` + `deploy` (add `write` if restart returns 403).
2. **Deploy**, either:
   - **From this GitHub repo** (Coolify builds the image): connect GitHub through Coolify's [GitHub App source](https://coolify.io/docs/applications/sources/github/app), create a resource from this repo, choose the **Docker Compose** build pack and set **Docker Compose Location** to `/docker-compose.build.yml`.
   - **From the prebuilt image**: + New Resource → Docker Compose (empty) → paste `docker-compose.yml`. It pulls `ghcr.io/kimtholstorf/coolify-watchtower:latest`.
3. **Environment variables**:
   - `COOLIFY_TOKEN`: the token
   - `COOLIFY_URL`: default `http://coolify:8080`, which needs **Connect to Predefined Network** enabled on this resource. Otherwise use your dashboard URL.
   - `TZ`: set this to the same timezone as the server in Coolify (Server → General), so checks run when Coolify runs the task.
   - `DRY_RUN`: `true` by default. Set it to `false` once the logs look right.
   - `AUTO_UPDATE_LABEL` / `DEFAULT_SCHEDULE` (optional): the label name (default `coolify.auto-update`) and the schedule used when its value is `true` (default `daily`).
   - `NOTIFY_URL` (optional): an ntfy topic URL or any endpoint that accepts a plain-text POST.
4. **Opt a service in**: on that service, go to Scheduled Tasks → New:
   - Name: `auto-update`
   - Schedule: `30 4 * * *` (or `daily`, `weekly`)
   - Command: `true`
   - Container: pick one that has a shell (`sh`)
   **Or use a label**: add it to any container in the service's compose file and redeploy the service:
   ```yaml
   labels:
     - coolify.auto-update=30 4 * * *   # cron or daily/weekly/...; "true" = DEFAULT_SCHEDULE (daily); "false" = paused
   ```
   The label opts in the whole service. If the service also has an `auto-update` task, the task wins. A label needs no shell in the container, but it's only visible in the compose file, and there's no entry under Settings → Scheduled Jobs. Label changes take effect only after a redeploy, because Docker sets labels when it creates the container.
5. **Check the startup report** in the Logs tab. It lists every opted-in service, its schedule, and whether updates are available. It never restarts anything.

## Behaviour and limits

- Only **services** (compose / one-click) are supported in v1. `auto-update` tasks and `coolify.auto-update` labels on applications are reported at startup and otherwise ignored.
- Images pinned by digest and locally built images are skipped.
- Only public registries are supported (anonymous token flow), including Docker Hub and ghcr.io.
- Cron support covers numbers, `*`, ranges, lists and steps, plus Coolify's words (`daily`, `weekly`, …). Month and day names are not supported.
- Coolify restarts compose services without a rolling update, so expect a short outage. There is no rollback.
- Changes to schedules are picked up within a minute.
