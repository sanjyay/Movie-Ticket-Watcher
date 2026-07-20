# CasaOS deployment

Replace `REPOSITORY_OWNER` in `docker-compose.yml` and generate `SECRET_KEY` with `openssl rand -hex 48`. The app has no built-in login screen, so expose it only on a trusted LAN or behind your own reverse proxy/VPN authentication. Private GHCR packages require a GitHub PAT with `read:packages` configured in CasaOS/Docker.

## Method A — Custom App

Create two containers from `ghcr.io/OWNER/movie-ticket-watcher:latest`: `movie-ticket-watcher-web` with command `web` and port `8787:8787`, and `movie-ticket-watcher-worker` with command `worker` and no ports. Map `/DATA/AppData/movie-ticket-watcher/data` to `/data` and `/DATA/AppData/movie-ticket-watcher/config` to `/config` in both. Enter the environment variables shown in the Compose file, use `unless-stopped`, 1 GB shared memory, and start with 1 GB RAM/1 CPU per service (2 GB available to the deployment is more comfortable during concurrent browser checks). Do not enable privileged mode or mount the Docker socket.

## Method B — Compose import

Paste/import `casaos/docker-compose.yml`, replace the image owner, and supply `SECRET_KEY`. Recreating either container preserves the database because it is at `/DATA/AppData/movie-ticket-watcher/data/tickets.db` on the host. If permissions fail, run `sudo chown -R PUID:PGID /DATA/AppData/movie-ticket-watcher/{data,config}` only after confirming those exact paths.

After launch, configure BookMyShow and PVR INOX independently as **Automatic discovery**, **Direct URL**, or **Disabled**. Direct mode requires an official platform URL; automatic mode does not. Cloudflare or platform-protection responses are never bypassed. Their per-platform cooldown is persisted under the same database and defaults to 30 minutes, 1 hour, 3 hours, then at most 6 hours; the `BLOCKED_RETRY_*_SECONDS` variables in the Compose example can tune those values.
