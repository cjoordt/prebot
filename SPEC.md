# Ultra Coach — Full Project Spec

## Overview
An AI running coach that texts me via Telegram, syncs with 
Strava and Google Calendar, generates weekly training plans, 
and adjusts dynamically based on my daily check-ins.

## Tech Stack
- Python 3.11+
- python-telegram-bot
- Anthropic Claude API (claude-opus-4-5)
- APScheduler
- Strava API (OAuth2)
- Google Calendar API (OAuth2)
- python-dotenv
- Deployable to Railway.app

## Project Structure
ultra-coach/
├── .env
├── main.py
├── scheduler.py
├── bot.py
├── agent.py
├── integrations/
│   ├── strava.py
│   └── calendar.py
├── tools/
│   ├── fatigue.py
│   ├── planner.py
│   └── parser.py
├── data/
│   ├── weekly_plan.json
│   ├── activity_log.json
│   ├── conversation.json
│   └── strava_cache.json
├── prompts/
│   ├── system.txt
│   ├── weekly_plan.txt
│   ├── evening_checkin.txt
│   └── missed_workout.txt
├── requirements.txt
├── Procfile               # For Railway deployment
└── README.md

## Athlete Context (hardcode in system.txt)
- Target race: Wyeast Wonder 50k, technical trail
- Home trail: Wildwood Trail, Portland OR
- Current base: ~40 miles/week
- Schedule: Remote sales, frequent travel, two young kids
  (sleep disruption is a real training variable)
- Member at Rock Creek Country Club (golf — relevant 
  for cross-training and fatigue)

## The 5 Workflows

### 1. Sunday Weekly Plan — 6am Pacific
- Pull 4 weeks Strava data
- Calculate ATL/CTL fatigue score
- Read Google Calendar next 7 days, tag each day:
  open / travel / busy-morning / busy-afternoon / blocked
- Generate weekly plan with daily workouts
- Send via Telegram, ask for confirmation
- Accept natural language adjustments, reconfirm

### 2. Evening Check-In — 9pm Pacific daily
- If run was logged today: ask about sleep, alcohol, 
  nutrition, legs, anything else
- If rest day: same questions, shorter tone
- If missed planned run: use missed workout flow instead
- Parse natural language replies and log to activity_log.json

### 3. Strava Check — 8am and 6pm Pacific
- Pull latest activities, compare to today's plan
- If new activity found: log it, note any major deviation
- If 6pm and no activity but workout was planned: 
  trigger missed workout flow

### 4. Missed Workout Detection
- Ask what happened, no judgment
- Accept reply and adjust remaining week accordingly
- Re-send updated plan, confirm

### 5. Anytime Freeform Messages
- User can text anything at any time
- Agent reads: last 20 messages + current weekly plan + 
  today's Strava data + fatigue score + calendar
- Responds intelligently, updates plan if needed

## Data Models

### weekly_plan.json
{
  "week_of": "2026-04-06",
  "target_miles": 42,
  "target_elevation_ft": 4800,
  "days": {
    "mon": {
      "type": "rest",
      "reason": "travel day"
    },
    "tue": {
      "type": "easy",
      "miles": 7,
      "notes": "HR under 140, Wildwood"
    },
    "wed": {
      "type": "tempo",
      "miles": 8,
      "structure": "3x10min threshold"
    },
    "thu": { "type": "easy", "miles": 5 },
    "fri": { "type": "rest" },
    "sat": {
      "type": "long",
      "miles": 16,
      "elevation_ft": 2800
    },
    "sun": { "type": "easy", "miles": 6 }
  },
  "actuals": {}   // filled in as week progresses
}

### activity_log.json
{
  "entries": [
    {
      "date": "2026-04-03",
      "sleep_hours": 6.5,
      "sleep_quality": 3,
      "alcohol_drinks": 2,
      "nutrition": "ok",
      "legs": "heavy",
      "stress": "moderate",
      "notes": "Ben was up at 3am"
    }
  ]
}

### conversation.json
{
  "messages": [
    {
      "timestamp": "2026-04-03T21:00:00",
      "role": "assistant",
      "content": "Nice work on today's 7mi..."
    },
    {
      "timestamp": "2026-04-03T21:02:00", 
      "role": "user",
      "content": "slept 7hrs, no drinks, legs good"
    }
  ]
}

## Fatigue Model (ATL/CTL)
- Load each run = distance_miles * effort_multiplier
- effort_multiplier: easy=1.0, moderate=1.3, hard=1.6
- ATL = 7-day exponential weighted average of daily load
- CTL = 42-day exponential weighted average of daily load  
- Form = CTL - ATL
- Recommendation:
    form < -20 → back off significantly
    form -20 to -5 → normal training
    form -5 to +5 → neutral
    form > +5 → can push, freshness is high

## Agent Behavior
- Max response length: 150 words (it's a text message)
- Tone: direct, no fluff, like a coach not a chatbot
- Always acknowledge what the athlete said before advising
- Never lecture about alcohol or sleep — note it, adjust, move on
- When plan changes, always re-state the affected days
- Remember: undertrained beats overtrained for a first 50k

## Environment Variables Needed
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
STRAVA_REFRESH_TOKEN=
GOOGLE_CALENDAR_CREDENTIALS_PATH=
GOOGLE_CALENDAR_ID=

## Build Order
1. bot.py + conversation logging (verify Telegram works)
2. strava.py (OAuth + activity fetch + cache)
3. calendar.py (OAuth + day tagging)
4. fatigue.py (ATL/CTL calculator)
5. planner.py (weekly plan generator)
6. agent.py (Claude brain, builds full context)
7. scheduler.py (all 4 cron jobs)
8. main.py (ties everything together)
9. Procfile + Railway deployment config
10. README with setup instructions for every OAuth flow