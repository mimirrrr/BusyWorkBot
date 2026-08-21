"""Phase 2: Monday logging message — one-tap buttons to record how busy last
weekend actually was.

Runs from GitHub Actions (Monday 08:00 Prague time) or locally:
    python src/log_message.py
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

from retry import connect_with_retry, request_with_retry

DISCORD_API = "https://discord.com/api/v10"

# Message flag that switches the payload to Components V2, where text blocks
# and button rows can be freely interleaved (day header directly above its
# own buttons) instead of the classic single content string + trailing rows.
IS_COMPONENTS_V2 = 1 << 15

# (custom_id key, button label, button style) in busyness order — must match
# the order `visited` was seeded in db/schema.sql (velmi slabe..naval), since
# the Worker maps this position to a visited_id.
# Styles: 4 Danger, 2 Secondary, 1 Primary, 3 Success.
BUSYNESS = [
    ("dead", "Dead", 4),
    ("slow", "Slow", 2),
    ("normal", "Normal", 2),
    ("busy", "Busy", 1),
    ("slammed", "Slammed", 3),
]

CZECH_DAYS = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]


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


def last_weekend(today: dt.date) -> tuple[dt.date, dt.date]:
    """Most recently completed Saturday and Sunday (the weekend that just ended)."""
    days_since_sunday = (today.weekday() - 6) % 7
    sunday = today - dt.timedelta(days=days_since_sunday)
    saturday = sunday - dt.timedelta(days=1)
    return saturday, sunday


def text(content: str) -> dict:
    return {"type": 10, "content": content}


def day_block(day: dt.date) -> list[dict]:
    """Header text + busyness row + note row for one day.

    custom_id carries the actual date (not "sat"/"sun") so the Worker never
    has to guess which weekend a stale message button belongs to.
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


def is_in_season(database_url: str, today: dt.date) -> bool:
    """Fails OPEN: True (keep running) if season_config has no row yet, so
    nothing silently breaks before the season has ever been configured.
    """
    with connect_with_retry(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT season_start, season_end FROM season_config WHERE id = 1")
            row = cur.fetchone()
    if row is None:
        return True
    season_start, season_end = row
    return season_start <= today <= season_end


def discord_dm(token: str, user_id: str, components: list[dict]) -> None:
    headers = {"Authorization": f"Bot {token}"}
    # 1. open (or reuse) the DM channel with me
    r = request_with_retry(
        "POST", f"{DISCORD_API}/users/@me/channels",
        headers=headers, json={"recipient_id": user_id}, timeout=30,
    )
    channel_id = r.json()["id"]
    # 2. send the message as Components V2 (no "content"/"embeds" alongside it)
    request_with_retry(
        "POST", f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=headers,
        json={"flags": IS_COMPONENTS_V2, "components": components},
        timeout=30,
    )


def main() -> None:
    load_dotenv()
    dry_run = "--dry-run" in sys.argv
    token = "" if dry_run else env("DISCORD_BOT_TOKEN")
    user_id = "" if dry_run else env("DISCORD_USER_ID")
    # Optional in --dry-run (only used if a local .env happens to have it).
    database_url = os.environ.get("DATABASE_URL") if dry_run else env("DATABASE_URL")
    tz = env("TZ_NAME", "Europe/Prague")

    today = dt.datetime.now(ZoneInfo(tz)).date()
    if not dry_run and not is_in_season(database_url, today):
        print("Logging message: outside the configured season, skipping.")
        return

    saturday, sunday = last_weekend(today)

    components = [
        text(f"📝 **Jak bylo o víkendu?** ({saturday.strftime('%d.%m.')}–{sunday.strftime('%d.%m.')})"),
        *day_block(saturday),
        {"type": 14, "divider": True, "spacing": 2},
        *day_block(sunday),
    ]

    if dry_run:
        import json
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps({"components": components}, indent=2, ensure_ascii=False))
        return
    discord_dm(token, user_id, components)
    print("Logging message sent.")


if __name__ == "__main__":
    main()
