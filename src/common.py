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


def czech_day(day: dt.date) -> str:
    return CZECH_DAYS[day.weekday()]


def last_weekend(today: dt.date) -> tuple[dt.date, dt.date]:
    """Most recently completed Saturday and Sunday."""
    days_since_sunday = (today.weekday() - 6) % 7
    sunday = today - dt.timedelta(days=days_since_sunday)
    return sunday - dt.timedelta(days=1), sunday


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


def discord_dm_with_file(
    token: str, user_id: str, components: list[dict],
    filename: str, file_bytes: bytes, content_type: str,
) -> None:
    """Multipart variant of discord_dm() with file attachment support."""
    import json
    headers = {"Authorization": f"Bot {token}"}
    r = request_with_retry(
        "POST", f"{DISCORD_API}/users/@me/channels",
        headers=headers, json={"recipient_id": user_id}, timeout=30,
    )
    channel_id = r.json()["id"]
    payload = {
        "flags": IS_COMPONENTS_V2,
        "components": components + [{"type": 13, "file": {"url": f"attachment://{filename}"}}],
        "attachments": [{"id": 0, "filename": filename}],
    }
    request_with_retry(
        "POST", f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=headers,
        data={"payload_json": json.dumps(payload)},
        files={"files[0]": (filename, file_bytes, content_type)},
        timeout=60,
    )


def fetch_season_config(database_url: str) -> dict | None:
    with connect_with_retry(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT season_start, season_end, report_sent_at, updated_at "
                "FROM season_config WHERE id = 1"
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {"season_start": row[0], "season_end": row[1], "report_sent_at": row[2], "updated_at": row[3]}


def is_in_season(database_url: str, today: dt.date) -> bool:
    """Fails OPEN: True (keep running) if season_config has no row yet, so
    nothing silently breaks before the season has ever been configured.
    """
    config = fetch_season_config(database_url)
    if config is None:
        return True
    return config["season_start"] <= today <= config["season_end"]
