"""Phase 1: fetch the weekend forecast for the workplace and DM it to me on Discord.

Runs from GitHub Actions (Thu + Fri 17:00 Prague time) or locally:
    python src/forecast.py
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

from retry import connect_with_retry, request_with_retry

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
DISCORD_API = "https://discord.com/api/v10"

# Message flag that switches the payload to Components V2, needed for the
# divider line between Saturday and Sunday — classic embeds can't put
# anything between fields, only Components V2's Separator can.
IS_COMPONENTS_V2 = 1 << 15

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

# Rule engine v1 tiers, best (busiest) to worst — order matters, predict_verdict
# indexes into this. Keys match BUSYNESS in src/log_message.py.
TIERS = ["slammed", "busy", "normal", "slow", "dead"]
VERDICT_LABELS = {"slammed": "Slammed 🔥", "busy": "Busy 📈", "normal": "Normal",
                   "slow": "Slow 📉", "dead": "Dead 💀"}
# verdict key -> visited.name_v (db/schema.sql seed order)
VISITED_NAMES = {"dead": "velmi slabe", "slow": "slabe", "normal": "stredni",
                  "busy": "hodne", "slammed": "naval"}
REVERSE_VISITED_NAMES = {v: k for k, v in VISITED_NAMES.items()}


def predict_verdict(max_rain_prob: float, temp_max: float) -> str:
    """Rule engine v1: rain probability sets the base tier (dominant signal),
    extreme temperature demotes one tier (too hot -> pool instead; too cold ->
    early/late season). Temp is orientation-only, per docs/PLAN.md — it never
    promotes, only demotes at the extremes.
    """
    if max_rain_prob <= 10:
        idx = 0
    elif max_rain_prob <= 30:
        idx = 1
    elif max_rain_prob <= 50:
        idx = 2
    elif max_rain_prob <= 80:
        idx = 3
    else:
        idx = 4
    if temp_max >= 34 or temp_max < 17:
        idx = min(idx + 1, len(TIERS) - 1)
    return TIERS[idx]


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


def last_weekend(today: dt.date) -> tuple[dt.date, dt.date]:
    """Most recently completed Saturday and Sunday (mirrors src/log_message.py)."""
    days_since_sunday = (today.weekday() - 6) % 7
    sunday = today - dt.timedelta(days=days_since_sunday)
    return sunday - dt.timedelta(days=1), sunday


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
    r = request_with_retry("GET", OPEN_METEO, params=params, timeout=30)
    return r.json()


def fetch_archive(lat: float, lon: float, start: dt.date, end: dt.date, tz: str) -> dict:
    """Actual (post-hoc) weather from Open-Meteo's archive/reanalysis API —
    a different dataset than fetch_forecast's live forecast, typically
    published with several days of lag (see src/completeness_sweep.py's
    ARCHIVE_LAG_DAYS). No precipitation_probability here: that's a
    forecast-uncertainty concept, and the archive API returns it as null
    for every hour regardless, so it's not requested at all.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "timezone": tz,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    r = request_with_retry("GET", OPEN_METEO_ARCHIVE, params=params, timeout=30)
    return r.json()


def summarize_actual_day(hourly: dict, day: dt.date, work_start: int, work_end: int) -> dict:
    """Like summarize_day, but for actual (archive) weather: no rain
    probability and no verdict, since neither concept applies to something
    that already happened. Just the dominant condition and the totals/
    extremes over working hours, for weather_actual.
    """
    idx = [
        i for i, t in enumerate(hourly["time"])
        if t.startswith(day.isoformat()) and work_start <= int(t[11:13]) < work_end
    ]
    if not idx:
        return {"day": day, "has_data": False}

    temps = [hourly["temperature_2m"][i] for i in idx]
    rain = sum(hourly["precipitation"][i] or 0 for i in idx)
    wind = max(hourly["wind_speed_10m"][i] for i in idx)
    codes = [hourly["weather_code"][i] for i in idx]
    dominant = max(set(codes), key=codes.count)
    sky = WEATHER_CODES.get(dominant, f"code {dominant}")
    weather_label = sky.rsplit(" ", 1)[0]

    return {
        "day": day,
        "has_data": True,
        "weather_label": weather_label,
        "srazky": round(rain, 1),
        "teplota_min": round(min(temps)),
        "teplota_max": round(max(temps)),
        "wind_speed": round(wind),
    }


def summarize_day(hourly: dict, day: dt.date, work_start: int, work_end: int) -> dict:
    """One message block for one day, over working hours only, plus the rule
    engine's verdict and the raw numbers needed to store the prediction.

    has_data is False when Open-Meteo returned nothing for the window — the
    caller must skip storing a prediction in that case (nothing to compute it
    from).
    """
    idx = [
        i for i, t in enumerate(hourly["time"])
        if t.startswith(day.isoformat()) and work_start <= int(t[11:13]) < work_end
    ]
    if not idx:
        return {
            "day": day,
            "name": f"📅 {czech_day(day)} {day.strftime('%d.%m.')}",
            "value": "žádná data o počasí ⚠️",
            "max_rain_prob": 0,
            "has_data": False,
        }

    temps = [hourly["temperature_2m"][i] for i in idx]
    probs = [hourly["precipitation_probability"][i] or 0 for i in idx]
    rain = sum(hourly["precipitation"][i] or 0 for i in idx)
    wind = max(hourly["wind_speed_10m"][i] for i in idx)
    codes = [hourly["weather_code"][i] for i in idx]
    # dominant = most frequent code in the window
    dominant = max(set(codes), key=codes.count)
    sky = WEATHER_CODES.get(dominant, f"code {dominant}")
    # WEATHER_CODES values are "label emoji" — strip the trailing emoji token
    # to get the plain label stored in db/schema.sql's weathers table.
    weather_label = sky.rsplit(" ", 1)[0]
    max_prob = max(probs)
    temp_min, temp_max = min(temps), max(temps)
    verdict = predict_verdict(max_prob, temp_max)

    value = (
        f"{sky}\n"
        f"🌡️ **{temp_min:.0f}–{temp_max:.0f} °C**\n"
        f"🌧️ max **{max_prob:.0f}%** šance · {rain:.1f} mm celkem\n"
        f"💨 rychlost větru do {wind:.0f} km/h\n"
        f"➡️ **Verdikt: {VERDICT_LABELS[verdict]}**"
    )
    return {
        "day": day,
        "name": f"📅 {czech_day(day)} {day.strftime('%d.%m.')}",
        "value": value,
        "max_rain_prob": max_prob,
        "has_data": True,
        "verdict": verdict,
        "weather_label": weather_label,
        "chance_rain": round(max_prob),
        "srazky": round(rain, 1),
        "teplota_min": round(temp_min),
        "teplota_max": round(temp_max),
        "wind_speed": round(wind),
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
    # 2. send the message as Components V2 (no "embeds" alongside it)
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


def store_predictions(database_url: str, predikce_den: dt.date, days: list[dict]) -> None:
    """Upsert one weather_prediction row per day that had data. ON CONFLICT
    (den, predikce_den) so a re-run (e.g. manual workflow_dispatch retry) on
    the same day overwrites rather than erroring.
    """
    with connect_with_retry(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for d in days:
                if not d["has_data"]:
                    continue
                cur.execute(
                    """
                    INSERT INTO weather_prediction
                        (den, predikce_den, pocasi_id, chance_rain, srazky,
                         teplota_min, teplota_max, wind_speed, predikce_navstevnost_id)
                    VALUES (
                        %(den)s, %(predikce_den)s,
                        (SELECT id_w FROM weathers WHERE name_w = %(weather_label)s),
                        %(chance_rain)s, %(srazky)s, %(teplota_min)s, %(teplota_max)s,
                        %(wind_speed)s,
                        (SELECT id_v FROM visited WHERE name_v = %(visited_name)s)
                    )
                    ON CONFLICT (den, predikce_den) DO UPDATE SET
                        pocasi_id = EXCLUDED.pocasi_id,
                        chance_rain = EXCLUDED.chance_rain,
                        srazky = EXCLUDED.srazky,
                        teplota_min = EXCLUDED.teplota_min,
                        teplota_max = EXCLUDED.teplota_max,
                        wind_speed = EXCLUDED.wind_speed,
                        predikce_navstevnost_id = EXCLUDED.predikce_navstevnost_id
                    """,
                    {
                        "den": d["day"],
                        "predikce_den": predikce_den,
                        "weather_label": d["weather_label"],
                        "chance_rain": d["chance_rain"],
                        "srazky": d["srazky"],
                        "teplota_min": d["teplota_min"],
                        "teplota_max": d["teplota_max"],
                        "wind_speed": d["wind_speed"],
                        "visited_name": VISITED_NAMES[d["verdict"]],
                    },
                )


def fetch_last_weekend_comparison(database_url: str, saturday: dt.date, sunday: dt.date) -> list[dict]:
    """For each of last Sat/Sun: the Friday-run prediction (the "official" one,
    per docs/PLAN.md — its predikce_den always sorts after Thursday's for the
    same den) and what was actually logged, if any. Either side can be missing
    (rule engine went live partway through the season; a day may not be
    logged yet) — the caller must render that gracefully, not assume both exist.
    """
    results = []
    with connect_with_retry(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for day in (saturday, sunday):
                cur.execute(
                    """
                    SELECT v.name_v FROM weather_prediction wp
                    JOIN visited v ON v.id_v = wp.predikce_navstevnost_id
                    WHERE wp.den = %s ORDER BY wp.predikce_den DESC LIMIT 1
                    """,
                    (day,),
                )
                pred = cur.fetchone()
                cur.execute(
                    """
                    SELECT v.name_v FROM user_input ui
                    JOIN visited v ON v.id_v = ui.visited_id
                    WHERE ui.den = %s
                    """,
                    (day,),
                )
                actual = cur.fetchone()
                results.append({
                    "day": day,
                    "predicted": REVERSE_VISITED_NAMES.get(pred[0]) if pred else None,
                    "actual": REVERSE_VISITED_NAMES.get(actual[0]) if actual else None,
                })
    return results


def comparison_line(c: dict) -> str:
    label = f"📅 {czech_day(c['day'])} {c['day'].strftime('%d.%m.')}"
    if c["predicted"] is None:
        return f"{label} — v DB nemám predikci (mimo sezónu pravidel)"
    predicted = VERDICT_LABELS[c["predicted"]]
    if c["actual"] is None:
        return f"{label} — predikce: **{predicted}**, realita: *zatím nezalogováno*"
    actual = VERDICT_LABELS[c["actual"]]
    match = "✅" if c["predicted"] == c["actual"] else "❌"
    return f"{label} — predikce: **{predicted}**, realita: **{actual}** {match}"


def main() -> None:
    load_dotenv()
    dry_run = "--dry-run" in sys.argv
    token = "" if dry_run else env("DISCORD_BOT_TOKEN")
    user_id = "" if dry_run else env("DISCORD_USER_ID")
    # Optional in --dry-run (only used if a local .env happens to have it, to
    # preview the Friday comparison block) but required for a real send.
    database_url = os.environ.get("DATABASE_URL") if dry_run else env("DATABASE_URL")
    lat = float(env("LAT", "49.97233"))       # Bělá 87, 747 23 Bělá (Opava)
    lon = float(env("LON", "18.14489"))
    tz = env("TZ_NAME", "Europe/Prague")
    work_start = int(env("WORK_START", "9"))  # working hours window for stats
    work_end = int(env("WORK_END", "20"))

    today = dt.datetime.now(ZoneInfo(tz)).date()
    if not dry_run and not is_in_season(database_url, today):
        print("Forecast: outside the configured season, skipping.")
        return

    saturday, sunday = next_weekend(today)

    data = fetch_forecast(lat, lon, saturday, sunday, tz)
    hourly = data["hourly"]

    fields = [
        summarize_day(hourly, saturday, work_start, work_end),
        summarize_day(hourly, sunday, work_start, work_end),
    ]

    now = dt.datetime.now(dt.timezone.utc)
    components = []

    # Friday's is the "official" prediction used for accuracy scoring (per
    # docs/PLAN.md), so only Friday's message looks back at last weekend.
    if today.weekday() == 4 and database_url:
        last_sat, last_sun = last_weekend(today)
        comparison = fetch_last_weekend_comparison(database_url, last_sat, last_sun)
        components.append({
            "type": 17,
            "accent_color": 0x95A5A6,
            "components": [
                text("🔁 **Minulý víkend: predikce vs. realita**"),
                text(comparison_line(comparison[0])),
                {"type": 14, "divider": True, "spacing": 1},
                text(comparison_line(comparison[1])),
            ],
        })

    components.append({
        "type": 17,  # Container — accent bar + grouped content, mirrors the old embed look
        "accent_color": 0x3498DB,  # flat blue for v1; embed_color() parked for phase 3 verdict rules
        "components": [
            text(f"📋 **Víkendová předpověď — Bělá**\notevřeno {work_start}:00–{work_end}:00"),
            text(f"{fields[0]['name']}\n{fields[0]['value']}"),
            {"type": 14, "divider": True, "spacing": 2},
            text(f"{fields[1]['name']}\n{fields[1]['value']}"),
            text(
                f"-# odesláno {czech_day(today)} {today.strftime('%d.%m.%Y')} "
                f"· <t:{int(now.timestamp())}:f>"
            ),
        ],
    })

    if dry_run:
        import json
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(components, indent=2, ensure_ascii=False))
        return
    discord_dm(token, user_id, components)
    store_predictions(database_url, today, fields)
    print("Forecast DM sent.")


if __name__ == "__main__":
    main()
