# testbed

A small service you can break on purpose, to test coolify-watchtower on a real Coolify server: updates, the health check after updates, and self-healing.

It has two containers, `web` and `worker`. `web` serves a control page with buttons that make either container unhealthy, crash, hang or start slowly. The image is `ghcr.io/kimtholstorf/coolify-watchtower-testbed`, published by the [testbed workflow](../.github/workflows/testbed.yml).

## Set it up

1. In Coolify, go to **+ New Resource → Docker Compose (empty)** and paste [`docker-compose.yml`](docker-compose.yml).
2. Deploy. A domain is optional: to use the web page, give `web` one with the port, e.g. `https://testbed.example.com:8080`. Without one, use the [`testbed` command](#from-a-terminal).
3. On the service, add a scheduled task named `watchtower` with the command `true`, frequency `*/5 * * * *` and container `web`.
4. Run `testbed status` in `web`'s terminal, or open the page. Both show the health of each container, the version and how often each has started.

To test faster, add `HEAL_AFTER=1` and `HEAL_RETRY_AFTER=2` to coolify-watchtower's environment while testing. Remove them afterwards.

## From a terminal

Open the container's terminal in Coolify and use the `testbed` command. It does the same as the buttons on the page.

```text
testbed                        menu: pick an action by number, w switches to the worker, q quits
testbed status                 health of web and worker
testbed unhealthy              run an action directly (testbed --unhealthy works too)
testbed unhealthy --worker     the same, on the worker
testbed help                   all actions
```

The actions are `unhealthy`, `unhealthy-persistent`, `hang`, `crash`, `crash-loop`, `slow-start` and `recover`.

## Publish a new version

Go to **Actions → testbed → Run workflow**. Each run publishes a new `latest`, versioned `1.0.<run number>`. Tick **broken** to publish a release whose health check always fails. The next normal run fixes it.

## Scenarios

The times assume the defaults: checks every 5 minutes, `HEAL_AFTER=5`, `HEAL_RETRY_AFTER=15`.

| Do this | Expect |
|---|---|
| Run the workflow | at the next 5-minute mark: "updating testbed" with `1.0.4 -> 1.0.5`, then "✓ healthy after the update" |
| Run the workflow with **broken** | "updating testbed", then after 10 minutes "⚠ looks unhealthy after the update". No rollback |
| **Make unhealthy** | after 5 minutes: "⚠ unhealthy, restarting web". The restart fixes it: "✓ recovered" |
| **Make the worker unhealthy** | the same, but only `worker` is restarted |
| **Make unhealthy, permanently** | two restarts, 15 minutes apart, then "✗ still unhealthy after 2 restarts". **Recover** ends it |
| **Hang the health check** | like Make unhealthy: running, but the check times out |
| **Worker crash loop** | the service turns degraded; the worker recovers by itself after 5 crashes |
| **Slow start next time**, then restart the service in Coolify | `starting` for about 100 seconds. Nothing is restarted |
| Add the label `coolify.watchtower.healthcheck=false`, redeploy, then **Make unhealthy** | nothing happens |

After a restart, a container that's still broken shows as `starting` for up to 2 minutes (the health check's start period) before it's `unhealthy` again. Real apps work the same way.
