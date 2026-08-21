# Shift Forecast Bot — Project Plan

A Discord bot that predicts how busy my weekend outdoor gastro shifts will be based on weather, collects my real-world feedback with one-tap buttons, and validates its predictions against reality over the season.

**One-liner for resume:** My income depends on weather, so I built a system that forecasts my shifts, logs actual outcomes, and reports its own prediction accuracy.

---

## Context

- I work weekends (Sat + Sun) in outdoor gastro. Business volume is heavily weather-dependent.
- Season runs 2.5.2026 (Sat) – 4.10.2026 (Sun). Live bot tracking started 1.8.2026, mid-season — everything from 2.5. through 31.7. (13 weekends, 26 days) needs backfilling from memory; live data covers the rest.
- Primary goals: (1) actually useful to me, (2) portfolio project with honest, verifiable metrics.

## Core principle

**Code computes, AI narrates.** All statistics, accuracy numbers, and graphs come from deterministic code (matplotlib — no pandas, see Component 7). An LLM (Google Gemini) only ever receives pre-computed results and writes the narrative summary. It never calculates anything. This is a deliberate architectural decision and belongs in the writeup.

---

## Architecture

```
GitHub Actions (cron)                    Cloudflare Worker (serverless)
├─ Thu+Fri: forecast messages ─► Discord ◄── button/modal interactions
├─ Monday: logging message         DM          │ verify signature
├─ Tuesday: completeness sweep                 ▼
│    + season report check               Postgres (Neon free tier)
│    (season_report.py, same job,              │
│     once the season is over — Component 7)   │
└─ ~April 20 (yearly): season prompt     End-of-season report pipeline
                                          (matplotlib → Gemini narrative)
```
(Plus a monthly no-op keepalive commit, unrelated to the message flow above —
see Component 8.)

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
   - Post prediction via DM: per-day verdict + key weather numbers + confidence.
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
   - Season-setup button opens a modal (two free-text `den.měsíc` fields); on submit, parses both dates and upserts the singleton `season_config` row. Re-prompts (ephemeral message) instead of guessing on anything that doesn't parse to a real calendar date.
   - Responds with a message edit (`UPDATE_MESSAGE`) in all these cases — no separate ephemeral confirmations, except the re-prompt above and the invalid-`sold_product` case.

4. **Database** (Neon Postgres free tier) — schema in `db/schema.sql`
   - `weathers` (číselník): weather code label, from `WEATHER_CODES` in `src/forecast.py`
   - `visited` (číselník): busyness scale (velmi slabe/slabe/stredni/hodne/naval = Dead/Slow/Normal/Busy/Slammed) — shared by both the bot's predicted verdict and my logged reality, same 5-level scale either way
   - `weather_prediction`: what the bot predicted — `den` (Sat/Sun forecast) + `predikce_den` (Thu or Fri, the day the job ran) as a pair, weather numbers, `predikce_navstevnost_id` (rule engine verdict), `created_at`
   - `user_input`: what actually happened — `den` (unique, upsert on re-click), `visited_id`, `sold_product` (units sold, via modal), `poznamka` (free-text note, via modal), `source` (live | backfill), `logged_at`
   - `season_config`: singleton row (`id` fixed to 1) holding this year's `season_start`/`season_end` — see Component 8. Set via Discord, never edited by hand.
   - `weather_actual`: post-hoc actual weather from Open-Meteo's archive API — `den` (unique), weather numbers, `created_at`. No `chance_rain`/verdict columns: rain probability is a forecast-uncertainty concept that doesn't apply to something that already happened, and a verdict belongs to a prediction, not the weather itself — `predict_verdict()` runs against these numbers at analysis time (phase 5) instead of being stored here. Filled in automatically by the completeness sweep (Component 5), so end-of-season analysis can separate "bad forecast" from "bad rule."

5. **Completeness sweep** (GitHub Actions cron, Tuesday ~21:00 local — after Monday's logging message has had its shot). Two independent jobs in one script:
   - **Actual-weather backfill** (`fill_missing_actual_weather`): query Sat/Sun days since `season_start` that are old enough Open-Meteo's archive dataset should be published (`ARCHIVE_LAG_DAYS = 5`, since the archive is reanalysis data with a publication lag, not live) but have no `weather_actual` row → pull them from the archive API (one ranged call) and store. Silent — no Discord message, this is data the script fills in on its own. Wrapped so a flaky Open-Meteo call can't block the nag below. Naturally covers both the pre-tracking backfill weekends and every live weekend going forward, since it's not limited to the 2.5.–31.7. range.
   - **Missing user_input nag**: query Sat/Sun days since `season_config.season_start` (falls back to the first-ever logged day if `season_config` is empty) with no `user_input` row → nag DM listing them, reusing the Monday message's exact button/custom_id scheme so the Worker needs no changes to *handle* it.
   - Reaching back to season start rather than just the first-ever logged day is deliberate: it's also the backfill-labeling flow (see Component 6) — the same message, same buttons, spread automatically across however many Tuesdays it takes. The Worker distinguishes the two by tagging each click's `source` as `live`/`backfill`, comparing the date against a fixed cutoff (2026-08-01, when live tracking began) — the one thing that *does* change in the Worker. Sends nothing when nothing's missing.
   - Caps at 3 days per message — Discord's 40-component cap counts recursively (each day's 5 busyness buttons + 1 note button all count, not just the visible rows), so the naive "10 days × 3 components" estimate was wrong; 3 days keeps every message safely under the cap even with the overflow line. "+N more" catches up on the next weekly run.
   - End of season: final sweep until dataset is 100% labeled.

6. **Backfill**
   - *Labeling* (busyness from memory for 2.5.–31.7.2026, 13 weekends/26 days): handled entirely by Component 5's user_input nag — no separate script. `source = backfill` is set automatically by the Worker's date cutoff. These labels are noisy and the analysis/writeup must say so. Distinguishing clean vs. noisy data is part of the point.
   - *Historical weather* for those same dates: also handled by Component 5, via the actual-weather backfill job — no separate script. There's no `weather_prediction` row for these pre-tracking weekends (the bot wasn't running), so `weather_actual` is the only data phase 5 can score backfill busyness against — via a retroactive `predict_verdict()` call at analysis time, not a stored prediction.

7. **End-of-season report** — `src/season_report.py`, run as a *second step* within the same `completeness-sweep.yml` workflow, right after `completeness_sweep.py`, on the same Tuesday cron. **No separate workflow/cron.** It does **not** use `is_in_season()` (unlike every other recurring script) — its job only makes sense once the season is *over*, at which point `completeness_sweep.py`'s own `is_in_season()` gate has already stopped that script from nagging/backfilling, so `season_report.py` has to cover that post-season tail itself:
   - `main()` flow: `load_dotenv()`; parse `--dry-run`, `--as-of YYYY-MM-DD` (overrides "today", orthogonal to `--dry-run`, exists so the whole pipeline — including the season-over gate — can be exercised before the real season ends). Read `season_config` (`season_start`, `season_end`, `report_sent_at`, `updated_at`): no row → exit (nothing to report against, fails **closed** here unlike `is_in_season`'s fail-open elsewhere). `today <= season_end` → exit (the normal no-op path on every in-season run). `report_sent_at IS NOT NULL AND report_sent_at >= updated_at` → exit ("already sent" — see idempotency below).
   - **Completeness check** (cross-module import from `completeness_sweep.py` — same pattern that module already uses on `forecast.py` — reusing `fill_missing_actual_weather`, `fetch_missing_days`, `day_block`, `text`, `czech_day_count`, `czech_more_days`, `MAX_DAYS_PER_MESSAGE`, `discord_dm`, `ARCHIVE_LAG_DAYS` as-is, no signature changes): call `fill_missing_actual_weather(..., today=min(real_today, season_end + timedelta(days=ARCHIVE_LAG_DAYS)), ...)` and `fetch_missing_days(database_url, today=season_end + timedelta(days=1))` — bounding both by `season_end`, **not** real "today", so the check can never drift into flagging *next* season's weekends once `season_config` gets overwritten (~April) if this row hasn't been re-checked since. If anything's missing, build and send the same nag layout as the regular Tuesday sweep and **return without generating a report this run**.
   - **Report generation**, once nothing's missing:
     - `fetch_season_dataset(database_url, season_start, season_end) -> list[dict]` in `src/season_report.py` — one query joining `weather_prediction` (latest per day, Friday winning over Thursday), `weather_actual`, `user_input`, and the `visited`/`weathers` labels, for every Sat/Sun in `[season_start, season_end]`:
       ```sql
       WITH season AS (
           SELECT %(season_start)s::date AS season_start, %(season_end)s::date AS season_end
       ),
       weekend_days AS (
           SELECT d::date AS den
           FROM season, generate_series(season_start, season_end, interval '1 day') AS d
           WHERE EXTRACT(DOW FROM d) IN (0, 6)
       ),
       latest_prediction AS (
           SELECT DISTINCT ON (den) den, predikce_den, pocasi_id, chance_rain, srazky,
                  teplota_min, teplota_max, wind_speed, predikce_navstevnost_id
           FROM weather_prediction
           ORDER BY den, predikce_den DESC   -- Friday's row wins over Thursday's
       )
       SELECT
           wd.den,
           ui.visited_id AS actual_visited_id, av.name_v AS actual_visited_name,
           ui.source, ui.sold_product, ui.poznamka,
           wa.pocasi_id AS actual_pocasi_id, aw.name_w AS actual_weather_label,
           wa.srazky AS actual_srazky, wa.teplota_min AS actual_teplota_min,
           wa.teplota_max AS actual_teplota_max, wa.wind_speed AS actual_wind_speed,
           lp.predikce_den,
           lp.pocasi_id AS pred_pocasi_id, pw.name_w AS pred_weather_label,
           lp.chance_rain AS pred_chance_rain, lp.srazky AS pred_srazky,
           lp.teplota_min AS pred_teplota_min, lp.teplota_max AS pred_teplota_max,
           lp.wind_speed AS pred_wind_speed, pv.name_v AS pred_visited_name
       FROM weekend_days wd
       LEFT JOIN user_input ui        ON ui.den = wd.den
       LEFT JOIN visited av            ON av.id_v = ui.visited_id
       LEFT JOIN weather_actual wa     ON wa.den = wd.den
       LEFT JOIN weathers aw           ON aw.id_w = wa.pocasi_id
       LEFT JOIN latest_prediction lp  ON lp.den = wd.den
       LEFT JOIN weathers pw           ON pw.id_w = lp.pocasi_id
       LEFT JOIN visited pv            ON pv.id_v = lp.predikce_navstevnost_id
       ORDER BY wd.den;
       ```
       Fetched via plain `cur.description`-zip into dicts, same as every other query in the codebase (no `psycopg.rows.dict_row`). Live weekends (Aug 2026+) get a real `pred_*` row; backfill weekends (2.5.–31.7.2026, no `weather_prediction` row since the bot wasn't running yet) get NULLs there.
     - `src/report_stats.py` — **pure functions only, no DB/network/matplotlib**, unit-tested in `tests/test_report_stats.py`:
       - `estimate_rain_prob_from_mm(srazky_mm: float) -> float` — piecewise-linear proxy (`0mm→0%`, `1mm→20%`, `5mm→50%`, `20mm+→100%` capped). Docstring must say explicitly this is **not a real probability**, only a retroactive-only stand-in for backfill weekends with no stored forecast.
       - `retroactive_predicted_verdict(srazky_mm: float, teplota_max: float) -> str` — `forecast.predict_verdict(estimate_rain_prob_from_mm(srazky_mm), teplota_max)`. This is how backfill weekends get scored: "what would the rule engine have said, had it seen this actual weather as a forecast?"
       - `rain_tier_only_verdict(max_rain_prob: float) -> str` — `forecast.TIERS` indexed by rain thresholds alone, no temp demotion, isolating the rain rule's standalone accuracy for the "per rule" breakdown.
       - `to_verdict_key(visited_name: str | None) -> str | None` — `forecast.REVERSE_VISITED_NAMES` lookup, `None` passes through.
       - `enrich_row(row: dict) -> dict` — one `fetch_season_dataset()` row → adds `predicted_verdict`, `prediction_kind` (`"forecast" | "retroactive" | None`), `actual_verdict`, `correct` (bool|None), `is_backfill` (bool), `complete` (bool). `enrich_dataset(rows: list[dict]) -> list[dict]` maps it over everything.
       - `overall_accuracy(rows: list[dict]) -> dict` → `{"n","correct","accuracy"}`; `accuracy` is `None` at `n==0` (never divide by zero — needed since August testing runs against a partial season).
       - `accuracy_by_split(rows: list[dict], key: str) -> dict[str, dict]` — groups by `is_backfill` or `prediction_kind`, applies `overall_accuracy` per group.
       - `accuracy_by_rule_component(rows: list[dict]) -> dict` → `{"rain_tier_only": {...}, "full_rule": {...}, "temp_demotion_triggered_n", "temp_demotion_helped_n", "temp_demotion_hurt_n"}`.
       - `confusion_matrix(rows: list[dict]) -> dict` → `{"labels": forecast.TIERS, "matrix": 5x5 int grid}`, built only from rows where `complete == True`.
       - `weather_forecast_error(rows: list[dict]) -> dict` — `rain_mm_mae`, `temp_max_mae` over `prediction_kind == "forecast"` rows with actual weather present, plus `{"bad_forecast","bad_rule","both","neither"}` bucket counts disentangling *why* a live-weekend prediction was wrong — this is the "forecast-vs-actual-weather error contribution" chart's data source.
       - `scatter_points(rows: list[dict], x_key: str, y_key: str) -> list[dict]` → `[{"x","y","verdict","is_backfill"}, ...]`.
       - `season_summary_stats(rows: list[dict]) -> dict` — assembles everything above into one JSON-serializable blob; the **only** object handed to the charts/narrative/HTML modules below.
       - Test coverage (`tests/test_report_stats.py`, flat `pytest` style matching `tests/test_pure_functions.py`): anchor-point tests for `estimate_rain_prob_from_mm`; tier-boundary tests for `rain_tier_only_verdict`; `enrich_row` on synthetic rows covering complete / missing-actual / missing-prediction / backfill; `overall_accuracy` at n=0 and n>0; `confusion_matrix` shape+counts on a small fixture; `accuracy_by_rule_component` on a case where the temp demotion helps vs. hurts.
     - `src/report_charts.py` — matplotlib (`matplotlib.use("Agg")` before importing `pyplot`, required for headless CI): `render_accuracy_bar(stats)`, `render_confusion_matrix(stats)`, `render_scatter(points, title, xlabel, ylabel)`, `render_live_vs_backfill(stats)` — each returns a base64-encoded PNG string (`_fig_to_base64`: `savefig` to `BytesIO`, base64-encode, `plt.close(fig)`) for direct `<img src="data:image/png;base64,...">` embedding. No temp files, no separate image assets — keeps the report a single file.
     - `src/report_narrative.py` — `build_prompt(stats: dict) -> str` (embeds the stats dict as JSON; explicit instruction not to invent or recompute any number, only narrate what's given) and `call_gemini(api_key: str, model: str, prompt: str) -> str`, calling Google Gemini directly over HTTPS via the existing `retry.request_with_retry` helper (no new SDK dependency — matches the plain-`requests` style already used for Discord/Open-Meteo). Call shape: `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`, header `X-goog-api-key: {api_key}`, body `{"contents":[{"parts":[{"text": prompt}]}], "generationConfig":{"temperature":0.3}}`, response text at `candidates[0].content.parts[0].text`, `timeout=60`. **Confirm the exact current Gemini flash model id/endpoint at implementation time** — parameterized via a `GEMINI_MODEL` env var rather than hardcoded, since a training-data-era model string shouldn't be trusted. Wrapped in a broad try/except — any failure (bad key, deprecated model, network) returns a canned fallback string instead of blocking the whole report.
     - `src/report_html.py` — `render_html(stats, charts, narratives, season_start, season_end, generated_at) -> str`. Plain f-string/template assembly, no Jinja2 (avoids a new dependency for a single-consumer template). **AI-written narrative text renders inside a visually distinct `.ai-box` div — different background color, an explicit "AI-generated summary" label — never blended in with the program-computed numbers/charts.**
   - **Delivery**: writes `reports/season-<year>.html` to disk (always, even in `--dry-run`, so a dry run can be opened and inspected locally). If not `--dry-run`: DMs it to Discord as a file attachment via `discord_dm_with_file(token, user_id, components, filename, file_bytes, content_type)` — a new multipart-upload variant of `discord_dm`, since every existing `discord_dm` call is JSON-only Components V2 with no attachment support (`payload_json` part with components/flags + `files={"files[0]": (filename, file_bytes, content_type)}`, sent via `request_with_retry(..., files=...)`, which already passes `**kwargs` through to `requests.request`); then `UPDATE season_config SET report_sent_at = now() WHERE id = 1`. Separately, the same generated file gets **committed to the repo** — a workflow step (`git add reports/ && git commit && git push`, mirroring Component 8's keepalive commit), not something the Python script does itself.
   - **Idempotency**: nullable `season_config.report_sent_at TIMESTAMPTZ` column, checked as `report_sent_at IS NOT NULL AND report_sent_at >= updated_at` — not a bare null-check. The `>=` matters: `worker/src/index.ts`'s `season_modal` handler already bumps `updated_at = now()` on every yearly overwrite of the singleton row, so this comparison self-invalidates once next year's season gets configured, with zero changes needed in `worker/`.
   - **Known limitation**: `season_config` is a singleton — if the report somehow still hasn't sent by the time `season_reminder.py`'s yearly flow overwrites it next April, the row needed to generate it correctly is gone. There's a ~6.5 month runway (Oct→April) for the weekly cron to catch up, which should be enough; not worth adding a DB read to `season_reminder.py` (deliberately DB-free today) to guard against it.
   - **Discord attachment + Components V2 combined in one message is new** in this codebase — needs a real smoke test (small dummy file) before trusting it for the actual report send.

8. **Season boundary + keepalive** (GitHub Actions cron)
   - The work season is shorter than the calendar year, but every cron fires year-round regardless — GitHub's scheduler has no concept of "season." `season_config` gates the other jobs: forecast/logging/sweep all check it right after computing today's date and quietly return (no Discord call, no DB write) when outside `[season_start, season_end]`. Fails **open** (keeps running) if the table's never been configured, so nothing breaks before the first setup.
   - Once a year (~April 20), `src/season_reminder.py` DMs a button that opens a modal (two `den.měsíc` fields) to set that window — see Component 3.
   - Separately, `.github/workflows/keepalive.yml` makes a trivial empty commit to `main` on the 1st of every month, year-round. GitHub auto-disables a repo's scheduled workflows after 60 days with no repository activity, and the season itself (2.5.–4.10., ~5 months) is longer than that — without this, the schedules could get disabled **mid-season**, not just over winter. Whether a scheduled workflow's own runs count as "activity" for that rule is unclear from GitHub's docs, so this sidesteps the ambiguity with an unambiguous real push instead.

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
- [x] Historical weather backfill 2.5.–31.7.2026 (13 weekends, 26 days) — folded into the completeness sweep (Component 5): `weather_actual` table + `fill_missing_actual_weather`, respects the archive API's ~5-day publication lag, also keeps capturing actual weather for the live season going forward (not just the backfill window)
- [x] Memory-labeling flow for past weekends (marked as backfill) — folded into the completeness sweep (Component 5): it now looks back to `season_config.season_start`, and the Worker source-tags each click by date cutoff
- [x] Error handling / hardening — broken down:
  - [x] Offset all four workflow cron schedules a few minutes off `:00` (`forecast.yml`, `monday-log.yml`, `completeness-sweep.yml`, `season-reminder.yml`) — GitHub's own docs name the top of every hour as the highest-delay-risk slot for scheduled runs; this is their documented mitigation, not a full fix (GitHub gives no upper bound on delay and admits a run can be skipped outright under high load).
  - [x] Guard `forecast.py`'s Thu/Fri branch against a severely delayed run landing on the wrong calendar day — `forecast.yml` now fires two separate `THU`/`FRI` schedule entries (not one combined `THU,FRI` cron) and passes `github.event.schedule` through as `SCHEDULE_CRON`; `is_official_run()`/`scheduled_weekday()` trust that over `today.weekday()`, so a delayed Friday run rolling past midnight into Saturday still gets the recap + correct `predikce_den` labeling instead of silently falling back to Thursday-style behavior. Logs a warning when the two disagree. Falls back to `today.weekday()` when there's no schedule (`workflow_dispatch`/local/`--dry-run`). Covered by pure-function tests in `tests/test_pure_functions.py`.
  - [x] Shared retry-with-backoff helper for transient HTTP failures (timeout/429/5xx only, not 4xx) — `src/retry.py` (`request_with_retry`, `connect_with_retry`), wired into every `requests.get`/`requests.post` and `psycopg.connect` call across `forecast.py`, `log_message.py`, `completeness_sweep.py`, `season_reminder.py`. Covered by mocked tests in `tests/test_retry.py`.
  - [x] Light retry on `psycopg.connect(..., connect_timeout=10)` calls (four call sites) for transient Neon cold-start/connection blips — same `connect_with_retry` helper as above.
  - [x] Season-modal date sanity check (Worker `season_modal` handler, `worker/src/index.ts`) — `inMonthDayRange()` rejects a parsed `season_start`/`season_end` outside `SEASON_START_RANGE`/`SEASON_END_RANGE` (Apr 15–May 15 / Sep 15–Oct 15) or a start on/after end, re-prompting the same way a malformed date already does. Not a DST fix (the season formula itself never crosses DST by construction) — just insurance against a fat-fingered date like `31.10.` slipping through the free-text modal.
- **Done when:** dataset covers the whole season to date and jobs survive a failed API call.

**Tests done:**
- [x] Pure-function unit tests (`tests/test_pure_functions.py`) — rule engine, date math, formatting, cross-file consistency. No DB/Discord needed.
- [x] `pytest` wired into GitHub Actions (`.github/workflows/tests.yml`) — runs on every push/PR to `main`

**Test ideas for later:**
- Worker pure-function tests (`parseDayMonth`, `confirmDay`/`confirmNote`/`confirmSeason`, `verifyDiscordRequest`) — needs a JS/TS test runner (Vitest)
- DB-integration tests for `is_in_season`, `store_predictions`, `fetch_missing_days`, `fetch_last_weekend_comparison` — needs a throwaway test DB

### Phase 5 — end-of-season report (early October)
Full spec (function signatures, SQL, workflow diff) in Component 7 above — this checklist is the implementation order, not a re-explanation.
- [x] `db/schema.sql`: add `report_sent_at TIMESTAMPTZ` (nullable) to `season_config` + manually `ALTER TABLE season_config ADD COLUMN report_sent_at TIMESTAMPTZ;` against the live Neon instance (schema.sql has no migration tool — hand-applied, per existing convention)
- [x] `requirements.txt`: add `matplotlib>=3.8` only — **no pandas** (season dataset is ~50 rows, plain dicts/lists match every other module's style; this is a deliberate deviation from this doc's earlier "pandas/matplotlib" wording) and **no Gemini SDK** (raw HTTPS via the existing `retry.py`, matching the plain-`requests` style already used for Discord/Open-Meteo)
- [x] `src/report_stats.py` — pure stats module (see Component 7 for exact function signatures: `estimate_rain_prob_from_mm`, `retroactive_predicted_verdict`, `rain_tier_only_verdict`, `to_verdict_key`, `enrich_row`/`enrich_dataset`, `overall_accuracy`, `accuracy_by_split`, `accuracy_by_rule_component`, `confusion_matrix`, `weather_forecast_error`, `scatter_points`, `season_summary_stats`)
- [x] `tests/test_report_stats.py` — flat pytest style matching `tests/test_pure_functions.py` (see Component 7 for exact coverage list); run `pytest -v` to confirm green before moving on — 15 tests, all green alongside the existing 30
- [x] `src/report_charts.py` — matplotlib, `Agg` backend, base64-PNG-returning functions (see Component 7)
- [x] `src/report_narrative.py` — Gemini call (see Component 7 for exact request/response shape); confirmed current model id via web search at implementation time (August 2026): `gemini-3.7-flash`, set as `DEFAULT_MODEL`, still overridable via `GEMINI_MODEL`
- [x] `src/report_html.py` — self-contained HTML assembly, `.ai-box` styling for AI text (see Component 7)
- [x] `src/season_report.py` — orchestrator: `fetch_season_dataset`, `discord_dm_with_file`, `main()` flow exactly as described in Component 7 (season/idempotency gates → completeness check/nag → report generation → delivery)
- [x] `.github/workflows/completeness-sweep.yml` (modified existing file, not a new one): added `permissions: contents: write` at workflow level; added `GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}` to the env block; added a second `run` step (`python src/season_report.py`, no flags on the real scheduled run — it self-gates internally) right after the existing `python src/completeness_sweep.py` step; added `workflow_dispatch` inputs `dry_run` (bool, default `true`) and `as_of` (string), passed as extra args to the `season_report.py` step only (the `completeness_sweep.py` step is untouched); added a third step that commits `reports/` back to `main` if anything changed, skipped when `dry_run == 'true'`, mirroring `keepalive.yml`'s git-identity pattern
- [x] `README.md`: bumped status line to Phase 5, added an "End-of-season report" section linking `reports/season-2026.html`
- [x] `CLAUDE.md`: updated — "PLANNED, not yet implemented" language removed now the code has landed
- [ ] Public writeup: architecture, honest accuracy numbers, what failed, what I'd measure differently next season — deferred until real season data exists to write about
- **Verification before trusting the real October cron:** [x] `pytest -v` green (45 tests) → [x] `python src/season_report.py --dry-run --as-of 2026-10-10` locally against real `DATABASE_URL` — confirmed the season gate and completeness check correctly detect the still-mostly-unlabeled 2026 season and print the nag payload without touching Discord → [x] full report-generation path (stats → charts → narrative fallback → HTML) smoke-tested end-to-end with synthetic data (real `DATABASE_URL` has too many missing days to reach this path organically yet) and the rendered HTML opened/inspected for chart embedding + `.ai-box` styling → [ ] `workflow_dispatch` on `completeness-sweep.yml` with `dry_run=true, as_of=2026-10-10` to confirm the CI environment (headless matplotlib, secrets wiring) → [ ] a second `workflow_dispatch` with `dry_run=false` on a safe test `as_of` date to prove the real Discord multipart send + git-commit-back step — **the last two need a real GitHub Actions run and are still outstanding.**
- **Done when:** report is published (committed to `reports/season-<year>.html`) and linked from the repo README + resume.

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
| Analysis | Python, matplotlib | deterministic, reproducible (no pandas — season dataset is ~50 rows, plain dicts/lists suffice) |
| Narrative | Google Gemini (flash tier) | summarizes computed stats only, never calculates |

## Secrets / config

`DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_USER_ID` (bot DMs me directly, no channel), `DATABASE_URL`, `GEMINI_API_KEY` (+ `GEMINI_MODEL` to pin the flash model id — see Component 7), workplace `LAT`/`LON`, timezone `Europe/Prague`. Stored in GitHub Actions secrets + Worker secrets. Never in the repo.

Workplace: Bělá 87, 747 23 Bělá (Opava district, CZ). Resolve exact `LAT`/`LON` once via Open-Meteo's geocoding API (https://geocoding-api.open-meteo.com/v1/search?name=B%C4%9Bl%C3%A1) or maps, hardcode as config (approx. 49.9 N, 18.1 E — verify).

## Open questions

- Do work hours vary by day/month? Rules need opening-hours windows to evaluate weather against.
- Tips/earnings as a second logged metric? More signal, slightly more friction — decide in Phase 2.
- Next season: replace hand rules with a simple model trained on season 1? (Nice v2 story, out of scope now.)
- v2 idea (after v1 is fully working): factor in nearby food festivals/events as a busyness signal alongside weather — some way to check for local events near the workplace on a given weekend and account for them in the prediction. Needs a data source for local events; deferred until the weather-only pipeline is proven.
