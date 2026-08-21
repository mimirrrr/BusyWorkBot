# Shift-forecast-bot

My weekend income depends on the weather (outdoor gastro). This bot DMs me the
Sat/Sun forecast for my workplace every Thursday and Friday, will collect what
actually happened via one-tap Discord buttons, and at the end of the season
reports how well weather predicted my shifts.

Full roadmap: [docs/PLAN.md](docs/PLAN.md). **Current status: Phase 5** —
forecasts, logging, rule-engine predictions, the weekly completeness sweep,
historical backfill, and error-handling hardening are all live. The
end-of-season report (`src/season_report.py`) is implemented and runs
every Tuesday alongside the completeness sweep, but stays a no-op until the
season ends (4.10.2026) — see "End-of-season report" below.

## Weekly cadence

Everything below is Europe/Prague time; full details in
[CLAUDE.md](CLAUDE.md#weekly-cadence).

| When | What happens |
|---|---|
| Thu 17:00 | Early Sat/Sun forecast DM |
| Fri 17:00 | Official forecast DM, + last-weekend recap |
| Mon 08:00 | Busyness-logging buttons DM |
| Tue 21:00 | Backfills actual weather (silent) + nags for unlogged days |
| ~Apr 20 (yearly) | Season-window setup prompt |
| Monthly | Keepalive commit (stops GitHub disabling the cron jobs) |

## End-of-season report

Once the season ends (4.10.2026), `src/season_report.py` — running as a
second step in the same Tuesday `completeness-sweep.yml` job — takes over:
it nags for any still-missing days first, and once the dataset is complete
it builds a self-contained HTML report (accuracy vs. reality, a confusion
matrix, live-vs-backfill accuracy, a weather-forecast-error breakdown, and
a short AI-written summary from Google Gemini that only narrates the
precomputed numbers, never calculates anything), DMs it to Discord as a
file attachment, and commits it to [reports/season-2026.html](reports/season-2026.html).

## How it works (phase 1)

- GitHub Actions cron fires Thu + Fri 17:00 Prague time
- `src/forecast.py` pulls the weekend forecast for Bělá (Opava) from
  [Open-Meteo](https://open-meteo.com/) — free, no API key
- Summarizes working hours (9:00–20:00) per day and sends it to my
  Discord DMs via the bot

No servers. The script runs for a few seconds and exits.

## Setup

1. Discord Developer Portal → New Application → Bot. Public Bot **off**,
   all privileged intents **off**, Installation: Guild Install only,
   Install Link = None. Copy the bot token.
2. Create an empty private Discord server and invite the bot with zero
   permissions (required so the bot may DM you — Discord rule):
   `https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=0`
3. Enable Developer Mode in Discord (Settings → Advanced), right-click
   yourself → Copy User ID.
4. GitHub repo → Settings → Secrets and variables → Actions → add
   `DISCORD_BOT_TOKEN` and `DISCORD_USER_ID`.
5. Test: Actions tab → weekend-forecast → **Run workflow**. A DM should arrive.

## Local testing

```
pip install -r requirements.txt
python src/forecast.py --dry-run     # prints the message, no Discord needed
```

For a real local send, copy `.env.example` to `.env` and fill in the token
and user ID (`.env` is gitignored).

## Tests

Pure-function unit tests (rule engine, date math, formatting — no DB or
Discord calls needed):

```
pip install -r requirements-dev.txt
pytest -v
```

## Notes

- Actions cron is UTC; `0 15 * * THU,FRI` = 17:00 CEST. The season runs
  2.5.–4.10. so DST always applies.
- Coordinates are hardcoded for Bělá 87, 747 23 Bělá: 49.97233 N, 18.14489 E.
- GitHub auto-disables scheduled workflows after 60+ days with no repository
  activity — and the season itself is longer than that, so this would've bitten
  mid-season too, not just over winter. `.github/workflows/keepalive.yml` makes
  a trivial monthly empty commit specifically to prevent that.
