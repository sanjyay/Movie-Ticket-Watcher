# Movie Ticket Watcher

Movie Ticket Watcher is a FastAPI dashboard plus a separate monitoring worker for BookMyShow and PVR INOX. It filters concrete show listings by movie, city, date, language, format, time range, theatres, and platform, then sends ntfy alerts only for newly detected shows. It never bypasses CAPTCHAs, queues, authentication, rate limits, seat selection, or payment.

## Architecture

`movie-ticket-watcher-web` serves port 8787; `movie-ticket-watcher-worker` runs the persisted schedule. Both use one image and bind-mount `./data` and `./config`. SQLite WAL mode, a database initialization lock, a deployment-wide worker file lock, persisted show fingerprints, and graceful SIGTERM handling protect migrations, scheduling, and notification deduplication.

## Docker prerequisites

Install Docker Engine and Docker Compose v2 yourself, add your user to the appropriate Docker group if desired, and verify `docker info` and `docker compose version`. The scripts never install Docker, use sudo, alter a firewall, mount the Docker socket, or use privileged mode.

## Omarchy / Arch Linux quick start

```bash
git clone YOUR_REPOSITORY_URL
cd movie_ticket_watcher
cp .env.example .env  # optional; install.sh also does this
./install.sh
docker compose logs -f
```

Use `./install.sh --port 8787`, `--pull`, or `--no-start` as needed. Open `http://127.0.0.1:8787`; from another LAN device use `http://OMARCHY_IP:8787` if your manually managed firewall allows it.

## Docker Compose configuration

Edit `.env`, never commit it. Runtime state is `/data/tickets.db`, `/data/screenshots`, `/data/logs`, `/data/backups`, and `/config` in both containers, mapped to the same repository-relative host directories. `PUID`/`PGID` default to 1000 and install detects the current user. Compose runs every process directly as that identity and uses `unless-stopped`, no-new-privileges, all capabilities dropped, 1 GB Chromium shared memory, and capped Docker JSON logs. For optional limits add `mem_limit: 2g` and `cpus: 1.0` per service; avoid less than roughly 1 GB during browser activity.

Development: `cp docker-compose.override.yml.example docker-compose.override.yml && docker compose up --build`; this bind-mounts source, enables reload/debug/simulation, and remains separate from production. A visible browser requires an explicitly configured display/socket and `PLAYWRIGHT_HEADLESS=false`; it is not enabled by default.

## First access

Open the dashboard directly. There is no built-in login screen:

```bash
http://127.0.0.1:8787
```

For access beyond a trusted LAN, put the app behind your own reverse proxy, VPN, or gateway authentication.

## Creating the Spider-Man watch

Select **New watch** and enter `Spider-Man: Brand New Day`, your city, a valid date whose day is 31, `English`, `2D`, the **Evening** preset (`17:00`–`21:59`), and optional theatres. Each platform has an independent mode: **Automatic discovery**, **Direct URL**, or **Disabled**. A direct URL must use the official platform hostname and is required only in Direct URL mode.

For BookMyShow, open the exact movie/event page in a normal browser and paste it when automatic discovery is blocked or unreliable. A direct URL is only a more precise starting point—it does not bypass Cloudflare and may be blocked from the server too. For PVR INOX, open the exact movie/event page and paste its `pvrcinemas.com` or `pvrinox.com` URL. The result page can test a saved direct URL once, retry one platform, disable one platform, clear only its discovered URL cache, show its screenshot, and download a sanitized JSON report.

## ntfy phone setup

Install ntfy, subscribe to a hard-to-guess topic, set `NTFY_SERVER`/optional token in `.env`, and place the topic on the watch. Use **Test ntfy** only when you intend to send a real test. Automated tests always mock notifications.

## Manual check and simulation

Use **Run check now** on a watch. For offline proof, edit it to simulation `UNAVAILABLE`, run once, change to `AVAILABLE`, and run twice; one notification per platform fingerprint is recorded and the repeat is deduplicated. `make simulate` seeds the existing demonstration workflow; simulation never calls live sites.

## Health checks and doctor

```bash
docker compose ps
curl --fail http://127.0.0.1:8787/health
docker compose exec web python scripts/doctor.py
```

The web health endpoint checks SQLite and writable mounts, not external ticket sites. The worker health check reads its persisted heartbeat. Doctor checks Chromium/local fixture screenshot, SQLite writes, directories, DNS/HTTPS, web, heartbeat, ntfy URL, UID/GID, and timezone. The dashboard shows version, heartbeat, database/storage data, last cycle, deployment mode, aggregate status, and an independent status badge for every enabled adapter without secrets.

Aggregate statuses preserve partial results: any matching platform is `AVAILABLE`; one blocked platform plus another nonmatching platform is `PARTIAL`; all enabled platforms blocked is `ALL_PLATFORMS_BLOCKED`; positively checked platforms with no qualifying show are `WAITING`; missing direct-mode configuration is `CONFIGURATION_REQUIRED`; and wholly failed/unsupported checks are `CHECK_FAILED`.

## Running tests

```bash
docker compose run --rm test
# equivalent: make test
```

Tests cover fixture parsers, independent modes, aggregate states, persisted platform cooldowns, manual retry, PVR public-JSON response shapes, Cloudflare/Ray-ID detection, matching, SQLite schema, mocked notifications, health, simulation transitions, and duplicate prevention. They do not use live platform pages or real ntfy.

## Logs and retention

```bash
docker compose logs -f
docker compose logs -f web
docker compose logs -f worker
```

Docker logs rotate at 10 MB × 3 files per service. Screenshots retain the newest `SCREENSHOT_RETENTION` (default 50) through existing adapter cleanup; backups retain `BACKUP_RETENTION` (default 10). File logs, if added, belong under `/data/logs`; `LOG_RETENTION_DAYS` documents the retention target.

## Backup and restore

```bash
./scripts/backup.sh
./scripts/restore.sh data/backups/tickets-YYYYMMDDTHHMMSSZ.db
# or: make backup; make restore FILE=data/backups/FILE.db
```

Backup uses SQLite's online backup API. Restore accepts only a validated file under `data/backups`, stops the worker, backs up the current database, atomically replaces it, restarts services, and checks health.

## Updating, stopping, and uninstalling

```bash
./update.sh                  # backup, pull-build, migrate, recreate, doctor
docker compose down         # stop/remove containers and network, preserve data
./uninstall.sh              # same preservation guarantee
./uninstall.sh --remove-image
./uninstall.sh --purge      # lists targets and requires typing PURGE
./uninstall.sh --purge --yes
```

Updates and normal uninstall never remove bind-mounted data or use Docker prune commands.

## CasaOS custom app installation

Method A: create web and worker custom containers using `ghcr.io/OWNER/movie-ticket-watcher:latest`; map `/DATA/AppData/movie-ticket-watcher/data:/data` and `/DATA/AppData/movie-ticket-watcher/config:/config` on both, expose only web `8787:8787`, enter variables from `.env.example`, use `unless-stopped`, and configure 1 GB shm if exposed. Give the deployment about 2 GB memory and at least one CPU. Commands are `web` and `worker` respectively.

Method B: replace `REPOSITORY_OWNER` and import [casaos/docker-compose.yml](casaos/docker-compose.yml). Full field guidance is in [casaos/README.md](casaos/README.md). The host bind mount means recreation cannot discard SQLite. Private GHCR repositories require registry login/PAT access.

## GHCR publishing

The GitHub Actions workflow tests then publishes `ghcr.io/${{ github.repository_owner }}/movie-ticket-watcher` on `main` and `v*` tags with `latest`, semantic-version, and SHA tags. Package permissions use only `GITHUB_TOKEN`; replace the CasaOS placeholder with the derived repository owner. Only `linux/amd64` is published until ARM64 Chromium is validated.

## Permissions troubleshooting

Confirm `PUID=$(id -u)` and `PGID=$(id -g)` in `.env`, then inspect `ls -ld data config`. Containers never change host ownership. For CasaOS/NAS paths, explicitly run `sudo chown -R PUID:PGID EXACT_DATA_PATH EXACT_CONFIG_PATH`; never target a parent directory.

## Playwright and Chromium troubleshooting

Run doctor and `docker compose logs web worker`. Keep `shm_size: 1gb`; do not add `--no-sandbox`, privileged mode, or host networking. Chromium is downloaded at image build, never container startup. Playwright's bundled Linux Chromium support for ARM64 has not been validated here, so the release workflow honestly targets amd64 only; ARM64 CasaOS should build/test experimentally or use an amd64 host.

## Database-lock troubleshooting

Run only one Compose project worker. `worker.lock` rejects duplicates; stale files are harmless because the kernel lock, not file contents, is authoritative. SQLite uses WAL plus a 10-second busy timeout. Stop the worker before manual maintenance and never place the database on a filesystem with unreliable POSIX locking.

## Platform selector maintenance

BookMyShow protection pages remain `BLOCKED`; screenshots and a Cloudflare Ray ID (when present) are retained, then only that platform cools down for 30 minutes, 1 hour, 3 hours, and up to 6 hours. Configure the schedule with `BLOCKED_RETRY_*_SECONDS`. Other platforms continue normally. Manual **Retry now** bypasses one cooldown once and warns that repeated attempts may prolong a block.

PVR INOX is a JavaScript SPA: `/search?q=…` is not a show-listing document. The adapter now uses the same unauthenticated official `content/search` and `content/msessions` JSON responses loaded by the public page, first identifying an exact movie and then requesting the configured date. This integration is isolated in the PVR adapter because it is unofficial and fragile. A changed/missing response shape becomes `PARSE_UNSUPPORTED`, a search miss becomes `DISCOVERY_NO_RESULTS`, and only an identified movie/date with no bookable sessions becomes `PAGE_LOADED_NO_SHOWS`; uncertainty is never relabeled as booking unavailable.

When platform markup or the public PVR response changes, update saved fixtures and parser code together, run Docker tests, then perform a manual check. Do not add stealth, proxy rotation, CAPTCHA solving, authentication-cookie imports, or generic booking-button clicks.

## Security limitations

This is an availability notifier, not a purchasing bot. It does not log in, solve CAPTCHAs, bypass waiting rooms/queues/rate limits, select seats, or pay. Protect `.env`, use a secret ntfy topic, place TLS/authentication at a trusted reverse proxy when exposed beyond a LAN, and keep the image updated.

## Native/systemd deployment deprecation

Files in `systemd/` are legacy reference only. Docker Compose is the primary and supported deployment. Normal operation contains no `/opt/movie-ticket-watcher`, `/etc/movie-ticket-watcher`, `/var/lib/movie-ticket-watcher`, `systemctl`, `journalctl`, host Python environment, or systemd-in-container dependency.

## Raw commands and Make targets

`make build/up/down/restart/logs/logs-web/logs-worker/test/doctor/simulate/backup/restore/update` wraps the corresponding `docker compose` or documented scripts. Inspect [Makefile](Makefile) for exact raw equivalents.
