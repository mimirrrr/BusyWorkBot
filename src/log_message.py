"""Phase 2: Monday logging message — one-tap buttons to record how busy last
weekend actually was.

Runs from GitHub Actions (Monday 08:00 Prague time) or locally:
    python src/log_message.py
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

from common import (
    BUSYNESS,
    czech_day,
    day_block,
    discord_dm,
    env,
    is_in_season,
    last_weekend,
    load_dotenv,
    text,
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
