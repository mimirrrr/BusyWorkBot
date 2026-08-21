"""Phase 5: assembles the single self-contained end-of-season report file.

Plain f-string/template assembly, no Jinja2 -- a single-consumer template
doesn't earn a new dependency. AI-written narrative text renders inside a
visually distinct .ai-box (different background, explicit label) -- never
blended in with the program-computed numbers/charts, per the "code
computes, AI narrates" principle in CLAUDE.md/docs/PLAN.md.
"""

import datetime as dt
import html as html_lib

from forecast import VERDICT_LABELS

CZECH_DAYS = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]


def czech_day(day: dt.date) -> str:
    return CZECH_DAYS[day.weekday()]


def _fmt_date(iso_date: str | None) -> str:
    if iso_date is None:
        return "?"
    d = dt.date.fromisoformat(iso_date)
    return f"{czech_day(d)} {d.strftime('%d.%m.%Y')}"


def _fmt_verdict(key: str | None) -> str:
    return VERDICT_LABELS[key] if key else "—"


def _fmt_pct(fraction: float | None) -> str:
    return f"{fraction * 100:.0f}%" if fraction is not None else "—"


def _accuracy_row_html(label: str, group: dict) -> str:
    return (
        f"<tr><td>{html_lib.escape(label)}</td>"
        f"<td>{group['n']}</td><td>{group['correct']}</td>"
        f"<td>{_fmt_pct(group['accuracy'])}</td></tr>"
    )


def _day_table_rows(rows: list[dict]) -> str:
    out = []
    for r in sorted(rows, key=lambda r: r["den"] or ""):
        match = "—" if r["correct"] is None else ("✅" if r["correct"] else "❌")
        source = {"live": "live", "backfill": "backfill", None: "—"}.get(r.get("source"))
        note = html_lib.escape(r["poznamka"]) if r.get("poznamka") else ""
        sold = r["sold_product"] if r.get("sold_product") is not None else "—"
        out.append(
            "<tr>"
            f"<td>{_fmt_date(r['den'])}</td>"
            f"<td>{source}</td>"
            f"<td>{_fmt_verdict(r['predicted_verdict'])} "
            f"<span class='kind'>({r['prediction_kind'] or 'chybí'})</span></td>"
            f"<td>{_fmt_verdict(r['actual_verdict'])}</td>"
            f"<td class='match'>{match}</td>"
            f"<td>{sold}</td>"
            f"<td>{note}</td>"
            "</tr>"
        )
    return "\n".join(out)


def _confusion_table(confusion: dict) -> str:
    labels = confusion["labels"]
    matrix = confusion["matrix"]
    header = "<th>realita \\ predikce</th>" + "".join(f"<th>{VERDICT_LABELS[l]}</th>" for l in labels)
    rows = []
    for i, label in enumerate(labels):
        cells = "".join(f"<td>{matrix[i][j]}</td>" for j in range(len(labels)))
        rows.append(f"<tr><th>{VERDICT_LABELS[label]}</th>{cells}</tr>")
    return f"<table class='confusion'><tr>{header}</tr>{''.join(rows)}</table>"


def render_html(
    stats: dict, charts: dict, narratives: list[str],
    season_start: dt.date, season_end: dt.date, generated_at: dt.datetime,
) -> str:
    ai_paragraphs = "".join(f"<p>{html_lib.escape(p)}</p>" for p in narratives)
    rule = stats["by_rule_component"]
    werr = stats["weather_forecast_error"]
    rain_mae = f"{werr['rain_mm_mae']:.1f} mm" if werr["rain_mm_mae"] is not None else "—"
    temp_mae = f"{werr['temp_max_mae']:.1f} °C" if werr["temp_max_mae"] is not None else "—"
    empty_group = {"n": 0, "correct": 0, "accuracy": None}
    live_group = stats["by_backfill"].get("False", empty_group)
    backfill_group = stats["by_backfill"].get("True", empty_group)

    return f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Sezónní report {season_start.year}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; max-width: 900px; margin: 2rem auto;
         padding: 0 1rem; color: #1a1a1a; background: #fff; }}
  h1, h2 {{ color: #2c3e50; }}
  .meta {{ color: #7f8c8d; font-size: 0.9rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.9rem; }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: center; }}
  th {{ background: #f4f6f7; }}
  td:first-child, th:first-child {{ text-align: left; }}
  .kind {{ color: #95a5a6; font-size: 0.8em; }}
  .match {{ font-weight: bold; }}
  .charts {{ display: flex; flex-wrap: wrap; gap: 1rem; }}
  .charts img {{ max-width: 100%; height: auto; border: 1px solid #eee; border-radius: 4px; }}
  .ai-box {{
    background: #eef6fb; border: 1px solid #b8dcef; border-left: 4px solid #3498db;
    border-radius: 4px; padding: 1rem 1.25rem; margin: 1.5rem 0;
  }}
  .ai-box .label {{
    display: inline-block; font-size: 0.75rem; font-weight: bold; letter-spacing: 0.05em;
    text-transform: uppercase; color: #2980b9; margin-bottom: 0.5rem;
  }}
  .caveat {{ color: #c0392b; font-size: 0.85rem; }}
</style>
</head>
<body>

<h1>📊 Sezónní report {season_start.year}</h1>
<p class="meta">
  Sezóna {season_start.strftime('%d.%m.%Y')} – {season_end.strftime('%d.%m.%Y')} ·
  vygenerováno {generated_at.strftime('%d.%m.%Y %H:%M')} UTC
</p>

<div class="ai-box">
  <div class="label">🤖 AI-generated summary (Gemini)</div>
  {ai_paragraphs}
</div>

<h2>Celková přesnost</h2>
<table>
  <tr><th>Skupina</th><th>n</th><th>správně</th><th>přesnost</th></tr>
  {_accuracy_row_html("Celkem", stats["overall"])}
  {_accuracy_row_html("Live", live_group)}
  {_accuracy_row_html("Backfill (zpětně doplněné)", backfill_group)}
</table>
<p class="caveat">
  ⚠️ Backfill dny (2.5.–31.7.{season_start.year}) jsou doplněné zpětně z paměti a
  nemají uloženou skutečnou předpověď — jejich "predikce" je dopočítaná zpětně z
  reálného počasí, ne z toho, co bot v tu dobu skutečně řekl. Ber jejich přesnost
  jako méně spolehlivou než u live dní.
</p>

<h2>Vliv teplotního pravidla (demotion)</h2>
<table>
  <tr><th>Varianta pravidla</th><th>n</th><th>správně</th><th>přesnost</th></tr>
  {_accuracy_row_html("Jen srážky (bez teploty)", rule["rain_tier_only"])}
  {_accuracy_row_html("Plné pravidlo (srážky + teplota)", rule["full_rule"])}
</table>
<p class="meta">
  Teplotní demotion se spustil {rule['temp_demotion_triggered_n']}×,
  pomohl {rule['temp_demotion_helped_n']}× a uškodil {rule['temp_demotion_hurt_n']}×.
</p>

<h2>Chyba předpovědi počasí (jen live dny)</h2>
<p class="meta">
  Průměrná odchylka srážek: {rain_mae} ·
  Průměrná odchylka max. teploty: {temp_mae}
  {'' if werr['rain_mm_mae'] is not None else '(zatím žádná live data)'}
</p>
<table>
  <tr><th>Příčina špatné predikce</th><th>počet</th></tr>
  <tr><td>špatná předpověď počasí</td><td>{werr['bad_forecast']}</td></tr>
  <tr><td>špatné pravidlo (i při přesném počasí)</td><td>{werr['bad_rule']}</td></tr>
  <tr><td>obojí</td><td>{werr['both']}</td></tr>
  <tr><td>predikce byla správně</td><td>{werr['neither']}</td></tr>
</table>

<h2>Grafy</h2>
<div class="charts">
  <img src="data:image/png;base64,{charts['accuracy_bar']}" alt="Přesnost podle zdroje dat">
  <img src="data:image/png;base64,{charts['confusion_matrix']}" alt="Konfúzní matice">
  <img src="data:image/png;base64,{charts['scatter']}" alt="Srážky vs. teplota">
  <img src="data:image/png;base64,{charts['live_vs_backfill']}" alt="Live vs. backfill">
</div>

<h2>Konfúzní matice (čísla)</h2>
{_confusion_table(stats["confusion_matrix"])}

<h2>Všechny dny sezóny</h2>
<table>
  <tr><th>Den</th><th>Zdroj</th><th>Predikce</th><th>Realita</th><th>Shoda</th><th>Ks</th><th>Poznámka</th></tr>
  {_day_table_rows(stats["rows"])}
</table>

</body>
</html>
"""
