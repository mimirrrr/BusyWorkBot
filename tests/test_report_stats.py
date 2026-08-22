"""Unit tests for src/report_stats.py -- pure functions only, no DB/network/
matplotlib. Flat pytest style, matching tests/test_pure_functions.py.
"""

import datetime as dt

import report_stats


# ---------------------------------------------------------------------------
# estimate_rain_prob_from_mm -- anchor points
# ---------------------------------------------------------------------------

def test_estimate_rain_prob_from_mm_anchor_points():
    assert report_stats.estimate_rain_prob_from_mm(0) == 0
    assert report_stats.estimate_rain_prob_from_mm(1) == 20
    assert report_stats.estimate_rain_prob_from_mm(5) == 50
    assert report_stats.estimate_rain_prob_from_mm(20) == 100


def test_estimate_rain_prob_from_mm_caps_above_20mm():
    assert report_stats.estimate_rain_prob_from_mm(35) == 100


def test_estimate_rain_prob_from_mm_interpolates_between_anchors():
    # halfway between the 1mm->20% and 5mm->50% anchors
    assert report_stats.estimate_rain_prob_from_mm(3) == 35


# ---------------------------------------------------------------------------
# rain_tier_only_verdict -- same boundaries as predict_verdict, no temp demotion
# ---------------------------------------------------------------------------

def test_rain_tier_only_verdict_boundaries():
    assert report_stats.rain_tier_only_verdict(10) == "slammed"
    assert report_stats.rain_tier_only_verdict(11) == "busy"
    assert report_stats.rain_tier_only_verdict(30) == "busy"
    assert report_stats.rain_tier_only_verdict(31) == "normal"
    assert report_stats.rain_tier_only_verdict(50) == "normal"
    assert report_stats.rain_tier_only_verdict(51) == "slow"
    assert report_stats.rain_tier_only_verdict(80) == "slow"
    assert report_stats.rain_tier_only_verdict(81) == "dead"


def test_rain_tier_only_verdict_never_demotes_for_extreme_temp():
    # rain_tier_only_verdict doesn't take temp at all -- nothing to assert
    # beyond "it has no temp parameter"; this documents the boundary case
    # that would demote under the full rule engine but not here.
    assert report_stats.rain_tier_only_verdict(5) == "slammed"


# ---------------------------------------------------------------------------
# enrich_row
# ---------------------------------------------------------------------------

def _base_row(**overrides):
    row = {
        "den": dt.date(2026, 8, 22),
        "actual_visited_id": None, "actual_visited_name": None,
        "source": None, "sold_product": None, "poznamka": None,
        "actual_pocasi_id": None, "actual_weather_label": None,
        "actual_srazky": None, "actual_teplota_min": None, "actual_teplota_max": None,
        "actual_wind_speed": None,
        "predikce_den": None,
        "pred_pocasi_id": None, "pred_weather_label": None,
        "pred_chance_rain": None, "pred_srazky": None,
        "pred_teplota_min": None, "pred_teplota_max": None, "pred_wind_speed": None,
        "pred_visited_name": None,
    }
    row.update(overrides)
    return row


def test_enrich_row_complete_live_weekend_correct_forecast():
    row = _base_row(
        actual_visited_name="hodne", source="live",
        actual_srazky=2.0, actual_teplota_max=24,
        predikce_den=dt.date(2026, 8, 21),
        pred_chance_rain=20, pred_srazky=1.5, pred_teplota_max=25,
        pred_visited_name="hodne",
    )
    out = report_stats.enrich_row(row)
    assert out["den"] == "2026-08-22"
    assert out["predikce_den"] == "2026-08-21"
    assert out["prediction_kind"] == "forecast"
    assert out["predicted_verdict"] == "busy"
    assert out["actual_verdict"] == "busy"
    assert out["correct"] is True
    assert out["complete"] is True
    assert out["is_backfill"] is False


def test_enrich_row_missing_actual_weather_and_prediction():
    # too recent for the archive API, no user_input yet either
    row = _base_row()
    out = report_stats.enrich_row(row)
    assert out["prediction_kind"] is None
    assert out["predicted_verdict"] is None
    assert out["actual_verdict"] is None
    assert out["correct"] is None
    assert out["complete"] is False


def test_enrich_row_missing_prediction_falls_back_to_retroactive():
    # backfill weekend: no weather_prediction row, but weather_actual exists
    row = _base_row(
        actual_visited_name="slabe", source="backfill",
        actual_srazky=6.0, actual_teplota_max=18,
    )
    out = report_stats.enrich_row(row)
    assert out["prediction_kind"] == "retroactive"
    assert out["is_backfill"] is True
    assert out["predicted_verdict"] == report_stats.retroactive_predicted_verdict(6.0, 18)
    assert out["actual_verdict"] == "slow"
    assert out["complete"] is True


def test_enrich_row_missing_actual_but_has_prediction_is_incomplete():
    # live weekend, forecast stored, but too recent for weather_actual/user_input yet
    row = _base_row(
        predikce_den=dt.date(2026, 8, 21),
        pred_chance_rain=10, pred_srazky=0.0, pred_teplota_max=30,
        pred_visited_name="naval",
    )
    out = report_stats.enrich_row(row)
    assert out["prediction_kind"] == "forecast"
    assert out["predicted_verdict"] == "slammed"
    assert out["actual_verdict"] is None
    assert out["correct"] is None
    assert out["complete"] is False


# ---------------------------------------------------------------------------
# overall_accuracy
# ---------------------------------------------------------------------------

def test_overall_accuracy_at_zero_rows():
    assert report_stats.overall_accuracy([]) == {"n": 0, "correct": 0, "accuracy": None}


def test_overall_accuracy_ignores_rows_with_no_verdict_to_compare():
    rows = [{"correct": True}, {"correct": False}, {"correct": None}]
    result = report_stats.overall_accuracy(rows)
    assert result == {"n": 2, "correct": 1, "accuracy": 0.5}


# ---------------------------------------------------------------------------
# confusion_matrix
# ---------------------------------------------------------------------------

def test_confusion_matrix_shape_and_counts():
    rows = [
        {"complete": True, "actual_verdict": "busy", "predicted_verdict": "busy"},
        {"complete": True, "actual_verdict": "busy", "predicted_verdict": "normal"},
        {"complete": False, "actual_verdict": None, "predicted_verdict": None},
    ]
    result = report_stats.confusion_matrix(rows)
    assert result["labels"] == report_stats.forecast.TIERS
    assert len(result["matrix"]) == 5
    assert all(len(row) == 5 for row in result["matrix"])
    busy_idx = result["labels"].index("busy")
    normal_idx = result["labels"].index("normal")
    assert result["matrix"][busy_idx][busy_idx] == 1
    assert result["matrix"][busy_idx][normal_idx] == 1
    assert sum(sum(row) for row in result["matrix"]) == 2  # the incomplete row is excluded


# ---------------------------------------------------------------------------
# accuracy_by_rule_component -- temp demotion helping vs. hurting
# ---------------------------------------------------------------------------

def test_accuracy_by_rule_component_temp_demotion_helped():
    # low rain -> rain-only says "slammed", but it's 40C so the full rule
    # demotes to "busy", which matches what actually happened.
    row = {
        "actual_verdict": "busy", "rain_prob_used": 5, "temp_max_used": 40,
        "predicted_verdict": "busy", "correct": True,
    }
    result = report_stats.accuracy_by_rule_component([row])
    assert result["temp_demotion_triggered_n"] == 1
    assert result["temp_demotion_helped_n"] == 1
    assert result["temp_demotion_hurt_n"] == 0
    assert result["rain_tier_only"]["correct"] == 0
    assert result["full_rule"]["correct"] == 1


def test_accuracy_by_rule_component_temp_demotion_hurt():
    # low rain -> rain-only correctly says "slammed"; the 40C demotion
    # pushes the full rule to "busy", which is wrong this time.
    row = {
        "actual_verdict": "slammed", "rain_prob_used": 5, "temp_max_used": 40,
        "predicted_verdict": "busy", "correct": False,
    }
    result = report_stats.accuracy_by_rule_component([row])
    assert result["temp_demotion_triggered_n"] == 1
    assert result["temp_demotion_helped_n"] == 0
    assert result["temp_demotion_hurt_n"] == 1
    assert result["rain_tier_only"]["correct"] == 1
    assert result["full_rule"]["correct"] == 0


def test_accuracy_by_rule_component_skips_rows_with_no_component_data():
    result = report_stats.accuracy_by_rule_component([{"actual_verdict": None}])
    assert result["temp_demotion_triggered_n"] == 0
    assert result["rain_tier_only"] == {"n": 0, "correct": 0, "accuracy": None}


# ---------------------------------------------------------------------------
# report_charts edge cases (missing split subsets, empty scatter)
# ---------------------------------------------------------------------------

def test_render_accuracy_bar_handles_missing_split_groups():
    import report_charts
    stats = {
        "overall": {"n": 5, "correct": 4, "accuracy": 0.8},
        "by_backfill": {},  # missing "False" and "True" keys
    }
    b64 = report_charts.render_accuracy_bar(stats)
    assert isinstance(b64, str) and len(b64) > 0


def test_render_scatter_handles_empty_points():
    import report_charts
    b64 = report_charts.render_scatter([], "Title", "X", "Y")
    assert isinstance(b64, str) and len(b64) > 0
