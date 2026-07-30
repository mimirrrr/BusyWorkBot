# Shift Forecast Bot — Project Plan

A Discord bot that predicts how busy my weekend outdoor gastro shifts will be based on weather, collects my real-world feedback with one-tap buttons, and validates its predictions against reality over the season.

**One-liner for resume:** My income depends on weather, so I built a system that forecasts my shifts, logs actual outcomes, and reports its own prediction accuracy.

---

## Context

- I work weekends (Sat + Sun) in outdoor gastro. Business volume is heavily weather-dependent.
- Season runs end of April → end of September. Starting mid-season (late July 2026) — ~9 weekends of live data left this season, plus backfill for April–July.
- Primary goals: (1) actually useful to me, (2) portfolio project with honest, verifiable metrics.

## Core principle

**Code computes, AI narrates.** All statistics, accuracy numbers, and graphs come from deterministic code (pandas/matplotlib). An LLM only ever receives pre-computed results and writes the narrative summary. It never calculates anything. This is a deliberate architectural decision and belongs in the writeup.

---

## Architecture

```
GitHub Actions (cron)                    Cloudflare Worker (serverless)
├─ Thu+Fri: forecast messages ─► Discord ◄── button click interactions
├─ Monday: logging message      channel        │ verify signature
└─ Sunday PM: season sweep                     ▼
                                         Postgres (Neon free tier)
                                               │
                                End-of-season report pipeline
                                (pandas + matplotlib → LLM narrative)
```

**No always-on server.** Scheduled sends via GitHub Actions cron hitting Discord webhook/API. Button clicks handled by Discord Interactions endpoint (HTTP POST) on a Cloudflare Worker free tier.

**All messages go to my DMs, not a channel.** The bot opens a DM channel via the API (`POST /users/@me/channels` with my user ID) and sends everything there — forecasts, button messages, nags. Buttons and modals work in DMs identically. Discord requirement: a bot can only DM users it shares a server with, so create one empty private server, invite the bot, mute it, never open it. It exists purely to satisfy that rule.

### Components

1. **Forecast job** (Python, GitHub Actions cron, Thursday ~17:00 AND Friday ~17:00 local)
   - Runs twice: Thursday gives the early picture, Friday gives the updated one (forecasts often shift on Friday).
   - Friday's message highlights what changed vs. Thursday ("rain chance Sat jumped 20% → 65%, verdict downgraded Busy → Slow"). The Friday prediction is the official one used for accuracy scoring; Thursday's is stored too, so I can later measure how much forecasts drift day-to-day.
   - Pull Sat+Sun forecast for workplace coordinates from Open-Meteo (free, no API key): temp, precipitation probability + amount, wind, cloud cover, hourly resolution for opening hours.
   - Apply rule-based prediction (v1 = hand-written rules, e.g. "rain prob >60% during opening hours → Dead/Slow", "≥30°C sunny → Slammed").
   - Post prediction to Discord channel: per-day verdict + key weather numbers + confidence.
   - Include last weekend's prediction vs. logged reality ("last week I said Busy, you logged Normal").
   - Store the prediction in DB (so accuracy is checked against what was *actually predicted*, no hindsight).

2. **Logging message** (GitHub Actions cron, Monday ~08:00 local — NOT Sunday, I get home late)
   - Bot posts one message with button rows:
     - Saturday: `[Dead] [Slow] [Normal] [Busy] [Slammed]`
     - Sunday: `[Dead] [Slow] [Normal] [Busy] [Slammed]`
     - Optional `[+ note]` button per day → opens Discord modal for free-text (e.g. "rained till noon, dead till 2").
   - One tap per day = full log entry. Target: under 10 seconds total on phone.
   - Clicking a button again overwrites (misclick recovery).
   - After click, bot edits the message to show `✅ Saturday: Busy` as confirmation.

3. **Interactions endpoint** (Cloudflare Worker, TypeScript)
   - Receives Discord interaction POSTs, verifies Ed25519 request signature (required by Discord).
   - Writes/updates the row in Postgres: `(date, busyness, note, logged_at)`.
   - Responds with message edit (confirmation state).

4. **Database** (Neon Postgres free tier)
   - `predictions`: date, predicted_busyness, forecast_json, rules_fired, created_at
   - `outcomes`: date, actual_busyness (1–5), note, source (live | backfill), logged_at
   - `weather_actual`: date, actual weather pulled post-hoc from Open-Meteo archive (so accuracy analysis can separate "bad forecast" from "bad rule").

5. **Completeness sweep** (GitHub Actions cron, weekly + end of season)
   - Query for unlogged past workdays → nag message in Discord listing missing dates, with the same button UI to fill them.
   - End of season: final sweep until dataset is 100% labeled.

6. **Backfill (one-time script)**
   - Pull historical weather April → July from Open-Meteo archive API.
   - Label those weekends from memory via the same Discord button flow (batch of messages).
   - Mark `source = backfill` — these labels are noisy and the analysis/writeup must say so. Distinguishing clean vs. noisy data is part of the point.

7. **End-of-season report**
   - **Code (pandas/matplotlib):** prediction accuracy overall and per rule, confusion matrix (predicted vs. actual busyness), busyness vs. temperature/rain scatter plots, live vs. backfill split, forecast-vs-actual-weather error contribution.
   - **LLM (any API):** receives the computed stats JSON only → writes narrative summary ("your rain rule held 80% of the time; the temperature threshold added nothing"). Prompt forbids inventing numbers.
   - Output: Markdown report + PNG charts, committed to repo / posted to Discord.

---

## Milestones

### Phase 1 — walking skeleton (this week)
- [ ] Repo, Discord application/bot setup + empty private server (DM requirement)
- [ ] GitHub Actions cron posts Friday forecast (raw weather, no rules yet) to Discord
- [ ] Hardcoded workplace coordinates, Open-Meteo integration
- **Done when:** a forecast message appears in Discord automatically on Friday.

### Phase 2 — logging loop
- [ ] Neon Postgres schema
- [ ] Cloudflare Worker interactions endpoint with signature verification
- [ ] Monday button message + click → DB write → message edit confirmation
- [ ] Note modal
- **Done when:** I can log a full weekend in two taps and see the rows in the DB.

### Phase 3 — prediction + feedback
- [ ] Rule engine v1 with predictions stored before the weekend
- [ ] Friday message includes last weekend's prediction vs. reality
- [ ] Weekly completeness sweep
- **Done when:** the bot is making falsifiable predictions and tracking its own record.

### Phase 4 — backfill + hardening
- [ ] Historical weather backfill April–July
- [ ] Memory-labeling flow for past weekends (marked as backfill)
- [ ] Error handling: API downtime, Actions cron quirks, timezone correctness (Europe/Prague), retries
- **Done when:** dataset covers the whole season to date and jobs survive a failed API call.

### Phase 5 — end-of-season report (early October)
- [ ] Analysis notebook/script → stats + charts
- [ ] LLM narrative layer on computed results
- [ ] Public writeup: architecture, honest accuracy numbers, what failed, what I'd measure differently next season
- **Done when:** report is published and linked from the repo README + resume.

---

## Proof moves (for resume/writeup)

- Prediction accuracy measured over N live weekends, stated honestly (small N is fine, vague claims are not).
- "Bot enforces data completeness" — automated nagging until every workday is labeled.
- Serverless interactions architecture (no idle server for two clicks a week).
- Clean separation: deterministic analysis vs. LLM narration, with the reasoning documented.
- System keeps running next season — this is an ongoing pipeline, not a finished demo.
- README section: "what made this hard" (signature verification, cron/timezones, noisy backfill labels).

## Stack summary

| Piece | Choice | Why |
|---|---|---|
| Scheduled jobs | GitHub Actions cron | free, no server |
| Weather | Open-Meteo (forecast + archive) | free, no key, historical data |
| Chat/UI | Discord bot + message components (buttons, modals) | I live there; one-tap UX |
| Click handling | Cloudflare Worker (interactions endpoint) | serverless, free tier |
| DB | Neon Postgres free tier | real SQL, free |
| Analysis | Python, pandas, matplotlib | deterministic, reproducible |
| Narrative | any LLM API | summarizes computed stats only |

## Secrets / config

`DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_USER_ID` (bot DMs me directly, no channel), `DATABASE_URL`, `LLM_API_KEY`, workplace `LAT`/`LON`, timezone `Europe/Prague`. Stored in GitHub Actions secrets + Worker secrets. Never in the repo.

Workplace: Bělá 87, 747 23 Bělá (Opava district, CZ). Resolve exact `LAT`/`LON` once via Open-Meteo's geocoding API (https://geocoding-api.open-meteo.com/v1/search?name=B%C4%9Bl%C3%A1) or maps, hardcode as config (approx. 49.9 N, 18.1 E — verify).

## Open questions

- Exact busyness scale semantics — define each of the 5 levels once, in writing, before logging starts (label consistency matters more than label granularity).
- Do work hours vary by day/month? Rules need opening-hours windows to evaluate weather against.
- Tips/earnings as a second logged metric? More signal, slightly more friction — decide in Phase 2.
- Next season: replace hand rules with a simple model trained on season 1? (Nice v2 story, out of scope now.)
