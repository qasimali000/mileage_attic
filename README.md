# The Mileage Attic 🚗

A small web app for turning raw mileage notes into a tidy, claim-ready CSV.

Paste your notes — date headers, one postcode per line, wait times scribbled after the postcode — and the app works out every journey's driving distance, daily totals, waiting pay, and hands you a CSV.

## How it works

1. **Paste notes** in the format you already write them:

   ```
   6 June

   GL1 1YF
   GL1 4SY
   GL4 6HG - 1 hour 10 minutes wait
   GL4 6JQ

   Email for barbara finch   <- non-postcode lines are ignored with a warning
   ```

   - A date line (`6 June`, `28th June`) starts a new day.
   - Consecutive postcodes chain into journeys (`GL1 1YF → GL1 4SY`, `GL1 4SY → GL4 6HG`, …).
   - A blank line breaks the chain (new trip, no journey across the gap).
   - Wait annotations (`- 30 min wait`, `wait 1 hour 10 minutes`) attach to the journey leaving that stop.

2. **Set your details** — name, month, and £/mile rate (defaults to £0.45; waiting time is paid at £13.40/hour).

3. **Calculate** — progress streams live while each journey is geocoded ([postcodes.io](https://postcodes.io)) and routed ([OpenRouteService](https://openrouteservice.org), driving-car profile). Repeated postcode pairs are served instantly from a local SQLite cache.

4. **Download CSV** — includes your name and month at the top, one row per journey (with the daily amount on each day's first row), and an overall summary: total miles, mileage amount, waiting time and pay, grand total. Filename comes out as `name_month_mileage.csv`.

## Stack

- **Backend** — Python / Flask, single file ([app.py](app.py)). Results stream over Server-Sent Events, so long runs show live progress instead of a frozen page.
- **Frontend** — single static page ([static/index.html](static/index.html)), no build step. Vintage-editorial theme: cream, chunky offset shadows, pill buttons, checkerboard dividers.
- **Cache** — SQLite table of postcode-pair distances (`distance_cache.db`, created automatically).

## Running locally

```bash
pip install -r requirements.txt
export ORS_API_KEY="your-openrouteservice-key"   # free key from openrouteservice.org
python app.py
# open http://localhost:5001
```

## Deploying

Deployed on [Railway](https://railway.com) using the included `Procfile` (gunicorn). Set two environment variables on the service:

| Variable | Purpose |
|----------|---------|
| `ORS_API_KEY` | OpenRouteService API key for driving distances |
| `DATABASE_URL` | Railway Postgres connection string (users, sessions, run log). Without it, a local `users.db` SQLite file is used |
| `SECRET_KEY` | Long random string used to hash login codes and session tokens |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | Send login codes via the Gmail API over HTTPS (Railway Hobby blocks SMTP). Get the refresh token with `scripts/gmail_token.py` |
| `EMAIL_FROM` | Sender, e.g. `The Mileage Attic <you@gmail.com>` |
| `ADMIN_TOKEN` | Bearer token for `GET /admin/stats` |

Alternatively set `RESEND_API_KEY` (needs a verified domain) instead of the Gmail vars. With no provider configured locally, codes are printed to the server console.

## Login & usage stats

Users enter their email, receive a 6-digit code (10 min expiry, 5 attempts, rate-limited), and get a 30-day session cookie. Each calculation is logged against the user's email.

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" https://<your-app>/admin/stats
```

returns total users, new users (7d/30d), active users (7d/30d), total runs and miles.

Note: the filesystem on Railway is ephemeral, so the distance cache resets on each deploy and rebuilds itself with use.

## API

`POST /calculate` — requires a session cookie from `/auth/verify`. JSON body:

```json
{
  "notes": "6 June\nGL1 1YF\nGL1 4SY\n…",
  "rate_per_mile": 0.45,
  "name": "Your Name",
  "month": "June"
}
```

Responds with an SSE stream: `warning` events for skipped lines, `progress` events per journey, and a final `done` event containing rows, daily totals, summary, and the complete CSV.
