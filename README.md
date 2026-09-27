# coolify-watchtower v1

Automatic image updates for Coolify services, similar to Watchtower, except that Coolify does the actual update through its own API. This service only decides when to trigger it. It isn't based on Watchtower and isn't affiliated with Coolify.

> Working on this code? See CLAUDE.md for design decisions, constraints and backlog.

## How it works

You opt a service in with a Scheduled Task named `auto-update` (command `true`) on the service itself. Alternatively, put a `coolify.auto-update` label in the service's compose file, next to Coolify's own labels such as `coolify.managed` and `coolify.serviceId`.

The updater reads those tasks through the Coolify API and checks image digests through a read-only Docker socket proxy. When an image has changed, it calls `POST /services/{uuid}/restart?latest=true`. Otherwise it does nothing.

You can follow what happens in several places:

- The service's Scheduled Tasks tab shows the schedule and lets you enable or disable it.
- Settings → Scheduled Jobs shows runs and failures of the `true` no-op.
- The service's deployment history shows the actual updates.
- This service's Logs tab shows a table of all auto-update services and the result of each check.

## Setup

1. Create an API token under Keys & Tokens → API tokens, with `read` and `deploy`. If restart returns 403, add `write`.
2. Deploy it in one of two ways:
   - From this GitHub repo, so Coolify builds the image: connect GitHub through Coolify's [GitHub App source](https://coolify.io/docs/applications/sources/github/app), create a resource from this repo, choose the Docker Compose build pack and set Docker Compose Location to `/docker-compose.build.yml`.
   - From the prebuilt image: + New Resource → Docker Compose (empty), then paste `docker-compose.yml`. It pulls `ghcr.io/kimtholstorf/coolify-watchtower:latest`.
3. Set the environment variables:
   - `COOLIFY_TOKEN`: the token.
   - `COOLIFY_URL`: defaults to `http://coolify:8080`, which only works with Connect to Predefined Network enabled on this resource. Otherwise use your dashboard URL.
   - `TZ`: use the same timezone as the server in Coolify (Server → General), so checks run at the same time Coolify runs the task.
   - `DRY_RUN`: `true` by default. Set it to `false` once the logs look right.
   - `AUTO_UPDATE_LABEL` and `DEFAULT_SCHEDULE` (optional): the label name (default `coolify.auto-update`) and the schedule used when the label's value is `true` (default `daily`).
   - `NOTIFY_URL` (optional): where to send a message when an update is queued, fails, or would run in dry-run mode. This can be a Discord webhook URL (`https://discord.com/api/webhooks/...`), an ntfy topic URL, or any endpoint that accepts a plain-text POST. Coolify's API can't send messages to the Discord channel you set up in Coolify, but you can create a second webhook in the same channel under Channel settings → Integrations → Webhooks and use its URL here.
4. Opt a service in. On that service, go to Scheduled Tasks → New and enter:
   - Name: `auto-update`
   - Schedule: `30 4 * * *` (or `daily`, `weekly`)
   - Command: `true`
   - Container: one that has a shell (`sh`)

   Or add the label to any container in the service's compose file and redeploy the service:
   ```yaml
   labels:
     - coolify.auto-update=30 4 * * *   # cron or daily/weekly/...; "true" = DEFAULT_SCHEDULE (daily); "false" = paused
   ```
   One label opts in the whole service, and if the service also has an `auto-update` task, the task wins. The label doesn't need a shell in the container. On the other hand, you can only see it in the compose file, and it doesn't show up under Settings → Scheduled Jobs. Docker sets labels when it creates a container, so a label change only takes effect after a redeploy.
5. Read the startup report in the Logs tab. It lists every opted-in service with its schedule and whether an update is available. The startup check never restarts anything.

## Behaviour and limits

- v1 supports services (compose and one-click) only. The startup report lists `auto-update` tasks and `coolify.auto-update` labels on applications, but the updater otherwise ignores them.
- The updater skips images pinned by digest and images built locally.
- It only works with public registries, such as Docker Hub and ghcr.io, because it uses the anonymous token flow.
- Schedules accept numbers, `*`, ranges, lists and steps, plus Coolify's words (`daily`, `weekly`, …). Month and day names don't work.
- Coolify restarts compose services without a rolling update, so expect a short outage. There is no rollback.
- The updater picks up schedule changes within a minute.
