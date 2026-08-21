"""Phase 5: pure stats functions for the end-of-season report.

No DB, no network, no matplotlib here — everything takes plain dicts/lists
and returns plain dicts/lists, so it's cheap to unit test (see
tests/test_report_stats.py) and safe to json.dumps() straight into the
Gemini prompt in src/report_narrative.py.

season_summary_stats() is the single entry point src/season_report.py calls;
everything else here is a building block for it.
"""

import forecast

# Anchor points for estimate_rain_prob_from_mm's piecewise-linear proxy.
_RAIN_PROB_ANCHORS = [(0.0, 0.0), (1.0, 20.0), (5.0, 50.0), (20.0, 100.0)]


def estimate_rain_prob_from_mm(srazky_mm: float) -> float:
    """Piecewise-linear stand-in for a rain *probability*, derived from actual
    rainfall (mm) that already fell. This is NOT a real probability -- it
    only exists to retroactively score backfill weekends (2.5.-31.7.2026),
    which have no stored forecast (the bot wasn't running yet) and therefore
    no real chance_rain to score against. Anchors: 0mm->0%, 1mm->20%,
    5mm->50%, 20mm+->100% (capped).
    """
    if srazky_mm <= _RAIN_PROB_ANCHORS[0][0]:
        return _RAIN_PROB_ANCHORS[0][1]
    if srazky_mm >= _RAIN_PROB_ANCHORS[-1][0]:
        return 100.0
    for (x0, y0), (x1, y1) in zip(_RAIN_PROB_ANCHORS, _RAIN_PROB_ANCHORS[1:]):
        if x0 <= srazky_mm <= x1:
            frac = (srazky_mm - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return 100.0  # unreachable, keeps type checkers happy


def retroactive_predicted_verdict(srazky_mm: float, teplota_max: float) -> str:
    """"What would the rule engine have said, had it seen this actual weather
    as a forecast?" -- how backfill weekends (and live weekends' weather-
    error analysis) get scored against the rule engine at all.
    """
    return forecast.predict_verdict(estimate_rain_prob_from_mm(srazky_mm), teplota_max)


def rain_tier_only_verdict(max_rain_prob: float) -> str:
    """Same tier boundaries as forecast.predict_verdict, but WITHOUT the
    extreme-temp demotion -- isolates the rain rule's standalone accuracy so
    accuracy_by_rule_component can tell whether the temp demotion helps or
    hurts.
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
    return forecast.TIERS[idx]


def to_verdict_key(visited_name: str | None) -> str | None:
    if visited_name is None:
        return None
    return forecast.REVERSE_VISITED_NAMES.get(visited_name)


def _num(x):
    """Normalize a DB numeric (psycopg returns Decimal for NUMERIC columns)
    to a plain float, so the whole stats blob stays json.dumps-able for
    report_narrative.py's prompt. None passes through.
    """
    return None if x is None else float(x)


def _iso(d):
    """date -> ISO string (same json.dumps-ability reason as _num); anything
    that isn't a date (e.g. already a string, from a synthetic test row)
    passes through unchanged.
    """
    return d.isoformat() if hasattr(d, "isoformat") else d


def enrich_row(row: dict) -> dict:
    """One fetch_season_dataset() row -> adds the fields analysis/reporting
    actually needs. Also normalizes den/predikce_den to ISO strings and every
    Decimal-typed weather number to float, so the result (and therefore
    season_summary_stats()'s whole blob) is safely json.dumps-able.

    prediction_kind picks "forecast" when a real weather_prediction row
    exists (live weekends, Aug 2026+) -- scoring against what was actually
    predicted, no hindsight -- and falls back to "retroactive" (score the
    rule engine against actual weather, since backfill weekends have no
    stored forecast) when only weather_actual is available. None when
    neither exists yet (day not old enough for the archive API).
    """
    out = dict(row)
    out["den"] = _iso(row.get("den"))
    out["predikce_den"] = _iso(row.get("predikce_den"))
    for key in ("actual_srazky", "actual_teplota_min", "actual_teplota_max", "actual_wind_speed",
                "pred_srazky", "pred_teplota_min", "pred_teplota_max", "pred_wind_speed",
                "pred_chance_rain"):
        out[key] = _num(row.get(key))

    out["is_backfill"] = row.get("source") == "backfill"
    out["actual_verdict"] = to_verdict_key(row.get("actual_visited_name"))

    has_actual_weather = out["actual_srazky"] is not None and out["actual_teplota_max"] is not None
    if row.get("pred_visited_name") is not None:
        out["prediction_kind"] = "forecast"
        out["predicted_verdict"] = to_verdict_key(row.get("pred_visited_name"))
        out["rain_prob_used"] = out["pred_chance_rain"]
        out["temp_max_used"] = out["pred_teplota_max"]
    elif has_actual_weather:
        out["prediction_kind"] = "retroactive"
        out["predicted_verdict"] = retroactive_predicted_verdict(out["actual_srazky"], out["actual_teplota_max"])
        out["rain_prob_used"] = estimate_rain_prob_from_mm(out["actual_srazky"])
        out["temp_max_used"] = out["actual_teplota_max"]
    else:
        out["prediction_kind"] = None
        out["predicted_verdict"] = None
        out["rain_prob_used"] = None
        out["temp_max_used"] = None

    if out["actual_verdict"] is None or out["predicted_verdict"] is None:
        out["correct"] = None
    else:
        out["correct"] = out["predicted_verdict"] == out["actual_verdict"]
    out["complete"] = out["actual_verdict"] is not None and out["predicted_verdict"] is not None
    return out


def enrich_dataset(rows: list[dict]) -> list[dict]:
    return [enrich_row(r) for r in rows]


def overall_accuracy(rows: list[dict]) -> dict:
    """accuracy is None at n==0 -- never divide by zero, needed since this
    also runs against a partial season during --as-of testing.
    """
    considered = [r for r in rows if r.get("correct") is not None]
    n = len(considered)
    correct = sum(1 for r in considered if r["correct"])
    return {"n": n, "correct": correct, "accuracy": (correct / n) if n else None}


def accuracy_by_split(rows: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r.get(key)), []).append(r)
    return {group_key: overall_accuracy(group_rows) for group_key, group_rows in groups.items()}


def accuracy_by_rule_component(rows: list[dict]) -> dict:
    rain_only_rows = []
    full_rows = []
    triggered_n = helped_n = hurt_n = 0
    for r in rows:
        if r.get("actual_verdict") is None or r.get("rain_prob_used") is None or r.get("temp_max_used") is None:
            continue
        rain_only_verdict = rain_tier_only_verdict(r["rain_prob_used"])
        rain_only_correct = rain_only_verdict == r["actual_verdict"]
        full_correct = r["correct"]
        rain_only_rows.append({"correct": rain_only_correct})
        full_rows.append({"correct": full_correct})

        if r["temp_max_used"] >= 34 or r["temp_max_used"] < 17:
            triggered_n += 1
            if full_correct and not rain_only_correct:
                helped_n += 1
            elif rain_only_correct and not full_correct:
                hurt_n += 1

    return {
        "rain_tier_only": overall_accuracy(rain_only_rows),
        "full_rule": overall_accuracy(full_rows),
        "temp_demotion_triggered_n": triggered_n,
        "temp_demotion_helped_n": helped_n,
        "temp_demotion_hurt_n": hurt_n,
    }


def confusion_matrix(rows: list[dict]) -> dict:
    labels = forecast.TIERS
    idx = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for r in rows:
        if not r.get("complete"):
            continue
        matrix[idx[r["actual_verdict"]]][idx[r["predicted_verdict"]]] += 1
    return {"labels": labels, "matrix": matrix}


def weather_forecast_error(rows: list[dict]) -> dict:
    """rain_mm_mae/temp_max_mae over live-weekend (prediction_kind ==
    "forecast") rows with actual weather present, plus bucket counts
    disentangling *why* a wrong live-weekend prediction was wrong:
    retroactive_predicted_verdict(actual weather) is "what the rule would
    have said with perfect weather knowledge" -- comparing that against both
    the original (forecast-based) prediction and reality separates a bad
    Open-Meteo forecast from a bad rule.
    """
    forecast_rows = [r for r in rows if r.get("prediction_kind") == "forecast"]
    rain_diffs, temp_diffs = [], []
    buckets = {"bad_forecast": 0, "bad_rule": 0, "both": 0, "neither": 0}

    for r in forecast_rows:
        if r.get("pred_srazky") is not None and r.get("actual_srazky") is not None:
            rain_diffs.append(abs(r["pred_srazky"] - r["actual_srazky"]))
        if r.get("pred_teplota_max") is not None and r.get("actual_teplota_max") is not None:
            temp_diffs.append(abs(r["pred_teplota_max"] - r["actual_teplota_max"]))

        if r.get("actual_verdict") is None or r.get("actual_srazky") is None or r.get("actual_teplota_max") is None:
            continue
        if r["correct"]:
            buckets["neither"] += 1
            continue
        retro = retroactive_predicted_verdict(r["actual_srazky"], r["actual_teplota_max"])
        fw_wrong = retro != r["predicted_verdict"]
        rule_wrong = retro != r["actual_verdict"]
        if fw_wrong and rule_wrong:
            buckets["both"] += 1
        elif fw_wrong:
            buckets["bad_forecast"] += 1
        elif rule_wrong:
            buckets["bad_rule"] += 1
        else:
            buckets["neither"] += 1  # unreachable given correct is False, kept as a safety net

    return {
        "rain_mm_mae": (sum(rain_diffs) / len(rain_diffs)) if rain_diffs else None,
        "temp_max_mae": (sum(temp_diffs) / len(temp_diffs)) if temp_diffs else None,
        **buckets,
    }


def scatter_points(rows: list[dict], x_key: str, y_key: str) -> list[dict]:
    points = []
    for r in rows:
        x, y = r.get(x_key), r.get(y_key)
        if x is None or y is None:
            continue
        points.append({"x": x, "y": y, "verdict": r.get("actual_verdict"), "is_backfill": r.get("is_backfill")})
    return points


def season_summary_stats(rows: list[dict]) -> dict:
    """Assembles everything above into one JSON-serializable blob -- the only
    object handed to report_charts.py/report_narrative.py/report_html.py.
    """
    enriched = enrich_dataset(rows)
    return {
        "n_days": len(enriched),
        "overall": overall_accuracy(enriched),
        "by_backfill": accuracy_by_split(enriched, "is_backfill"),
        "by_prediction_kind": accuracy_by_split(enriched, "prediction_kind"),
        "by_rule_component": accuracy_by_rule_component(enriched),
        "confusion_matrix": confusion_matrix(enriched),
        "weather_forecast_error": weather_forecast_error(enriched),
        "scatter_points": scatter_points(enriched, "actual_srazky", "actual_teplota_max"),
        "rows": enriched,
    }
