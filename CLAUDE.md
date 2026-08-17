# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Discord bot that forecasts how busy the user's weekend outdoor-gastro shifts will be, based on weather. It DMs a Thu/Fri forecast, collects real outcomes via one-tap Discord buttons on Monday, and (eventually) reports prediction accuracy over the season. Full plan and current phase status: `docs/PLAN.md` (read this before making architectural changes — it defines the phase roadmap and what's intentionally deferred).

Core design principle: **code computes, AI narrates.** All stats/accuracy/graphs come from deterministic Python (pandas/matplotlib in phase 5); an LLM only ever narrates precomputed results and must never calculate anything.

## Two independent runtimes

This repo has two separately-deployed pieces that only communicate through Postgres — there is no shared process or shared code between them.

1. **`src/` (Python)** — one-shot scripts triggered by GitHub Actions cron, each running for a few seconds and exiting. No persistent server. Every script except `season_reminder.py` calls `is_in_season()` right after computing `today` and returns early (no Discord call, no DB writes) outside the configured `season_config` window — see "Season boundary" below.
   - `src/forecast.py` — Thu+Fri 17:00 Prague: pulls Open-Meteo forecast, runs the rule engine (`predict_verdict` — rain sets the base tier, extreme temp demotes one tier), DMs a Components V2 message to Discord, writes the prediction to `weather_prediction` (`store_predictions`). Friday's run only (the "official" prediction) prepends a second Container comparing last weekend's stored prediction against what was actually logged (`fetch_last_weekend_comparison`) — degrades gracefully (no crash) when either side is missing.
   - `src/log_message.py` — Monday 08:00 Prague: DMs the busyness-logging buttons message.
   - `src/completeness_sweep.py` — Tuesday 21:00 Prague: DMs a nag (reusing `log_message.py`'s button/custom_id scheme) for any past Sat/Sun since the first-ever logged day with no `user_input` row. Sends nothing when nothing's missing.
   - `src/season_reminder.py` — once yearly (~April 20): DMs a button that opens a Discord modal to set `season_config`. Doesn't touch the DB itself — the Worker's `season_modal` handler does the write.
   - Deployed via `.github/workflows/forecast.yml`, `monday-log.yml`, `completeness-sweep.yml`, `season-reminder.yml`.

2. **`worker/` (TypeScript, Cloudflare Worker)** — the always-listening piece, but serverless (no idle cost). Handles Discord's interaction POSTs: Ed25519 signature verification, button clicks, and modal submits. Reads/writes Postgres via `@neondatabase/serverless` (HTTP-based — Workers can't open raw TCP sockets, so the normal `pg` driver won't work here; don't add it).

Both sides send **DMs only, never a channel message** — the bot opens a DM channel per-send via `POST /users/@me/channels`. This requires the bot to share a server with the user (one muted, empty private server exists solely to satisfy that Discord rule).

## Commands

Python (`src/`), run from repo root:
```
pip install -r requirements.txt
python src/forecast.py --dry-run           # prints Discord payload JSON, no network/Discord calls
python src/log_message.py --dry-run
python src/completeness_sweep.py --dry-run # needs a real DATABASE_URL even in --dry-run — nothing to preview otherwise
python src/season_reminder.py --dry-run
```
For a real local send, copy `.env.example` to `.env` (gitignored) and fill in `DISCORD_BOT_TOKEN`/`DISCORD_USER_ID`/`DATABASE_URL`. `--dry-run` needs no `.env` except for `completeness_sweep.py` (always) and optionally `forecast.py`/`log_message.py` (only to preview DB-dependent parts locally).

Worker (`worker/`):
```
cd worker
npm run typecheck   # tsc --noEmit — run this after any index.ts change
npm run dev          # wrangler dev, local Worker testing
npm run deploy       # wrangler deploy
```
Local Worker dev needs `worker/.dev.vars` (copy from `.dev.vars.example`, gitignored).

There is no JS/TS test runner and no Python test suite configured yet — verification is `--dry-run` for the Python scripts and `npm run typecheck` for the Worker.

## Database

Schema lives in `db/schema.sql`, applied by hand against Neon Postgres (Frankfurt) — there is no migration tool. When changing the schema, edit `db/schema.sql` and note in the PR/commit that it must be re-run manually against Neon.

Two lookup tables (číselníky) and two fact tables:
- `weathers` — weather code → Czech label, mirrors `WEATHER_CODES` in `src/forecast.py`. Keep these in sync by hand if you add/change weather codes there.
- `visited` — the 5-point busyness scale (`velmi slabe/slabe/stredni/hodne/naval` = Dead/Slow/Normal/Busy/Slammed), shared by both the bot's prediction and the user's logged reality.
- `weather_prediction` — what the bot predicted, keyed on `(den, predikce_den)`: `den` is the Sat/Sun being forecast, `predikce_den` is the day the job ran (Thu or Fri) — this lets both runs insert a row for the same weekend day without colliding, and lets Friday-vs-Thursday drift be measured later.
- `user_input` — what actually happened, keyed unique on `den`. Upserted on busyness click (misclick recovery). `visited_id` is `NOT NULL`, so a note (`poznamka`/`sold_product`, via modal) can only ever `UPDATE` an existing row — the busyness button must be clicked first. `source` is `live` or `backfill`; backfilled labels are known-noisy and analysis must say so.
- `season_config` — singleton row (`id` fixed to 1 via `CHECK`), this year's `season_start`/`season_end`. Set/overwritten via the `season_setup` button → `season_modal` flow, never edited by hand. Every scheduled script's `is_in_season()` fails **open** (keeps running) when this table is empty, so nothing breaks before it's ever been configured.

## Cross-file coupling to preserve

These aren't enforced by types across the Python/TS boundary — keep them in sync by hand:
- Busyness button order/keys: `BUSYNESS` in `src/log_message.py` ↔ `BUSYNESS` in `worker/src/index.ts` ↔ seed order of `visited` in `db/schema.sql`. The Worker looks up `visited_id` by name (not hardcoded id), so seeding order isn't strictly load-bearing anymore, but the key strings (`dead`/`slow`/`normal`/`busy`/`slammed`) and Czech `visitedName` mapping must match.
- `custom_id` format: `log:<isoDate>:<key>` for busyness buttons, `note:<isoDate>` → modal `note_modal:<isoDate>`. The date is embedded directly in the id (not "sat"/"sun") so a stale message's buttons always resolve to the correct real date. Separately, `season_setup` → modal `season_modal` (fields `season_start`/`season_end`, free text `den.měsíc` e.g. `2.5.`) sets `season_config`; the Worker's `parseDayMonth()` re-prompts (ephemeral message) rather than guessing on anything that doesn't parse to a real calendar date.
- Weather code labels: `WEATHER_CODES` in `src/forecast.py` (WMO codes) collapse to the same Czech label set seeded into `weathers` in `db/schema.sql`.
- Message rendering: both DM messages use Discord Components V2 (`flags: 1 << 15`, no `content`/`embeds` alongside `components`) with a `Container` (type 17) + `Separator` (type 14, divider) between Saturday/Sunday blocks, so the two message types look visually consistent.

## Season boundary

The work season (outdoor gastro, roughly May–October) is shorter than the calendar year, but every cron in `.github/workflows/` fires year-round regardless — there's no season-awareness in GitHub's scheduler itself. Two pieces handle that:

- **`season_config`** (see Database above) gates the three recurring scripts. `src/season_reminder.py` fires once a year and DMs a button that opens a modal to set it — see the `custom_id` note above.
- **`.github/workflows/keepalive.yml`** makes an empty commit to `main` on the 1st of every month, year-round. This exists because GitHub auto-disables a repo's scheduled workflows after 60 days with no repository activity — and the work season itself (2.5.–4.10., ~5 months) is longer than that window, so without this, GitHub could disable the schedules **mid-season**, not just over winter. Whether a scheduled workflow's own executions count as "activity" for that rule is genuinely unclear from GitHub's docs, so this sidesteps the question with an unambiguous real git push instead of relying on an interpretation.

## Conventions worth knowing

- All day names are rendered in Czech (`CZECH_DAYS`), computed by hand rather than via locale-dependent `%A` — GitHub's runners don't reliably have a Czech locale.
- Timezone is always `Europe/Prague`, passed as `TZ_NAME` env var and used with `zoneinfo.ZoneInfo` in Python / manual UTC-date parsing in the Worker (`czechDay` parses as UTC midnight specifically to avoid TZ-shifting the weekday).
- The Worker's `CZECH_DAYS` array starts at Sunday (JS `getUTCDay()` convention: 0=Sunday); Python's starts at Monday (`date.weekday()` convention). Don't copy one array into the other without adjusting the offset.
- Secrets are never committed: GitHub Actions secrets for the Python side (`DISCORD_BOT_TOKEN`, `DISCORD_USER_ID`, `DATABASE_URL` — the forecast job needs the latter to write predictions), `wrangler secret put` for the Worker side (`DISCORD_PUBLIC_KEY`, `DISCORD_APPLICATION_ID`, `DATABASE_URL`) — not even `wrangler.toml [vars]`, by deliberate choice (see comment in `worker/wrangler.toml`).
