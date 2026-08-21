"""Phase 5: end-of-season report -- orchestrator.

Runs as a *second step* in the same Tuesday completeness-sweep.yml job,
right after completeness_sweep.py (see docs/PLAN.md Component 7):
    python src/season_report.py
    python src/season_report.py --dry-run --as-of 2026-10-10

Unlike every other recurring script, this deliberately does NOT call
is_in_season() -- its job only makes sense once the season is *over*, at
which point completeness_sweep.py's own is_in_season() gate has already
stopped that script from nagging/backfilling, so this script covers that
post-season tail itself. It is a no-op every week while today <= season_end,
then starts nagging (if data's incomplete) or generating the report (once
complete) every Tuesday after that, until season_config.report_sent_at is
set.

--as-of overrides "today" (orthogonal to --dry-run) so the whole pipeline,
including the season-over gate, can be exercised before the real season
ends.
"""

import json
import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import completeness_sweep
import report_charts
import report_html
import report_narrative
import report_stats
from retry import connect_with_retry, request_with_retry

DISCORD_API = "https://discord.com/api/v10"
IS_COMPONENTS_V2 = 1 << 15

SEASON_DATASET_QUERY = """
WITH season AS (
    SELECT %(season_start)s::date AS season_start, %(season_end)s::date AS season_end
),
weekend_days AS (
    SELECT d::date AS den
    FROM season, generate_series(season_start, season_end, interval '1 day') AS d
    WHERE EXTRACT(DOW FROM d) IN (0, 6)
),
latest_prediction AS (
    SELECT DISTINCT ON (den) den, predikce_den, pocasi_id, chance_rain, srazky,
           teplota_min, teplota_max, wind_speed, predikce_navstevnost_id
    FROM weather_prediction
    ORDER BY den, predikce_den DESC   -- Friday's row wins over Thursday's
)
SELECT
    wd.den,
    ui.visited_id AS actual_visited_id, av.name_v AS actual_visited_name,
    ui.source, ui.sold_product, ui.poznamka,
    wa.pocasi_id AS actual_pocasi_id, aw.name_w AS actual_weather_label,
    wa.srazky AS actual_srazky, wa.teplota_min AS actual_teplota_min,
    wa.teplota_max AS actual_teplota_max, wa.wind_speed AS actual_wind_speed,
    lp.predikce_den,
    lp.pocasi_id AS pred_pocasi_id, pw.name_w AS pred_weather_label,
    lp.chance_rain AS pred_chance_rain, lp.srazky AS pred_srazky,
    lp.teplota_min AS pred_teplota_min, lp.teplota_max AS pred_teplota_max,
    lp.wind_speed AS pred_wind_speed, pv.name_v AS pred_visited_name
FROM weekend_days wd
LEFT JOIN user_input ui        ON ui.den = wd.den
LEFT JOIN visited av            ON av.id_v = ui.visited_id
LEFT JOIN weather_actual wa     ON wa.den = wd.den
LEFT JOIN weathers aw           ON aw.id_w = wa.pocasi_id
LEFT JOIN latest_prediction lp  ON lp.den = wd.den
LEFT JOIN weathers pw           ON pw.id_w = lp.pocasi_id
LEFT JOIN visited pv            ON pv.id_v = lp.predikce_navstevnost_id
ORDER BY wd.den;
"""


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


def already_sent(config: dict) -> bool:
    """Not a bare null-check: >= updated_at self-invalidates once next
    year's season_config gets overwritten (the Worker's season_modal
    handler bumps updated_at on every overwrite of this singleton row).
    """
    return config["report_sent_at"] is not None and config["report_sent_at"] >= config["updated_at"]


def fetch_season_dataset(database_url: str, season_start: dt.date, season_end: dt.date) -> list[dict]:
    """One row per Sat/Sun in [season_start, season_end], joining the latest
    stored prediction (Friday beats Thursday), actual weather, and what got
    logged. Plain cur.description-zip into dicts (no psycopg.rows.dict_row).
    """
    with connect_with_retry(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(SEASON_DATASET_QUERY, {"season_start": season_start, "season_end": season_end})
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def mark_report_sent(database_url: str) -> None:
    with connect_with_retry(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE season_config SET report_sent_at = now() WHERE id = 1")


def discord_dm_with_file(
    token: str, user_id: str, components: list[dict],
    filename: str, file_bytes: bytes, content_type: str,
) -> None:
    """Multipart variant of the discord_dm() every other script uses --
    every existing discord_dm call is JSON-only Components V2 with no
    attachment support, and Discord requires the components payload as a
    payload_json form field (not a JSON body) once a file part is involved.
    """
    headers = {"Authorization": f"Bot {token}"}
    r = request_with_retry(
        "POST", f"{DISCORD_API}/users/@me/channels",
        headers=headers, json={"recipient_id": user_id}, timeout=30,
    )
    channel_id = r.json()["id"]
    payload = {"flags": IS_COMPONENTS_V2, "components": components}
    request_with_retry(
        "POST", f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=headers,
        data={"payload_json": json.dumps(payload)},
        files={"files[0]": (filename, file_bytes, content_type)},
        timeout=60,
    )


def build_completeness_nag_components(missing: list[dt.date]) -> list[dict]:
    """Same nag layout as completeness_sweep.py's regular Tuesday sweep,
    reusing its day_block/text/czech_day_count/czech_more_days helpers
    as-is, just with a season-specific header line.
    """
    shown = missing[:completeness_sweep.MAX_DAYS_PER_MESSAGE]
    overflow = missing[completeness_sweep.MAX_DAYS_PER_MESSAGE:]
    components = [completeness_sweep.text(
        f"⚠️ **Sezóna skončila, ale chybí ti zápis za "
        f"{completeness_sweep.czech_day_count(len(missing))}** — bez toho nejde "
        f"vygenerovat sezónní report:"
    )]
    for i, day in enumerate(shown):
        components.extend(completeness_sweep.day_block(day))
        if i < len(shown) - 1:
            components.append({"type": 14, "divider": True, "spacing": 1})
    if overflow:
        components.append(completeness_sweep.text(
            f"-# {completeness_sweep.czech_more_days(len(overflow))} chybí, doplní se v příštím sweepu"
        ))
    return components


def report_ready_components(season_start: dt.date, stats: dict) -> list[dict]:
    overall = stats["overall"]
    headline = (
        f"📊 **Sezónní report {season_start.year} je hotový!** "
        f"Přesnost predikce: {overall['accuracy'] * 100:.0f}% (n={overall['n']})."
        if overall["accuracy"] is not None else
        f"📊 **Sezónní report {season_start.year} je hotový!**"
    )
    return [completeness_sweep.text(headline)]


def main() -> None:
    load_dotenv()
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    as_of = dt.date.fromisoformat(args[args.index("--as-of") + 1]) if "--as-of" in args else None

    database_url = env("DATABASE_URL")
    tz = env("TZ_NAME", "Europe/Prague")
    lat = float(env("LAT", "49.97233"))
    lon = float(env("LON", "18.14489"))
    work_start = int(env("WORK_START", "9"))
    work_end = int(env("WORK_END", "20"))
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    gemini_model = env("GEMINI_MODEL", report_narrative.DEFAULT_MODEL)

    real_today = dt.datetime.now(ZoneInfo(tz)).date()
    today = as_of or real_today

    config = fetch_season_config(database_url)
    if config is None:
        print("season_report: no season_config row yet, nothing to report against.")
        return
    season_start, season_end = config["season_start"], config["season_end"]
    if today <= season_end:
        print("season_report: season not over yet, skipping (normal in-season no-op).")
        return
    if already_sent(config):
        print("season_report: report already sent for this season_config, skipping.")
        return

    # Completeness check, bounded by season_end (not real "today") -- so this
    # can never drift into flagging *next* season's weekends once
    # season_config gets overwritten (~April) if this row hasn't been
    # re-checked since.
    if not dry_run:
        completeness_sweep.fill_missing_actual_weather(
            database_url,
            min(real_today, season_end + dt.timedelta(days=completeness_sweep.ARCHIVE_LAG_DAYS)),
            lat, lon, tz, work_start, work_end,
        )
    missing = completeness_sweep.fetch_missing_days(database_url, season_end + dt.timedelta(days=1))
    if missing:
        components = build_completeness_nag_components(missing)
        if dry_run:
            sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps({"components": components}, indent=2, ensure_ascii=False))
            return
        token = env("DISCORD_BOT_TOKEN")
        user_id = env("DISCORD_USER_ID")
        completeness_sweep.discord_dm(token, user_id, components)
        print(f"season_report: {len(missing)} day(s) still missing, sent nag, no report this run.")
        return

    rows = fetch_season_dataset(database_url, season_start, season_end)
    stats = report_stats.season_summary_stats(rows)

    charts = {
        "accuracy_bar": report_charts.render_accuracy_bar(stats),
        "confusion_matrix": report_charts.render_confusion_matrix(stats),
        "scatter": report_charts.render_scatter(
            stats["scatter_points"], "Srážky vs. max. teplota", "Srážky (mm)", "Max. teplota (°C)"
        ),
        "live_vs_backfill": report_charts.render_live_vs_backfill(stats),
    }

    if gemini_api_key:
        narrative_text = report_narrative.call_gemini(
            gemini_api_key, gemini_model, report_narrative.build_prompt(stats)
        )
    else:
        narrative_text = report_narrative.FALLBACK_NARRATIVE
    narratives = [p.strip() for p in narrative_text.split("\n\n") if p.strip()]

    generated_at = dt.datetime.now(dt.timezone.utc)
    html_out = report_html.render_html(stats, charts, narratives, season_start, season_end, generated_at)

    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(reports_dir, f"season-{season_start.year}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"season_report: wrote {report_path}")

    if dry_run:
        print("season_report: --dry-run, not sending to Discord or marking report_sent_at.")
        return

    token = env("DISCORD_BOT_TOKEN")
    user_id = env("DISCORD_USER_ID")
    discord_dm_with_file(
        token, user_id, report_ready_components(season_start, stats),
        filename=f"season-{season_start.year}.html",
        file_bytes=html_out.encode("utf-8"),
        content_type="text/html",
    )
    mark_report_sent(database_url)
    print("season_report: report sent and report_sent_at stamped.")


if __name__ == "__main__":
    main()
