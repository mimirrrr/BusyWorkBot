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

# Discord Components V2 messages cap at 40 total components; each missing day
# costs 3 (header + busyness row + note row), so 10 days stays safely under
# that with room to spare.
MAX_DAYS_PER_MESSAGE = 10


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
    """Sat/Sun dates with no user_input row, from the first-ever logged day
    through yesterday. Bootstraps the start of the range off MIN(den) in
    user_input instead of a hardcoded season-start date to keep in sync —
    if nothing has ever been logged yet, this naturally returns nothing.
    """
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH bounds AS (
                    SELECT MIN(den) AS start_day FROM user_input
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
    missing = fetch_missing_days(database_url, today)

    if not missing:
        print("Completeness sweep: nothing missing.")
        return

    shown, overflow = missing[:MAX_DAYS_PER_MESSAGE], missing[MAX_DAYS_PER_MESSAGE:]

    components = [text(f"⚠️ **Chybí ti zápis za {len(missing)} den(dní)** — doplň prosím:")]
    for i, day in enumerate(shown):
        components.extend(day_block(day))
        if i < len(shown) - 1:
            components.append({"type": 14, "divider": True, "spacing": 1})
    if overflow:
        components.append(text(f"-# +{len(overflow)} dalších chybí, doplní se v příštím sweepu"))

    if dry_run:
        import json
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps({"components": components}, indent=2, ensure_ascii=False))
        return
    discord_dm(token, user_id, components)
    print(f"Completeness nag sent for {len(missing)} missing day(s).")


if __name__ == "__main__":
    main()
