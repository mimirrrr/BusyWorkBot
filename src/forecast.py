"""Phase 1: fetch the weekend forecast for the workplace and DM it to me on Discord.

Runs from GitHub Actions (Thu + Fri 17:00 Prague time) or locally:
    python src/forecast.py
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

from common import (
    czech_day,
    discord_dm,
    env,
    is_in_season,
    last_weekend,
    load_dotenv,
    text,
)
from retry import connect_with_retry, request_with_retry

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

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

# Rule engine v1 tiers, best (busiest) to worst — order matters, predict_verdict
# indexes into this. Keys match BUSYNESS in src/log_message.py.
TIERS = ["slammed", "busy", "normal", "slow", "dead"]
VERDICT_LABELS = {"slammed": "Slammed 🔥", "busy": "Busy 📈", "normal": "Normal",
                   "slow": "Slow 📉", "dead": "Dead 💀"}
# verdict key -> visited.name_v (db/schema.sql seed order)
VISITED_NAMES = {"dead": "velmi slabe", "slow": "slabe", "normal": "stredni",
                  "busy": "hodne", "slammed": "naval"}
REVERSE_VISITED_NAMES = {v: k for k, v in VISITED_NAMES.items()}


def rain_tier_only_verdict(max_rain_prob: float) -> str:
    """Rain probability alone determines the base tier (dominant signal)."""
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
    return TIERS[idx]


def predict_verdict(max_rain_prob: float, temp_max: float) -> str:
    """Rule engine v1: rain probability sets the base tier (dominant signal),
    extreme temperature demotes one tier (too hot -> pool instead; too cold ->
    early/late season). Temp is orientation-only, per docs/PLAN.md — it never
    promotes, only demotes at the extremes.
    """
    base = rain_tier_only_verdict(max_rain_prob)
    idx = TIERS.index(base)
    if temp_max >= 34 or temp_max < 17:
        idx = min(idx + 1, len(TIERS) - 1)
    return TIERS[idx]


def scheduled_weekday(schedule_cron: str) -> int | None:
    """weekday() this run was scheduled for, per the cron string GitHub
    actually fired (github.event.schedule, passed in as SCHEDULE_CRON --
    forecast.yml has two schedule triggers, Thu and Fri, so this is how the
    script tells them apart instead of guessing from today's date). None for
    workflow_dispatch/local runs, which don't set it.
    """
    if "THU" in schedule_cron:
        return 3
    if "FRI" in schedule_cron:
        return 4
    return None


def is_official_run(schedule_cron: str, today: dt.date) -> bool:
    """True when this run should be treated as the "official" Friday
    forecast: prepends the last-weekend recap, and its predikce_den is the
    row accuracy scoring keys off (per docs/PLAN.md).

    Prefers scheduled_weekday(schedule_cron) over today.weekday() -- a
    severely delayed GitHub Actions run can execute on the wrong calendar
    day (e.g. Friday's job rolling past midnight into Saturday), and
    inferring purely from today's date would then silently drop the recap
    instead of just running a day late. Falls back to weekday when there's
    no schedule to go on (workflow_dispatch, local run, --dry-run).
    """
    expected = scheduled_weekday(schedule_cron)
    if expected is not None:
        return expected == 4
    return today.weekday() == 4


def intended_run_date(schedule_cron: str, today: dt.date) -> dt.date:
    """The calendar date this run was actually scheduled for (Thu or Fri),
    shifting `today` backward when a delayed GitHub Actions run rolls past
    midnight into the next day. Used for the message label and for
    predikce_den, so a late-night delayed run still reads/stores as the day
    it was meant to represent instead of colliding with the same week's other
    run once it fires on schedule (see is_official_run's docstring for why
    the schedule string is trusted over today.weekday()). Only ever shifts
    backward (a run can't fire before its own schedule); falls back to
    `today` when there's no schedule to go on (workflow_dispatch, local run,
    --dry-run).
    """
    expected = scheduled_weekday(schedule_cron)
    if expected is None:
        return today
    return today - dt.timedelta(days=(today.weekday() - expected) % 7)


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


def _extract_working_hours_indices(hourly: dict, day: dt.date, work_start: int, work_end: int) -> list[int]:
    return [
        i for i, t in enumerate(hourly["time"])
        if t.startswith(day.isoformat()) and work_start <= int(t[11:13]) < work_end
    ]


def _aggregate_working_hours_weather(hourly: dict, idx: list[int]) -> dict:
    temps = [hourly["temperature_2m"][i] for i in idx]
    rain = sum(hourly["precipitation"][i] or 0 for i in idx)
    wind = max(hourly["wind_speed_10m"][i] for i in idx)
    codes = [hourly["weather_code"][i] for i in idx]
    dominant = max(set(codes), key=codes.count)
    sky = WEATHER_CODES.get(dominant, f"code {dominant}")
    weather_label = sky.rsplit(" ", 1)[0]
    return {
        "temps": temps,
        "rain": rain,
        "wind": wind,
        "sky": sky,
        "weather_label": weather_label,
    }


def summarize_actual_day(hourly: dict, day: dt.date, work_start: int, work_end: int) -> dict:
    """Like summarize_day, but for actual (archive) weather: no rain
    probability and no verdict, since neither concept applies to something
    that already happened. Just the dominant condition and the totals/
    extremes over working hours, for weather_actual.
    """
    idx = _extract_working_hours_indices(hourly, day, work_start, work_end)
    if not idx:
        return {"day": day, "has_data": False}

    agg = _aggregate_working_hours_weather(hourly, idx)
    return {
        "day": day,
        "has_data": True,
        "weather_label": agg["weather_label"],
        "srazky": round(agg["rain"], 1),
        "teplota_min": round(min(agg["temps"])),
        "teplota_max": round(max(agg["temps"])),
        "wind_speed": round(agg["wind"]),
    }


def summarize_day(hourly: dict, day: dt.date, work_start: int, work_end: int) -> dict:
    """One message block for one day, over working hours only, plus the rule
    engine's verdict and the raw numbers needed to store the prediction.

    has_data is False when Open-Meteo returned nothing for the window — the
    caller must skip storing a prediction in that case (nothing to compute it
    from).
    """
    idx = _extract_working_hours_indices(hourly, day, work_start, work_end)
    if not idx:
        return {
            "day": day,
            "name": f"📅 {czech_day(day)} {day.strftime('%d.%m.')}",
            "value": "žádná data o počasí ⚠️",
            "max_rain_prob": 0,
            "has_data": False,
        }

    agg = _aggregate_working_hours_weather(hourly, idx)
    probs = [hourly["precipitation_probability"][i] or 0 for i in idx]
    max_prob = max(probs)
    temp_min, temp_max = min(agg["temps"]), max(agg["temps"])
    verdict = predict_verdict(max_prob, temp_max)

    value = (
        f"{agg['sky']}\n"
        f"🌡️ **{temp_min:.0f}–{temp_max:.0f} °C**\n"
        f"🌧️ max **{max_prob:.0f}%** šance · {agg['rain']:.1f} mm celkem\n"
        f"💨 rychlost větru do {agg['wind']:.0f} km/h\n"
        f"➡️ **Verdikt: {VERDICT_LABELS[verdict]}**"
    )
    return {
        "day": day,
        "name": f"📅 {czech_day(day)} {day.strftime('%d.%m.')}",
        "value": value,
        "max_rain_prob": max_prob,
        "has_data": True,
        "verdict": verdict,
        "weather_label": agg["weather_label"],
        "chance_rain": round(max_prob),
        "srazky": round(agg["rain"], 1),
        "teplota_min": round(temp_min),
        "teplota_max": round(temp_max),
        "wind_speed": round(agg["wind"]),
    }


def store_predictions(database_url: str, predikce_den: dt.date, days: list[dict]) -> None:
    """Upsert one weather_prediction row per day that had data. ON CONFLICT
    (den, predikce_den) so a re-run (e.g. manual workflow_dispatch retry) on
    the same day overwrites rather than erroring.
    """
    valid_days = [
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
        }
        for d in days
        if d.get("has_data")
    ]
    if not valid_days:
        return

    with connect_with_retry(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.executemany(
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
                valid_days,
            )


def fetch_last_weekend_comparison(database_url: str, saturday: dt.date, sunday: dt.date) -> list[dict]:
    """For each of last Sat/Sun: the Friday-run prediction (the "official" one,
    per docs/PLAN.md — its predikce_den always sorts after Thursday's for the
    same den) and what was actually logged, if any. Either side can be missing
    (rule engine went live partway through the season; a day may not be
    logged yet) — the caller must render that gracefully, not assume both exist.
    """
    with connect_with_retry(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    d.den,
                    (
                        SELECT v.name_v FROM weather_prediction wp
                        JOIN visited v ON v.id_v = wp.predikce_navstevnost_id
                        WHERE wp.den = d.den
                        ORDER BY wp.predikce_den DESC
                        LIMIT 1
                    ) AS pred_name,
                    (
                        SELECT v.name_v FROM user_input ui
                        JOIN visited v ON v.id_v = ui.visited_id
                        WHERE ui.den = d.den
                        LIMIT 1
                    ) AS actual_name
                FROM (VALUES (%(sat)s::date), (%(sun)s::date)) AS d(den)
                ORDER BY d.den
                """,
                {"sat": saturday, "sun": sunday},
            )
            rows = cur.fetchall()
            return [
                {
                    "day": row[0],
                    "predicted": REVERSE_VISITED_NAMES.get(row[1]) if row[1] else None,
                    "actual": REVERSE_VISITED_NAMES.get(row[2]) if row[2] else None,
                }
                for row in rows
            ]


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

    schedule_cron = os.environ.get("SCHEDULE_CRON", "")
    predikce_den = intended_run_date(schedule_cron, today)
    expected_weekday = scheduled_weekday(schedule_cron)
    if expected_weekday is not None and expected_weekday != today.weekday():
        print(
            f"Forecast: WARNING - scheduled for weekday {expected_weekday} "
            f"('{schedule_cron}') but running on weekday {today.weekday()} "
            f"({czech_day(today)}) - likely a delayed GitHub Actions run. "
            f"Treating this as the "
            f"{'official Friday' if is_official_run(schedule_cron, today) else 'early Thursday'} "
            f"run based on the cron trigger, not today's date."
        )

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
    if is_official_run(schedule_cron, today) and database_url:
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
            text(f"📋 **Víkendová předpověď — {czech_day(predikce_den)}**\notevřeno {work_start}:00–{work_end}:00"),
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
    store_predictions(database_url, predikce_den, fields)
    print("Forecast DM sent.")


if __name__ == "__main__":
    main()
