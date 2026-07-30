"""Phase 1: fetch the weekend forecast for the workplace and DM it to me on Discord.

Runs from GitHub Actions (Thu + Fri 17:00 Prague time) or locally:
    python src/forecast.py
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import requests

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
DISCORD_API = "https://discord.com/api/v10"

# Weather code groups (WMO codes used by Open-Meteo)
WEATHER_CODES = {
    0: "slunečno ☀️",
    1: "lehce zataženo 🌤️",
    2: "oblačno ⛅",
    3: "zataženo ☁️",
    45: "mlha 🌫️", 48: "mlha 🌫️",
    51: "mrholení 🌦️", 53: "mrholení 🌦️", 55: "mrholení 🌦️",
    61: "mírný déšť 🌧️", 63: "déšť 🌧️", 65: "silný déšť 🌧️",
    66: "ledový déšť 🌧️", 67: "ledový déšť 🌧️",
    71: "sníh 🌨️", 73: "sníh 🌨️", 75: "sníh 🌨️", 77: "sníh 🌨️",
    80: "přeháňky 🌦️", 81: "přeháňky 🌧️", 82: "silné přeháňky 🌧️",
    85: "sněhové přeháňky 🌨️", 86: "sněhové přeháňky 🌨️",
    95: "bouřky ⛈️", 96: "bouřky ⛈️", 99: "bouřky ⛈️",
}

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


def next_weekend(today: dt.date) -> tuple[dt.date, dt.date]:
    """Upcoming Saturday and Sunday (if today is Saturday, that's this weekend)."""
    saturday = today + dt.timedelta(days=(5 - today.weekday()) % 7)
    return saturday, saturday + dt.timedelta(days=1)


def fetch_forecast(lat: float, lon: float, start: dt.date, end: dt.date, tz: str) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation_probability,precipitation,"
                  "wind_speed_10m,weather_code",
        "timezone": tz,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    r = requests.get(OPEN_METEO, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def summarize_day(hourly: dict, day: dt.date, work_start: int, work_end: int) -> dict:
    """One embed field for one day, over working hours only.

    Returns {"name": ..., "value": ..., "inline": False, "max_rain_prob": float}
    — max_rain_prob feeds the embed's overall color pick.
    """
    idx = [
        i for i, t in enumerate(hourly["time"])
        if t.startswith(day.isoformat()) and work_start <= int(t[11:13]) < work_end
    ]
    if not idx:
        return {
            "name": f"📅 {czech_day(day)} {day.strftime('%d.%m.')}",
            "value": "žádná data o počasí ⚠️",
            "inline": False,
            "max_rain_prob": 0,
        }

    temps = [hourly["temperature_2m"][i] for i in idx]
    probs = [hourly["precipitation_probability"][i] or 0 for i in idx]
    rain = sum(hourly["precipitation"][i] or 0 for i in idx)
    wind = max(hourly["wind_speed_10m"][i] for i in idx)
    codes = [hourly["weather_code"][i] for i in idx]
    # dominant = most frequent code in the window
    dominant = max(set(codes), key=codes.count)
    sky = WEATHER_CODES.get(dominant, f"code {dominant}")
    max_prob = max(probs)

    value = (
        f"{sky}\n"
        f"🌡️ **{min(temps):.0f}–{max(temps):.0f} °C**\n"
        f"🌧️ max **{max_prob:.0f}%** šance · {rain:.1f} mm celkem\n"
        f"💨 rychlost větru do {wind:.0f} km/h"
    )
    return {
        "name": f"📅 {czech_day(day)} {day.strftime('%d.%m.')}",
        "value": value,
        "inline": False,
        "max_rain_prob": max_prob,
    }


def embed_color(fields: list[dict]) -> int:
    """Pick a left-border color from the worst rain chance across the weekend.

    Parked for phase 3 — v1 uses a flat blue (see main()) since the
    red/yellow/green scheme looked noisy before real verdict rules exist.
    """
    worst = max((f["max_rain_prob"] for f in fields), default=0)
    if worst >= 60:
        return 0xE74C3C  # red — likely wet
    if worst >= 30:
        return 0xF1C40F  # yellow — mixed
    return 0x2ECC71      # green — looking dry


def discord_dm(token: str, user_id: str, embed: dict) -> None:
    headers = {"Authorization": f"Bot {token}"}
    # 1. open (or reuse) the DM channel with me
    r = requests.post(
        f"{DISCORD_API}/users/@me/channels",
        headers=headers, json={"recipient_id": user_id}, timeout=30,
    )
    r.raise_for_status()
    channel_id = r.json()["id"]
    # 2. send the message as an embed (title, colored border, fields, footer)
    r = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=headers, json={"embeds": [embed]}, timeout=30,
    )
    r.raise_for_status()


def main() -> None:
    load_dotenv()
    dry_run = "--dry-run" in sys.argv
    token = "" if dry_run else env("DISCORD_BOT_TOKEN")
    user_id = "" if dry_run else env("DISCORD_USER_ID")
    lat = float(env("LAT", "49.97233"))       # Bělá 87, 747 23 Bělá (Opava)
    lon = float(env("LON", "18.14489"))
    tz = env("TZ_NAME", "Europe/Prague")
    work_start = int(env("WORK_START", "9"))  # working hours window for stats
    work_end = int(env("WORK_END", "20"))

    today = dt.datetime.now(ZoneInfo(tz)).date()
    saturday, sunday = next_weekend(today)

    data = fetch_forecast(lat, lon, saturday, sunday, tz)
    hourly = data["hourly"]

    fields = [
        summarize_day(hourly, saturday, work_start, work_end),
        summarize_day(hourly, sunday, work_start, work_end),
    ]

    embed = {
        "title": "📋 Víkendová předpověď — Bělá",
        "description": f"otevřeno {work_start}:00–{work_end}:00",
        "color": 0x3498DB,  # flat blue for v1; embed_color() parked for phase 3 verdict rules
        # Discord already adds spacing between description/fields and between
        # each field on its own — no manual spacer fields needed.
        "fields": [
            {"name": fields[0]["name"], "value": fields[0]["value"], "inline": False},
            {"name": fields[1]["name"], "value": fields[1]["value"], "inline": False},
        ],
        "footer": {"text": f"v1 · odesláno {czech_day(today)} {today.strftime('%d.%m.%Y')} · pravidla vyhodnocení přijdou ve fázi 3"},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    if dry_run:
        import json
        print(json.dumps(embed, indent=2, ensure_ascii=False))
        return
    discord_dm(token, user_id, embed)
    print("Forecast DM sent.")


if __name__ == "__main__":
    main()
