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
    0: "clear ☀️",
    1: "mostly clear 🌤️",
    2: "partly cloudy ⛅",
    3: "overcast ☁️",
    45: "fog 🌫️", 48: "fog 🌫️",
    51: "drizzle 🌦️", 53: "drizzle 🌦️", 55: "drizzle 🌦️",
    61: "light rain 🌧️", 63: "rain 🌧️", 65: "heavy rain 🌧️",
    66: "freezing rain 🌧️", 67: "freezing rain 🌧️",
    71: "snow 🌨️", 73: "snow 🌨️", 75: "snow 🌨️", 77: "snow 🌨️",
    80: "showers 🌦️", 81: "showers 🌧️", 82: "heavy showers 🌧️",
    85: "snow showers 🌨️", 86: "snow showers 🌨️",
    95: "thunderstorm ⛈️", 96: "thunderstorm ⛈️", 99: "thunderstorm ⛈️",
}


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
            "name": day.strftime("%A %d.%m."),
            "value": "no forecast data ⚠️",
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
        f"🌧️ max **{max_prob:.0f}%** chance · {rain:.1f} mm total\n"
        f"💨 up to {wind:.0f} km/h"
    )
    return {
        "name": f"📅 {day.strftime('%A %d.%m.')}",
        "value": value,
        "inline": False,
        "max_rain_prob": max_prob,
    }


def embed_color(fields: list[dict]) -> int:
    """Pick a left-border color from the worst rain chance across the weekend.

    Placeholder heuristic for v1 — real verdict rules land in phase 3.
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
        "title": "📋 Weekend Forecast — Bělá",
        "description": f"working hours {work_start}:00–{work_end}:00",
        "color": embed_color(fields),
        "fields": [
            {"name": f["name"], "value": f["value"], "inline": f["inline"]}
            for f in fields
        ],
        "footer": {"text": f"v1 raw forecast · sent {today.strftime('%A %d.%m.%Y')} · verdict rules come in phase 3"},
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
