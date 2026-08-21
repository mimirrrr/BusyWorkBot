"""Shared helpers used by every scheduled script in src/: env/dotenv loading,
Components V2 text blocks, the Discord DM send, the season-window gate, and
Czech day names. Mirrors the shared-helper pattern retry.py already
establishes for retry-with-backoff.
"""

import os
import sys
import datetime as dt

from retry import connect_with_retry, request_with_retry

DISCORD_API = "https://discord.com/api/v10"

# Message flag that switches the payload to Components V2, needed for the
# divider line between Saturday and Sunday — classic embeds can't put
# anything between fields, only Components V2's Separator can.
IS_COMPONENTS_V2 = 1 << 15

# Python's %A depends on system locale (not reliably set on GitHub's runners),
# so day names are mapped by hand instead.
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


def text(content: str) -> dict:
    return {"type": 10, "content": content}


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
