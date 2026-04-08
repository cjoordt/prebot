# Ultra Coach — AI Running Coach

An AI running coach that texts you via Telegram, syncs with Strava and Google Calendar, generates weekly training plans, and adjusts dynamically based on daily check-ins.

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Local Dev Setup](#local-dev-setup)
3. [Strava: Get Your Refresh Token](#strava-get-your-refresh-token)
4. [Google Calendar: OAuth Setup](#google-calendar-oauth-setup)
5. [Find Your Calendar IDs](#find-your-calendar-ids)
6. [Environment Variables Reference](#environment-variables-reference)
7. [Deploy to Railway](#deploy-to-railway)
8. [Scheduled Jobs](#scheduled-jobs)
9. [Slash Commands](#slash-commands)

---

## Project Structure

```
ultra-coach/
├── main.py              # Entry point — starts bot + scheduler
├── bot.py               # Telegram send/receive + conversation logging
├── agent.py             # Claude brain — context assembly + response routing
├── scheduler.py         # APScheduler cron jobs
├── state.py             # Conversation flow state machine
├── integrations/
│   ├── strava.py        # Strava OAuth2 + activity fetch + cache
│   └── calendar.py      # Google Calendar OAuth2 + day tagging
├── tools/
│   ├── fatigue.py       # ATL/CTL fatigue model
│   ├── planner.py       # Weekly plan generator (Claude)
│   └── parser.py        # Natural language check-in parser (Claude)
├── data/
│   ├── weekly_plan.json
│   ├── activity_log.json
│   ├── conversation.json
│   ├── strava_cache.json
│   └── state.json
├── prompts/
│   ├── system.txt
│   ├── weekly_plan.txt
│   ├── evening_checkin.txt
│   └── missed_workout.txt
├── requirements.txt
├── Procfile
└── railway.toml
```

---

## Local Dev Setup

```bash
# Clone and enter the project
cd ultra-coach

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy the env template and fill in your values (see sections below)
cp .env.example .env

# Run locally
python main.py
```

Create `.env.example`:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
STRAVA_REFRESH_TOKEN=
GOOGLE_CALENDAR_CREDENTIALS_PATH=credentials.json
GOOGLE_CALENDAR_IDS=primary,your_family_calendar_id@group.calendar.google.com
```

---

## Strava: Get Your Refresh Token

You need three values: `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, and `STRAVA_REFRESH_TOKEN`.

### 1. Create a Strava API application

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api) and log in.
2. Fill in the form:
   - **Application Name**: Ultra Coach (or anything)
   - **Category**: Training
   - **Website**: `http://localhost`
   - **Authorization Callback Domain**: `localhost`
3. Click **Create**. You'll see your `Client ID` and `Client Secret` — copy both.

### 2. Authorize your account and get the refresh token

Run this in a browser, replacing `YOUR_CLIENT_ID`:

```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:read_all
```

1. Click **Authorize** on the Strava page.
2. You'll be redirected to `http://localhost/?code=XXXXXXXX` — the URL won't load, but **copy the `code` value** from it.

Now exchange that code for a refresh token. Run this in your terminal (replace all three values):

```bash
curl -X POST https://www.strava.com/oauth/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d code=YOUR_CODE \
  -d grant_type=authorization_code
```

The JSON response contains `"refresh_token"` — copy that value into your `.env` as `STRAVA_REFRESH_TOKEN`.

---

## Google Calendar: OAuth Setup

### 1. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in with the Google account that owns your calendars.
2. Click **Select a project** → **New Project**.
3. Name it `Ultra Coach` and click **Create**.

### 2. Enable the Google Calendar API

1. In the left sidebar go to **APIs & Services → Library**.
2. Search for **Google Calendar API** and click **Enable**.

### 3. Create OAuth credentials

1. Go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. If prompted, configure the OAuth consent screen first:
   - User type: **External**
   - App name: `Ultra Coach`
   - Add your email as a test user
   - Scopes: add `https://www.googleapis.com/auth/calendar.readonly`
4. Back in **Create OAuth client ID**:
   - Application type: **Desktop app**
   - Name: `Ultra Coach`
5. Click **Create**, then **Download JSON**.
6. Save the downloaded file as `credentials.json` in the project root.
7. Set `GOOGLE_CALENDAR_CREDENTIALS_PATH=credentials.json` in your `.env`.

### 4. Run the auth flow (once, locally)

```bash
python -c "from integrations.calendar import fetch_week_schedule; print(fetch_week_schedule())"
```

A browser window will open asking you to authorize access. Click through and allow. This creates `token.json` in the same directory as `credentials.json`. You only need to do this once — the token auto-refreshes after that.

---

## Find Your Calendar IDs

You need the IDs for both your work calendar and your family calendar.

1. Go to [calendar.google.com](https://calendar.google.com).
2. In the left sidebar, hover over a calendar name and click the **⋮ menu → Settings and sharing**.
3. Scroll down to **Integrate calendar** — you'll see the **Calendar ID**.
   - Your primary/work calendar ID is usually your email address.
   - Family calendars have an ID like `abc123xyz@group.calendar.google.com`.
4. Copy both IDs and set them as a comma-separated list:

```env
GOOGLE_CALENDAR_IDS=you@gmail.com,familyid@group.calendar.google.com
```

---

## Environment Variables Reference

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot's token from BotFather |
| `TELEGRAM_CHAT_ID` | Your personal Telegram user ID |
| `ANTHROPIC_API_KEY` | From [console.anthropic.com](https://console.anthropic.com) |
| `STRAVA_CLIENT_ID` | From strava.com/settings/api |
| `STRAVA_CLIENT_SECRET` | From strava.com/settings/api |
| `STRAVA_REFRESH_TOKEN` | Generated in the Strava auth flow above |
| `GOOGLE_CALENDAR_CREDENTIALS_PATH` | Path to `credentials.json` (default: `credentials.json`) |
| `GOOGLE_CALENDAR_IDS` | Comma-separated calendar IDs (work + family) |
| `GOOGLE_TOKEN_JSON` | **Railway only** — contents of `token.json` (see Deploy section) |

---

## Deploy to Railway

### 1. Create a Railway project

1. Go to [railway.app](https://railway.app) and sign in with GitHub.
2. Click **New Project → Deploy from GitHub repo**.
3. Select this repository.
4. Railway will detect the `Procfile` and use `python main.py` as the start command.

### 2. Set environment variables

In the Railway dashboard, go to your service → **Variables** and add every variable from the table above.

For `GOOGLE_TOKEN_JSON`, paste the **entire contents** of your local `token.json` file as a single-line string. You can get it with:

```bash
cat token.json | tr -d '\n'
```

> **Note:** `credentials.json` is not needed on Railway — the token is refreshed directly from `GOOGLE_TOKEN_JSON`. Do not set `GOOGLE_CALENDAR_CREDENTIALS_PATH` in the Railway environment.

### 3. Deploy

Railway deploys automatically on every push to your main branch. To trigger a manual deploy, click **Deploy** in the Railway dashboard.

### 4. Verify it's running

Check the Railway logs — you should see:

```
Ultra Coach starting — polling for updates...
Scheduler started.
```

Send `/status` to your Telegram bot. It should reply with the bot status and scheduler state.

### Redeploying after a token refresh

The Strava refresh token is long-lived and does not need to be rotated. The Google token auto-refreshes in memory but the updated token is not written back to `GOOGLE_TOKEN_JSON` on Railway. If Calendar auth ever fails after a long period:

1. Re-run the local auth flow to regenerate `token.json`.
2. Update the `GOOGLE_TOKEN_JSON` variable in Railway with the new contents.
3. Redeploy.

---

## Scheduled Jobs

| Job | Schedule (Pacific) | Description |
|---|---|---|
| Weekly plan | Sunday 7:00 pm | Pulls Strava + Calendar, generates plan via Claude, sends to Telegram |
| Morning Strava check | Daily 11:00 am | Checks for morning activities, notes deviations from plan |
| Evening Strava check | Daily 7:00 pm | Checks for activities; triggers missed workout flow if nothing logged |
| Evening check-in | Daily 9:00 pm | Asks about sleep, nutrition, legs — or runs missed workout flow |

---

## Slash Commands

| Command | Description |
|---|---|
| `/status` | Health check — shows flow state and scheduler status |
| `/reset` | Resets conversation flow to freeform (use if the bot seems stuck) |
