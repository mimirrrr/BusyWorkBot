# Shift-forecast-bot

My weekend income depends on the weather (outdoor gastro). This bot DMs me the
Sat/Sun forecast for my workplace every Thursday and Friday, will collect what
actually happened via one-tap Discord buttons, and at the end of the season
reports how well weather predicted my shifts.

Full roadmap: [docs/PLAN.md](docs/PLAN.md). **Current status: Phase 1** —
scheduled forecast DMs, no predictions or logging yet.

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

## Notes

- Actions cron is UTC; `0 15 * * THU,FRI` = 17:00 CEST. The season is
  April–September so DST always applies.
- Coordinates are hardcoded for Bělá 87, 747 23 Bělá: 49.97233 N, 18.14489 E.
- GitHub may skip scheduled runs on repos with no activity for 60+ days —
  a non-issue during the season, worth knowing in the off-season.
