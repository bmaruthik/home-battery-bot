# Home Energy Bot (Amber + FoxESS)

Read-only watchdog. Every 30 minutes it checks Amber prices + FoxESS battery, sends
Telegram alerts, and updates a dashboard on GitHub Pages.

## Alerts
- 🔴 Spike ≥40c forecast in next 12h while battery <45%
- 🚨 Extreme price ≥100c forecast
- 🟢 Cheap power ≤8c soon while battery <60%
- ⚠️ Amber or FoxESS API unreachable (comms outage watchdog)
- ☀️ Daily 7am summary

Thresholds are at the top of `bot.py`.

## Setup (once, ~15 min)
1. Create a **private** GitHub repo, upload these files keeping the folder
   structure (`.github/workflows/bot.yml`, `bot.py`, `docs/index.html`).
2. Repo → Settings → Secrets and variables → Actions → add 4 secrets:
   - `AMBER_TOKEN` – app.amber.com.au → Developers
   - `FOX_KEY` – foxesscloud.com/user/center → API Management
   - `TG_TOKEN` – Telegram @BotFather → /newbot
   - `TG_CHAT` – message your bot once, then open
     `https://api.telegram.org/bot<TG_TOKEN>/getUpdates` and copy
     `message.chat.id`
3. Repo → Settings → Pages → Source: Deploy from branch → `main` / `/docs`.
   Your dashboard URL: `https://<username>.github.io/<repo>/`
   (Note: Pages on a private repo requires GitHub Pro; otherwise make the repo
   public — it contains no secrets — or skip Pages and rely on Telegram only.)
4. Repo → Actions → enable workflows → run "energy-bot" manually once to test.
   You should get output in the log and the first data point in `docs/data.json`.

## Notes
- FoxESS personal API key allows 1,440 calls/day; this bot uses ~100/day.
- GitHub cron can be delayed 5–15 min at busy times; fine for this purpose.
- If FoxESS changes their API auth, alerts will tell you the fox side is down.
