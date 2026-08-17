# Shift Forecast Bot — Project Plan

A Discord bot that predicts how busy my weekend outdoor gastro shifts will be based on weather, collects my real-world feedback with one-tap buttons, and validates its predictions against reality over the season.

**One-liner for resume:** My income depends on weather, so I built a system that forecasts my shifts, logs actual outcomes, and reports its own prediction accuracy.

---

## Context

- I work weekends (Sat + Sun) in outdoor gastro. Business volume is heavily weather-dependent.
- Season runs 2.5.2026 (Sat) – 4.10.2026 (Sun). Live bot tracking started 1.8.2026, mid-season — everything from 2.5. through 31.7. (13 weekends, 26 days) needs backfilling from memory; live data covers the rest.
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
   - Apply rule-based prediction (v1 = hand-written rules). Rain is the dominant signal; temperature is secondary/orientation-only — it barely matters except at the extremes (too hot and people go to the pool instead; too cold mostly only happens at the start/end of the season). Values below are orientation, not hard boundaries:

     | Verdict | Temp | Rain probability | Sky/weather |
     |---|---|---|---|
     | Slammed | 28–34°C | 0–10% | sunny / lehce zataženo |
     | Busy | 20–28°C | 10–30% | cloudy / zataženo |
     | Normal | 17–20°C or 34°C+ | 30–50% | zataženo / mrholení |
     | Slow | 14–17°C or 34°C+ | 50–80% | mrholení / slabý déšť |
     | Dead | 14–17°C or 37°C+ | 80–100% | slabý déšť – bouřky |
   - Post prediction to Discord channel: per-day verdict + key weather numbers + confidence.
   - Message renders as Components V2 (Container with the blue accent bar + a Separator between the Saturday and Sunday blocks) — same building blocks as the Monday logging message, so both messages share one visual style.
   - Include last weekend's prediction vs. logged reality ("last week I said Busy, you logged Normal").
   - Store the prediction in DB (so accuracy is checked against what was *actually predicted*, no hindsight).

2. **Logging message** (GitHub Actions cron, Monday ~08:00 local — NOT Sunday, I get home late)
   - Bot posts one Components V2 message, day header directly above its own buttons, a divider between the two days:
     - **Sobota `dd.mm`**: `[Dead] [Slow] [Normal] [Busy] [Slammed]` then `[ADD NOTE]`
     - **Neděle `dd.mm`**: same layout
   - One tap per day = full log entry. Target: under 10 seconds total on phone.
   - Clicking a busyness button again overwrites (misclick recovery); re-submitting the note modal updates in place too, without clobbering a field left blank.
   - After a busyness click, the bot edits the message in place, replacing that day's button row with `✅ Sobota dd.mm — Busy`.
   - `ADD NOTE` opens a modal (free-text `poznamka` + `sold_product` count, both optional). On submit, the bot edits the message again, inserting a `*poznámka přidána*` marker under that day's `ADD NOTE` row (no separate confirmation message) — re-submitting replaces the marker instead of stacking duplicates.

3. **Interactions endpoint** (Cloudflare Worker, TypeScript)
   - Receives Discord interaction POSTs, verifies Ed25519 request signature (required by Discord).
   - Talks to Postgres via `@neondatabase/serverless` (HTTP-based — Workers can't open raw TCP sockets, so the usual `pg` driver doesn't work here).
   - Busyness click: upserts `user_input (den, visited_id, source='live')`, looking up `visited_id` by name rather than a hardcoded id.
   - Note modal submit: `UPDATE user_input SET poznamka, sold_product WHERE den = ...` — update-only, since `visited_id` is `NOT NULL` a note can't be the first thing logged for a day (busyness button first, note is the optional follow-up).
   - Responds with a message edit (`UPDATE_MESSAGE`) in both cases — no separate ephemeral confirmations.

4. **Database** (Neon Postgres free tier) — schema in `db/schema.sql`
   - `weathers` (číselník): weather code label, from `WEATHER_CODES` in `src/forecast.py`
   - `visited` (číselník): busyness scale (velmi slabe/slabe/stredni/hodne/naval = Dead/Slow/Normal/Busy/Slammed) — shared by both the bot's predicted verdict and my logged reality, same 5-level scale either way
   - `weather_prediction`: what the bot predicted — `den` (Sat/Sun forecast) + `predikce_den` (Thu or Fri, the day the job ran) as a pair, weather numbers, `predikce_navstevnost_id` (rule engine verdict, phase 3), `created_at`
   - `user_input`: what actually happened — `den` (unique, upsert on re-click), `visited_id`, `sold_product` (units sold, via modal), `poznamka` (free-text note, via modal), `source` (live | backfill), `logged_at`
   - `weather_actual`: not yet designed — deferred to phase 4 (post-hoc actual weather from Open-Meteo archive, so end-of-season analysis can separate "bad forecast" from "bad rule")

5. **Completeness sweep** (GitHub Actions cron, weekly + end of season)
   - Query for unlogged past workdays → nag message in Discord listing missing dates, with the same button UI to fill them.
   - End of season: final sweep until dataset is 100% labeled.

6. **Backfill (one-time script)**
   - Pull historical weather 2.5.–31.7.2026 from Open-Meteo archive API.
   - Label those weekends from memory via the same Discord button flow (batch of messages).
   - Mark `source = backfill` — these labels are noisy and the analysis/writeup must say so. Distinguishing clean vs. noisy data is part of the point.

7. **End-of-season report**
   - **Code (pandas/matplotlib):** prediction accuracy overall and per rule, confusion matrix (predicted vs. actual busyness), busyness vs. temperature/rain scatter plots, live vs. backfill split, forecast-vs-actual-weather error contribution.
   - **LLM (any API):** receives the computed stats JSON only → writes narrative summary ("your rain rule held 80% of the time; the temperature threshold added nothing"). Prompt forbids inventing numbers.
   - Output: Markdown report + PNG charts, committed to repo / posted to Discord.

---

## Milestones

### Phase 1 — walking skeleton (this week)
- [x] Repo, Discord application/bot setup + empty private server (DM requirement)
- [x] GitHub Actions cron posts Friday forecast (raw weather, no rules yet) to Discord
- [x] Hardcoded workplace coordinates, Open-Meteo integration
- **Done when:** a forecast message appears in Discord automatically on Friday.

### Phase 2 — logging loop
- [x] Neon Postgres schema designed and deployed (`db/schema.sql`, run against Neon — Frankfurt, Postgres 18, Neon Auth off)
- [x] Cloudflare Worker scaffolded (`worker/`) — Ed25519 signature verification + PING handshake, typechecks and bundles clean; button/modal handlers are stubs
- [x] Worker deployed to Cloudflare + `DISCORD_PUBLIC_KEY`/`DISCORD_APPLICATION_ID` set as Worker secrets
- [x] Interactions Endpoint URL set in Discord Developer Portal (confirms signature verification works against a real PING)
- [x] Monday button message + click → DB write → message edit confirmation
- [x] Note modal (extended to also capture `sold_product` as a number field)
- **Done when:** I can log a full weekend in two taps and see the rows in the DB.

### Phase 3 — prediction + feedback
- [x] Rule engine v1 with predictions stored before the weekend
- [x] Friday message includes last weekend's prediction vs. reality
- [x] Weekly completeness sweep
- **Done when:** the bot is making falsifiable predictions and tracking its own record.

### Phase 4 — backfill + hardening
- [x] Season boundary: `season_config` DB singleton + yearly Discord-modal prompt (`src/season_reminder.py`) to set it; forecast/logging/sweep all no-op outside the configured window (fails open if never set)
- [x] Monthly keepalive commit (`.github/workflows/keepalive.yml`) — prevents GitHub's 60-day-inactivity auto-disable of scheduled workflows, which would otherwise silently kill everything (mid-season too, not just over winter — the season itself is >60 days)
- [ ] Historical weather backfill 2.5.–31.7.2026 (13 weekends, 26 days)
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

- Do work hours vary by day/month? Rules need opening-hours windows to evaluate weather against.
- Tips/earnings as a second logged metric? More signal, slightly more friction — decide in Phase 2.
- Next season: replace hand rules with a simple model trained on season 1? (Nice v2 story, out of scope now.)
- v2 idea (after v1 is fully working): factor in nearby food festivals/events as a busyness signal alongside weather — some way to check for local events near the workplace on a given weekend and account for them in the prediction. Needs a data source for local events; deferred until the weather-only pipeline is proven.
