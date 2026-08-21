"""Unit tests for src/retry.py's transient-failure retry logic.

Mocked network/DB only (no real requests, no real Postgres) — this is
separate from test_pure_functions.py, which is reserved for functions with
zero I/O of any kind, mocked or otherwise.
"""

from unittest.mock import patch, MagicMock

import psycopg
import requests

import retry


def _response(status_code: int) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.reason = "error"
    if status_code < 400:
        r.raise_for_status.return_value = None
    else:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status_code} error", response=r)
    return r


@patch("retry.time.sleep")
@patch("retry.requests.request")
def test_succeeds_first_try_no_sleep(mock_request, mock_sleep):
    mock_request.return_value = _response(200)
    r = retry.request_with_retry("GET", "https://example.invalid", attempts=3)
    assert r.status_code == 200
    mock_sleep.assert_not_called()


@patch("retry.time.sleep")
@patch("retry.requests.request")
def test_retries_on_500_then_succeeds(mock_request, mock_sleep):
    mock_request.side_effect = [_response(503), _response(200)]
    r = retry.request_with_retry("GET", "https://example.invalid", attempts=3)
    assert r.status_code == 200
    assert mock_request.call_count == 2
    mock_sleep.assert_called_once()


@patch("retry.time.sleep")
@patch("retry.requests.request")
def test_gives_up_after_exhausting_attempts(mock_request, mock_sleep):
    mock_request.return_value = _response(503)
    try:
        retry.request_with_retry("GET", "https://example.invalid", attempts=3)
        assert False, "expected HTTPError"
    except requests.HTTPError:
        pass
    assert mock_request.call_count == 3
    assert mock_sleep.call_count == 2


@patch("retry.time.sleep")
@patch("retry.requests.request")
def test_does_not_retry_on_client_error(mock_request, mock_sleep):
    mock_request.return_value = _response(404)
    try:
        retry.request_with_retry("GET", "https://example.invalid", attempts=3)
        assert False, "expected HTTPError"
    except requests.HTTPError:
        pass
    assert mock_request.call_count == 1
    mock_sleep.assert_not_called()


@patch("retry.time.sleep")
@patch("retry.requests.request")
def test_retries_on_connection_error(mock_request, mock_sleep):
    mock_request.side_effect = [requests.ConnectionError("boom"), _response(200)]
    r = retry.request_with_retry("GET", "https://example.invalid", attempts=3)
    assert r.status_code == 200
    mock_sleep.assert_called_once()


@patch("retry.time.sleep")
@patch("retry.psycopg.connect")
def test_connect_retries_on_operational_error_then_succeeds(mock_connect, mock_sleep):
    mock_connect.side_effect = [psycopg.OperationalError("cold start"), MagicMock()]
    retry.connect_with_retry("postgres://fake", attempts=3, connect_timeout=10)
    assert mock_connect.call_count == 2
    mock_sleep.assert_called_once()


@patch("retry.time.sleep")
@patch("retry.psycopg.connect")
def test_connect_gives_up_after_exhausting_attempts(mock_connect, mock_sleep):
    mock_connect.side_effect = psycopg.OperationalError("down")
    try:
        retry.connect_with_retry("postgres://fake", attempts=3, connect_timeout=10)
        assert False, "expected OperationalError"
    except psycopg.OperationalError:
        pass
    assert mock_connect.call_count == 3
