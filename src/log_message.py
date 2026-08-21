"""Phase 2: Monday logging message — one-tap buttons to record how busy last
weekend actually was.

Runs from GitHub Actions (Monday 08:00 Prague time) or locally:
    python src/log_message.py
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

from common import czech_day, load_dotenv, env, text, is_in_season, discord_dm

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


def last_weekend(today: dt.date) -> tuple[dt.date, dt.date]:
    """Most recently completed Saturday and Sunday (the weekend that just ended)."""
    days_since_sunday = (today.weekday() - 6) % 7
    sunday = today - dt.timedelta(days=days_since_sunday)
    saturday = sunday - dt.timedelta(days=1)
    return saturday, sunday


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
