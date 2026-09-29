<img src="https://raw.githubusercontent.com/KimTholstorf/coolify-watchtower/main/assets/logo-scene.png" width="160" alt="coolify-watchtower logo, a pixel-art lighthouse by the sea">

# coolify-watchtower

Automatic image updates for your [Coolify](https://coolify.io) services, carried out by Coolify itself.

coolify-watchtower checks the images of the services you choose, on a schedule you set per service. When a registry has a newer image, it asks Coolify to restart that service with the latest image. Coolify does the update, so it lands in the service's deployment history like any other deployment. Docker access is read-only, through a socket proxy.

Full documentation, source and issues: **[github.com/KimTholstorf/coolify-watchtower](https://github.com/KimTholstorf/coolify-watchtower)**

## Quick start

1. In Coolify, enable **API access** in the settings and create an API token with the `read` and `deploy` permissions.
2. Go to **+ New Resource → Docker Compose (empty)** and paste [`docker-compose.dockerhub.yml`](https://github.com/KimTholstorf/coolify-watchtower/blob/main/docker-compose.dockerhub.yml). Leave **Connect to Predefined Network** off.
3. Set `COOLIFY_TOKEN` under **Environment Variables** and deploy. Everything else has a default.
4. On each service you want kept up to date, add a scheduled task named `auto-update` with the command `true`. Its frequency is the update schedule, e.g. `30 4 * * *` for every day at 04:30.

Services (compose and one-click) and Docker Image applications are supported. Notifications can go to Discord or ntfy, and coolify-watchtower keeps itself up to date too.

## Tags

| Tag | Meaning |
|---|---|
| `latest` | the newest release |
| `1.7.1`, `1.7`, `1` | a specific release, minor or major version |

Images are built for `linux/amd64` and `linux/arm64`, and are identical to those on `ghcr.io/kimtholstorf/coolify-watchtower`.

## License

MIT. coolify-watchtower isn't affiliated with Coolify or Watchtower.
