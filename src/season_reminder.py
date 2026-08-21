"""Phase 4: yearly nudge to set this season's start/end dates.

Runs from GitHub Actions (once a year, around late April) or locally:
    python src/season_reminder.py

Sends a DM with a button that opens a Discord modal (handled by the Worker's
season_modal handler) to set db/schema.sql's season_config singleton row.
Every other scheduled script reads that row to skip itself outside the
season. Doesn't touch the DB itself — this script only sends the prompt.
"""

import sys

from common import load_dotenv, env, text, discord_dm


def main() -> None:
    load_dotenv()
    dry_run = "--dry-run" in sys.argv
    token = "" if dry_run else env("DISCORD_BOT_TOKEN")
    user_id = "" if dry_run else env("DISCORD_USER_ID")

    components = [
        text(
            "🌱 **Nová sezóna?** Nastav prosím začátek a konec, ať vím, "
            "kdy ti mám posílat předpovědi a logovací zprávy."
        ),
        {
            "type": 1,
            "components": [{
                "type": 2,
                "style": 1,
                "label": "Nastavit sezónu",
                "custom_id": "season_setup",
            }],
        },
    ]

    if dry_run:
        import json
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps({"components": components}, indent=2, ensure_ascii=False))
        return
    discord_dm(token, user_id, components)
    print("Season reminder sent.")


if __name__ == "__main__":
    main()
