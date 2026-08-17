"""Phase 4: yearly nudge to set this season's start/end dates.

Runs from GitHub Actions (once a year, around late April) or locally:
    python src/season_reminder.py

Sends a DM with a button that opens a Discord modal (handled by the Worker's
season_modal handler) to set db/schema.sql's season_config singleton row.
Every other scheduled script reads that row to skip itself outside the
season. Doesn't touch the DB itself — this script only sends the prompt.
"""

import os
import sys

import requests

DISCORD_API = "https://discord.com/api/v10"
IS_COMPONENTS_V2 = 1 << 15


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
