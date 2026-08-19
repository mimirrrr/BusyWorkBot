"""Phase 3: weekly completeness sweep — nag about unlogged past weekend days.

Runs from GitHub Actions (weekly) or locally:
    python src/completeness_sweep.py

Sends nothing when there's nothing missing. Unlike the other scripts,
--dry-run still needs a real DATABASE_URL (via .env) since there's no
meaningful preview without querying what's actually missing.
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import psycopg
import requests

DISCORD_API = "https://discord.com/api/v10"
IS_COMPONENTS_V2 = 1 << 15

# (custom_id key, button label, button style) — must match BUSYNESS in
# src/log_message.py and the `visited` seed order in db/schema.sql, since the
# Worker maps these keys to a visited_id by name lookup.
BUSYNESS = [
    ("dead", "Dead", 4),
    ("slow", "Slow", 2),
    ("normal", "Normal", 2),
    ("busy", "Busy", 1),
    ("slammed", "Slammed", 3),
]

CZECH_DAYS = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]

# Discord's 40-component cap counts recursively, not just top-level items:
# each shown day costs 9 (header text=1, busyness row=1 wrapper+5 buttons,
# note row=1 wrapper+1 button), plus a divider between every pair of shown
# days (N-1), plus the leading warning line and, when there's overflow, the
# "+N more" line: worst case 10*N + 1 <= 40, so N=3 is the most that's
# always safe with margin to spare (N=4 hits exactly 41 with overflow).
MAX_DAYS_PER_MESSAGE = 3


def czech_day(day: dt.date) -> str:
    return CZECH_DAYS[day.weekday()]


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader for local runs. Real secrets live in GitHub Actions."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def text(content: str) -> dict:
    return {"type": 10, "content": content}


def czech_day_count(n: int) -> str:
    """Czech noun declension for 'den' (day): 1 den, 2-4 dny, 0/5+ dní."""
    if n == 1:
        return f"{n} den"
    if 2 <= n <= 4:
        return f"{n} dny"
    return f"{n} dní"


def czech_more_days(n: int) -> str:
    """Same declension, for the '+N more' overflow line."""
    if n == 1:
        return f"+{n} další den"
    if 2 <= n <= 4:
        return f"+{n} další dny"
    return f"+{n} dalších dní"


def day_block(day: dt.date) -> list[dict]:
    """Header text + busyness row + note row for one day — identical layout
    to src/log_message.py's day_block, so the Worker's button handling needs
    no changes to also serve this message.
    """
    return [
        text(f"**{czech_day(day)} {day.strftime('%d.%m')}**"),
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": style,
                    "label": label,
                    "custom_id": f"log:{day.isoformat()}:{key}",
                }
                for key, label, style in BUSYNESS
            ],
        },
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 2,
                    "label": "ADD NOTE",
                    "custom_id": f"note:{day.isoformat()}",
                },
            ],
        },
    ]


def fetch_missing_days(database_url: str, today: dt.date) -> list[dt.date]:
    """Sat/Sun dates with no user_input row, from the season start through
    yesterday (falls back to MIN(den) in user_input if season_config has no
    row yet, matching is_in_season's fail-open behavior). Reaching back to
    season_start rather than just the first-ever logged day is deliberate:
    it's also how pre-live-tracking weekends get surfaced for backfill
    labeling — the Worker tags each click's source as live/backfill by
    comparing the date against its own cutoff constant.
    """
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH bounds AS (
                    SELECT COALESCE(
                        (SELECT season_start FROM season_config WHERE id = 1),
                        (SELECT MIN(den) FROM user_input)
                    ) AS start_day
                ),
                weekend_days AS (
                    SELECT d::date AS day
                    FROM bounds, generate_series(start_day, %(today)s::date, interval '1 day') AS d
                    WHERE EXTRACT(DOW FROM d) IN (0, 6)
                )
                SELECT wd.day FROM weekend_days wd
                LEFT JOIN user_input ui ON ui.den = wd.day
                WHERE ui.den IS NULL AND wd.day < %(today)s
                ORDER BY wd.day
                """,
                {"today": today},
            )
            return [row[0] for row in cur.fetchall()]


def is_in_season(database_url: str, today: dt.date) -> bool:
    """Fails OPEN: True (keep running) if season_config has no row yet, so
    nothing silently breaks before the season has ever been configured.
    """
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT season_start, season_end FROM season_config WHERE id = 1")
            row = cur.fetchone()
    if row is None:
        return True
    season_start, season_end = row
    return season_start <= today <= season_end


def discord_dm(token: str, user_id: str, components: list[dict]) -> None:
    headers = {"Authorization": f"Bot {token}"}
    r = requests.post(
        f"{DISCORD_API}/users/@me/channels",
        headers=headers, json={"recipient_id": user_id}, timeout=30,
    )
    r.raise_for_status()
    channel_id = r.json()["id"]
    r = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=headers,
        json={"flags": IS_COMPONENTS_V2, "components": components},
        timeout=30,
    )
    r.raise_for_status()


def main() -> None:
    load_dotenv()
    dry_run = "--dry-run" in sys.argv
    token = "" if dry_run else env("DISCORD_BOT_TOKEN")
    user_id = "" if dry_run else env("DISCORD_USER_ID")
    database_url = env("DATABASE_URL")
    tz = env("TZ_NAME", "Europe/Prague")

    today = dt.datetime.now(ZoneInfo(tz)).date()
    if not is_in_season(database_url, today):
        print("Completeness sweep: outside the configured season, skipping.")
        return

    missing = fetch_missing_days(database_url, today)

    if not missing:
        print("Completeness sweep: nothing missing.")
        return

    shown, overflow = missing[:MAX_DAYS_PER_MESSAGE], missing[MAX_DAYS_PER_MESSAGE:]

    components = [text(f"⚠️ **Chybí ti zápis za {czech_day_count(len(missing))}** — doplň prosím:")]
    for i, day in enumerate(shown):
        components.extend(day_block(day))
        if i < len(shown) - 1:
            components.append({"type": 14, "divider": True, "spacing": 1})
    if overflow:
        components.append(text(f"-# {czech_more_days(len(overflow))} chybí, doplní se v příštím sweepu"))

    if dry_run:
        import json
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps({"components": components}, indent=2, ensure_ascii=False))
        return
    discord_dm(token, user_id, components)
    print(f"Completeness nag sent for {len(missing)} missing day(s).")


if __name__ == "__main__":
    main()
