# CasaOS deployment

CasaOS requires all three services: web serves the dashboard, worker monitors watches, and telegram-bot performs outbound long polling. Give all three the same `/data` and `/config` mounts and environment, expose port 8787 only from web, and set `TELEGRAM_ALLOWED_CHAT_IDS` explicitly. Long polling requires no port forwarding.

Confirm health with `docker compose ps` and inspect interaction logs with `docker compose logs telegram-bot`. Manual `getUpdates` can be empty while the bot consumes updates. For diagnostics, run `docker compose stop telegram-bot`, send the bot a fresh message, call `getUpdates`, then `docker compose start telegram-bot`. If `getWebhookInfo` returns a URL, remove it with `deleteWebhook` only after choosing to abandon that webhook; the application never deletes it automatically.

Replace `REPOSITORY_OWNER` in `docker-compose.yml`, generate `SECRET_KEY` with `openssl rand -hex 48`, and import the Compose file. Both the web and worker containers must receive `TELEGRAM_BOT_TOKEN`, `TELEGRAM_DEFAULT_CHAT_ID`, and `TELEGRAM_API_BASE`. Keep the API base at `https://api.telegram.org`.

Map `/DATA/AppData/movie-ticket-watcher/data:/data` and `/DATA/AppData/movie-ticket-watcher/config:/config` in both containers. Expose only web port `8787`, use commands `web` and `worker`, and never enable privileged mode or mount the Docker socket. Recreating containers preserves SQLite and screenshots in the data mount.

## Telegram setup

1. Open Telegram.
2. Open `@BotFather`.
3. Send `/newbot`.
4. Save the generated bot token privately.
5. Open the newly created bot.
6. Tap **Start** or send `/start`.
7. Send a message such as `hello`.
8. Obtain the chat ID with `getUpdates`.
9. Test `sendMessage` with curl.
10. Enter `TELEGRAM_BOT_TOKEN` in CasaOS.
11. Enter `TELEGRAM_DEFAULT_CHAT_ID` in CasaOS.
12. Keep `TELEGRAM_API_BASE` as `https://api.telegram.org`.
13. Recreate or restart both containers.
14. Open Movie Ticket Watcher.
15. Click **Test Telegram** on a watch.

```bash
BOT_TOKEN='your-token'

curl -s \
  "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"

CHAT_ID='your-chat-id'

curl -sS -X POST \
  "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=Movie Ticket Watcher test"
```

Never publish or commit the bot token. Private chat IDs are positive; group and channel IDs are normally negative. If permissions fail, run `sudo chown -R PUID:PGID /DATA/AppData/movie-ticket-watcher/{data,config}` only after confirming those exact paths.
