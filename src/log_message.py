"""Phase 2: Monday logging message — one-tap buttons to record how busy last
weekend actually was.

Runs from GitHub Actions (Monday 08:00 Prague time) or locally:
    python src/log_message.py
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import requests

DISCORD_API = "https://discord.com/api/v10"

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


def day_row(day: dt.date) -> dict:
    """One action row of 5 busyness buttons for a single day.

    custom_id carries the actual date (not "sat"/"sun") so the Worker never
    has to guess which weekend a stale message button belongs to.
    """
    return {
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
    }


def note_row(saturday: dt.date, sunday: dt.date) -> dict:
    return {
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 2,
                "label": f"+ note {czech_day(saturday)[:2]}",
                "custom_id": f"note:{saturday.isoformat()}",
            },
            {
                "type": 2,
                "style": 2,
                "label": f"+ note {czech_day(sunday)[:2]}",
                "custom_id": f"note:{sunday.isoformat()}",
            },
        ],
    }


def discord_dm(token: str, user_id: str, content: str, components: list[dict]) -> None:
    headers = {"Authorization": f"Bot {token}"}
    # 1. open (or reuse) the DM channel with me
    r = requests.post(
        f"{DISCORD_API}/users/@me/channels",
        headers=headers, json={"recipient_id": user_id}, timeout=30,
    )
    r.raise_for_status()
    channel_id = r.json()["id"]
    # 2. send the message with button components
    r = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=headers, json={"content": content, "components": components}, timeout=30,
    )
    r.raise_for_status()


def main() -> None:
    load_dotenv()
    dry_run = "--dry-run" in sys.argv
    token = "" if dry_run else env("DISCORD_BOT_TOKEN")
    user_id = "" if dry_run else env("DISCORD_USER_ID")
    tz = env("TZ_NAME", "Europe/Prague")

    today = dt.datetime.now(ZoneInfo(tz)).date()
    saturday, sunday = last_weekend(today)

    content = (
        f"📝 **Jak bylo o víkendu?** ({saturday.strftime('%d.%m.')}–{sunday.strftime('%d.%m.')})\n"
        f"1. řada = **Sobota**, 2. řada = **Neděle**. Klepnutí znovu přepíše zápis."
    )
    components = [
        day_row(saturday),
        day_row(sunday),
        note_row(saturday, sunday),
    ]

    if dry_run:
        import json
        print(json.dumps({"content": content, "components": components}, indent=2, ensure_ascii=False))
        return
    discord_dm(token, user_id, content, components)
    print("Logging message sent.")


if __name__ == "__main__":
    main()
