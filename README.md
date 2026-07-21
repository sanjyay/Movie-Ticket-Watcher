# Movie Ticket Watcher

## Interactive Telegram bot

The Docker deployment has three services using the same image: `web`, `worker`, and `telegram-bot`. The bot uses outbound `getUpdates` long polling, exposes no port, persists its update offset and heartbeat in SQLite, and delegates manual checks to the existing watcher workflow.

Set `TELEGRAM_ALLOWED_CHAT_IDS` to a comma-separated list of numeric private or group chat IDs explicitly allowed to use commands. Usernames are never authorization. `TELEGRAM_ALLOWED_USER_IDS` optionally restricts numeric users inside allowed groups. The default notification destination is not implicitly authorized.

Commands are `/start`, `/newwatch`, `/search`, `/watches`, `/status`, `/check`, `/enable`, `/disable`, `/delete`, `/cancel`, and `/help`. `/search` uses the existing PVR INOX public search source. Nothing is saved before confirmation.

`/newwatch` is the primary persistent-watch wizard and accepts any typed movie title, even when it is absent from the current PVR catalogue. A no-result `/search` offers **Create watch anyway**. The wizard requires explicit city, date, language, format, time preset, and platform selection before its final summary. BookMyShow can be selected with a protection warning. All explicitly authorized chats are administrators and may manage all watches, including watches created in the web dashboard; a Telegram-created watch also stores its creating chat as the notification override.

On the first long-poll startup, updates older than `TELEGRAM_STALE_UPDATE_AGE_SECONDS` (default five minutes) are acknowledged without executing commands. Later restarts resume the persisted offset normally.

Inspect delivery retries safely with `docker compose exec -T web python -m app.cli notifications pending`. Mark only orphaned pending live deliveries cancelled with `docker compose exec -T web python -m app.cli notifications clean-orphans`; history is retained.

BookMyShow monitoring may be unavailable from this server because platform protection is active. No bypass is attempted. If `getWebhookInfo` reports a URL, the bot records a conflict and does not remove it. Stop the bot and use Telegram's `deleteWebhook` only when the administrator explicitly chooses to switch to long polling. Manual `getUpdates` may be empty while the bot is running because updates have already been consumed.

Movie Ticket Watcher is a Docker-based FastAPI dashboard and worker for BookMyShow and PVR INOX availability. Telegram is the only notification provider. Existing matching, polling, per-platform cooldowns, simulation, persisted fingerprints, and restart-safe deduplication remain shared by the web and worker containers.

## Docker and Omarchy setup

```bash
git clone YOUR_REPOSITORY_URL
cd movie_ticket_watcher
cp .env.example .env
openssl rand -hex 48             # paste into SECRET_KEY in .env
# add Telegram values described below
./install.sh
docker compose ps
curl --fail http://127.0.0.1:8787/health
```

Open `http://127.0.0.1:8787`. On Omarchy, Docker must already be installed and enabled; set `PUID=$(id -u)` and `PGID=$(id -g)` in `.env`, keep `HOST_PORT=8787`, then run `docker compose up -d`. No Omarchy desktop configuration is required. From another LAN device use `http://OMARCHY_IP:8787` only if your firewall permits it. The app has no login screen, so use a trusted LAN, VPN, or authenticated reverse proxy.

Both containers share `./data` and `./config`. SQLite, screenshots, backups, heartbeat, cooldowns, availability state, and show fingerprints survive recreation. Compose drops capabilities, enables no-new-privileges, uses the configured unprivileged UID/GID, and does not mount the Docker socket.

## Create a Telegram bot and obtain a chat ID

1. Open Telegram and message `@BotFather`.
2. Send `/newbot`, follow the prompts, and save the generated token privately.
3. Open the new bot, tap **Start** or send `/start`, then send `hello`.
4. Query updates:

```bash
BOT_TOKEN='your-token'
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe"
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"
```

Read `message.chat.id` from the response. A private-chat ID is positive. Group and channel IDs are negative (often beginning with `-100`); add the bot to the group/channel with suitable permission, send a fresh message, and inspect updates.

Test the destination explicitly:

```bash
CHAT_ID='your-chat-id'
curl -sS -X POST \
  "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=Movie Ticket Watcher test"
```

Never publish, log, or commit the token. If it leaks, use `@BotFather` to revoke/rotate it immediately, replace `TELEGRAM_BOT_TOKEN`, and recreate both containers.

## Configure Telegram in Docker

Set only these notification variables in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_DEFAULT_CHAT_ID=
TELEGRAM_API_BASE=https://api.telegram.org
```

The token is environment-only and is never stored in SQLite or shown in the UI, health output, diagnostics, or delivery errors. `.env` is Git-ignored. A watch may override the default chat ID in its edit form. Leaving the override empty uses `TELEGRAM_DEFAULT_CHAT_ID`. Enable or disable **Telegram notifications enabled** independently on each watch.

After changing `.env`, run `docker compose up -d --force-recreate`. Click **Test Telegram** for an explicit test that does not change availability, fingerprints, deduplication, or last status. Command-line equivalents are:

```bash
docker compose exec -T web python scripts/doctor.py --test-telegram
# or, using the first saved watch's effective chat ID:
docker compose exec -T web python scripts/test_notification.py
```

The normal `python scripts/doctor.py` never sends a message.

## Troubleshooting Telegram

`{"ok":true,"result":[]}` means the token is valid but no unread bot updates are available. Message the correct bot, verify it with `getMe`, check `getWebhookInfo`, remove a webhook if `getUpdates` is needed, send a fresh message, and try again.

- **Bot blocked by user:** unblock/open the bot and send `/start` again.
- **Chat not found:** verify the signed chat ID, ensure the bot belongs to the group/channel, and send a fresh message.
- **Empty updates:** confirm the correct bot and token; another `getUpdates` consumer may already have acknowledged the update.
- **Leaked token:** revoke it with `@BotFather`, update `.env`, and recreate web and worker.

## Watches, checks, and deduplication

Create a watch with movie, city, date, language, format, time preference, optional theatres, and independent BookMyShow/PVR INOX modes. Automatic discovery and direct URL behavior are unchanged. The result page can retry or disable one platform, clear its discovery cache, inspect screenshots, and download sanitized diagnostics.

New matching fingerprints are stored before delivery. Successful sends mark only that fingerprint; failures retain it for bounded later retry. Repeated polls, manual checks, simulation repeats, and container restarts do not resend successfully delivered fingerprints. Telegram test history is stored separately from availability-delivery history.

For offline validation, run simulation as `UNAVAILABLE`, then `AVAILABLE`, then repeat `AVAILABLE`. The first available transition sends once per genuinely new platform fingerprint; repeats send nothing. Automated tests mock Telegram and never send real messages.

## CasaOS

Replace `REPOSITORY_OWNER` and import [casaos/docker-compose.yml](casaos/docker-compose.yml). Enter the three Telegram variables in both services, share the data/config mounts, expose only web port 8787, and recreate both containers. The exact 15-step bot setup and curl commands are in [casaos/README.md](casaos/README.md).

## Health, doctor, tests, backup, and updates

```bash
docker compose config
docker compose exec -T web python scripts/doctor.py
docker compose run --rm web pytest
docker compose run --rm test ruff check --no-cache app scripts tests
curl --fail http://127.0.0.1:8787/health
./scripts/backup.sh
./update.sh
```

Health reports only `telegram_configured: true|false`, never a token or chat ID. Telegram being unconfigured does not make web/worker health fail. Doctor validates the HTTPS API base, token/chat-ID presence, DNS, HTTPS, SQLite, writable paths, browser, web, heartbeat, identity, and timezone without sending an alert.

The Telegram migration makes a timestamped `pre-telegram-*.db` backup before altering an existing SQLite database, adds the override and plural notification flag, preserves all monitoring/history tables, and leaves old notification columns physically present only when inherited from an older database. Those legacy columns are ignored by the active application.

## Security and platform limitations

This is an availability notifier, not a purchasing bot. It does not log in, solve CAPTCHAs, bypass queues/rate limits, select seats, or pay. BookMyShow protection remains `BLOCKED` with persisted cooldowns; PVR INOX continues using its isolated public-page JSON adapter. Keep `.env` private, keep images updated, and do not add privileged mode, host networking, stealth, CAPTCHA solving, or authentication-cookie imports.
