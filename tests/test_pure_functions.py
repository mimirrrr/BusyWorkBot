"""Unit tests for pure functions only — no DB connection, no Discord API call,
no network of any kind. Every function tested here depends solely on its
arguments.

Run with: pytest

If a test here fails, the bug is in local logic (rule engine, date math,
formatting). If a live script (forecast.py etc.) fails but everything in
this file passes, the problem is almost certainly environment/DB/network,
not the logic these tests cover.
"""

import datetime as dt

import completeness_sweep
import common
import forecast
import log_message
import report_html


# ---------------------------------------------------------------------------
# forecast.py — rule engine (predict_verdict)
# ---------------------------------------------------------------------------

def test_predict_verdict_rain_tier_boundaries():
    assert forecast.predict_verdict(max_rain_prob=5, temp_max=25) == "slammed"
    assert forecast.predict_verdict(max_rain_prob=10, temp_max=25) == "slammed"
    assert forecast.predict_verdict(max_rain_prob=11, temp_max=25) == "busy"
    assert forecast.predict_verdict(max_rain_prob=30, temp_max=25) == "busy"
    assert forecast.predict_verdict(max_rain_prob=31, temp_max=25) == "normal"
    assert forecast.predict_verdict(max_rain_prob=50, temp_max=25) == "normal"
    assert forecast.predict_verdict(max_rain_prob=51, temp_max=25) == "slow"
    assert forecast.predict_verdict(max_rain_prob=80, temp_max=25) == "slow"
    assert forecast.predict_verdict(max_rain_prob=81, temp_max=25) == "dead"


def test_predict_verdict_extreme_temp_demotes_one_tier():
    # hot (>=34) demotes
    assert forecast.predict_verdict(max_rain_prob=5, temp_max=34) == "busy"
    assert forecast.predict_verdict(max_rain_prob=5, temp_max=40) == "busy"
    # cold (<17) demotes
    assert forecast.predict_verdict(max_rain_prob=5, temp_max=16) == "busy"
    # demotion never pushes past the worst tier
    assert forecast.predict_verdict(max_rain_prob=90, temp_max=40) == "dead"


def test_predict_verdict_comfortable_temp_never_shifts_tier():
    assert forecast.predict_verdict(max_rain_prob=5, temp_max=17) == "slammed"
    assert forecast.predict_verdict(max_rain_prob=5, temp_max=33.9) == "slammed"


# ---------------------------------------------------------------------------
# forecast.py — is_official_run / scheduled_weekday (Thu-vs-Fri branch,
# hardened against a delayed GitHub Actions run landing on the wrong day)
# ---------------------------------------------------------------------------

def test_scheduled_weekday_from_cron_string():
    assert forecast.scheduled_weekday("4 15 * * THU") == 3
    assert forecast.scheduled_weekday("4 15 * * FRI") == 4
    assert forecast.scheduled_weekday("") is None  # workflow_dispatch/local run


def test_is_official_run_trusts_schedule_over_todays_date():
    # Friday's job, delayed past midnight into Saturday -- still official.
    saturday = dt.date(2026, 8, 22) + dt.timedelta(days=1)
    assert forecast.is_official_run("4 15 * * FRI", saturday) is True
    # Thursday's job, delayed into Friday -- still NOT official (it's the
    # early-picture run, even though today happens to look like Friday).
    friday = dt.date(2026, 8, 21)
    assert forecast.is_official_run("4 15 * * THU", friday) is False


def test_is_official_run_falls_back_to_weekday_with_no_schedule():
    friday = dt.date(2026, 8, 21)
    thursday = dt.date(2026, 8, 20)
    assert forecast.is_official_run("", friday) is True
    assert forecast.is_official_run("", thursday) is False


# ---------------------------------------------------------------------------
# forecast.py / log_message.py — weekend date math
# ---------------------------------------------------------------------------

def test_next_weekend_from_a_weekday():
    monday = dt.date(2026, 8, 17)
    assert forecast.next_weekend(monday) == (dt.date(2026, 8, 22), dt.date(2026, 8, 23))


def test_next_weekend_when_today_is_already_saturday():
    saturday = dt.date(2026, 8, 22)
    assert forecast.next_weekend(saturday) == (saturday, dt.date(2026, 8, 23))


def test_last_weekend_on_a_sunday_is_today():
    sunday = dt.date(2026, 8, 23)
    assert forecast.last_weekend(sunday) == (dt.date(2026, 8, 22), sunday)


def test_last_weekend_agrees_between_forecast_and_log_message():
    # Defined independently in both files (log_message.py has its own copy)
    # -- they must keep agreeing, or Monday's message and Friday's recap
    # would silently disagree about which weekend they mean.
    for offset in range(8):
        today = dt.date(2026, 8, 17) + dt.timedelta(days=offset)
        assert forecast.last_weekend(today) == log_message.last_weekend(today)


def test_czech_day_matches_across_all_modules():
    saturday = dt.date(2026, 8, 22)
    assert forecast.czech_day(saturday) == "Sobota"
    assert log_message.czech_day(saturday) == "Sobota"
    assert completeness_sweep.czech_day(saturday) == "Sobota"
    assert report_html.czech_day(saturday) == "Sobota"


# ---------------------------------------------------------------------------
# forecast.py — summarize_day
# ---------------------------------------------------------------------------

def _fake_hourly(day, hours, temps, probs, precip, wind, codes):
    return {
        "time": [f"{day.isoformat()}T{h:02d}:00" for h in hours],
        "temperature_2m": temps,
        "precipitation_probability": probs,
        "precipitation": precip,
        "wind_speed_10m": wind,
        "weather_code": codes,
    }


def test_summarize_day_no_data_in_working_hours():
    day = dt.date(2026, 8, 22)
    hourly = _fake_hourly(day, hours=[3, 4], temps=[10, 11], probs=[0, 0],
                           precip=[0, 0], wind=[5, 5], codes=[0, 0])
    result = forecast.summarize_day(hourly, day, work_start=9, work_end=20)
    assert result["has_data"] is False


def test_summarize_day_aggregates_correctly():
    day = dt.date(2026, 8, 22)
    hourly = _fake_hourly(
        day,
        hours=[9, 10, 11],
        temps=[20, 25, 22],
        probs=[10, 40, 20],
        precip=[0.0, 1.5, 0.0],
        wind=[5, 12, 8],
        codes=[1, 1, 3],  # code 1 appears twice -> dominant
    )
    result = forecast.summarize_day(hourly, day, work_start=9, work_end=20)
    assert result["has_data"] is True
    assert result["teplota_min"] == 20
    assert result["teplota_max"] == 25
    assert result["chance_rain"] == 40
    assert result["srazky"] == 1.5
    assert result["wind_speed"] == 12
    assert result["weather_label"] == "lehce zataženo"
    assert result["verdict"] == forecast.predict_verdict(40, 25)


# ---------------------------------------------------------------------------
# forecast.py — summarize_actual_day (weather_actual, no probability/verdict)
# ---------------------------------------------------------------------------

def _fake_archive_hourly(day, hours, temps, precip, wind, codes):
    return {
        "time": [f"{day.isoformat()}T{h:02d}:00" for h in hours],
        "temperature_2m": temps,
        "precipitation": precip,
        "wind_speed_10m": wind,
        "weather_code": codes,
    }


def test_summarize_actual_day_no_data_in_working_hours():
    day = dt.date(2026, 8, 22)
    hourly = _fake_archive_hourly(day, hours=[3, 4], temps=[10, 11], precip=[0, 0],
                                   wind=[5, 5], codes=[0, 0])
    result = forecast.summarize_actual_day(hourly, day, work_start=9, work_end=20)
    assert result["has_data"] is False


def test_summarize_actual_day_aggregates_correctly():
    day = dt.date(2026, 8, 22)
    hourly = _fake_archive_hourly(
        day,
        hours=[9, 10, 11],
        temps=[20, 25, 22],
        precip=[0.0, 1.5, 0.0],
        wind=[5, 12, 8],
        codes=[1, 1, 3],  # code 1 appears twice -> dominant
    )
    result = forecast.summarize_actual_day(hourly, day, work_start=9, work_end=20)
    assert result["has_data"] is True
    assert result["teplota_min"] == 20
    assert result["teplota_max"] == 25
    assert result["srazky"] == 1.5
    assert result["wind_speed"] == 12
    assert result["weather_label"] == "lehce zataženo"
    # no chance_rain/verdict — neither concept applies to actual weather
    assert "chance_rain" not in result
    assert "verdict" not in result


# ---------------------------------------------------------------------------
# forecast.py — comparison_line (Friday's "last weekend" recap text)
# ---------------------------------------------------------------------------

def test_comparison_line_no_prediction_stored():
    c = {"day": dt.date(2026, 8, 22), "predicted": None, "actual": None}
    assert "nemám predikci" in forecast.comparison_line(c)


def test_comparison_line_predicted_but_not_yet_logged():
    c = {"day": dt.date(2026, 8, 22), "predicted": "busy", "actual": None}
    line = forecast.comparison_line(c)
    assert "Busy" in line
    assert "zatím nezalogováno" in line


def test_comparison_line_match_vs_mismatch():
    match = {"day": dt.date(2026, 8, 22), "predicted": "busy", "actual": "busy"}
    mismatch = {"day": dt.date(2026, 8, 22), "predicted": "busy", "actual": "slow"}
    assert "✅" in forecast.comparison_line(match)
    assert "❌" in forecast.comparison_line(mismatch)



# ---------------------------------------------------------------------------
# completeness_sweep.py — Czech noun declension
# ---------------------------------------------------------------------------

def test_czech_day_count_declension():
    assert completeness_sweep.czech_day_count(0) == "0 dní"
    assert completeness_sweep.czech_day_count(1) == "1 den"
    assert completeness_sweep.czech_day_count(2) == "2 dny"
    assert completeness_sweep.czech_day_count(4) == "4 dny"
    assert completeness_sweep.czech_day_count(5) == "5 dní"


def test_czech_more_days_declension():
    assert completeness_sweep.czech_more_days(1) == "+1 další den"
    assert completeness_sweep.czech_more_days(3) == "+3 další dny"
    assert completeness_sweep.czech_more_days(7) == "+7 dalších dní"


# ---------------------------------------------------------------------------
# Cross-file coupling that CLAUDE.md documents as "kept in sync by hand" --
# these tests turn that manual invariant into something that actually fails
# loudly if the two files drift apart.
# ---------------------------------------------------------------------------

def test_day_block_identical_between_log_message_and_completeness_sweep():
    day = dt.date(2026, 8, 22)
    assert log_message.day_block(day) == completeness_sweep.day_block(day)
    assert common.day_block(day) == log_message.day_block(day)


def test_busyness_keys_and_order_match_between_modules():
    assert [key for key, _, _ in log_message.BUSYNESS] == \
           [key for key, _, _ in completeness_sweep.BUSYNESS] == \
           [key for key, _, _ in common.BUSYNESS]
