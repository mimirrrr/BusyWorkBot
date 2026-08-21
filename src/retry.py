"""Shared retry-with-backoff helpers for transient failures.

Every script in src/ is a one-shot GitHub Actions cron job that runs for a
few seconds and exits — a single dropped packet, a Discord 503, or a Neon
cold-start connection blip otherwise kills the whole run with nothing
retried and (aside from one spot in completeness_sweep.py) no handling at
all. These retry only conditions that are actually transient (timeouts,
connection errors, 429, 5xx); a real 4xx (bad token, bad payload) fails
immediately since retrying it would never help.
"""

import time

import psycopg
import requests

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def request_with_retry(method: str, url: str, *, attempts: int = 3,
                        backoff: float = 2.0, **kwargs) -> requests.Response:
    """Like requests.request(...), but retries transient failures with linear
    backoff (backoff * attempt seconds between tries) before giving up.
    Raises the last error once attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
        else:
            if r.status_code not in RETRYABLE_STATUS:
                r.raise_for_status()
                return r
            last_exc = requests.HTTPError(f"{r.status_code} {r.reason} for url: {url}", response=r)
        if attempt < attempts:
            time.sleep(backoff * attempt)
    raise last_exc


def connect_with_retry(database_url: str, *, attempts: int = 3,
                        backoff: float = 2.0, **kwargs) -> psycopg.Connection:
    """Like psycopg.connect(...), but retries transient connection failures
    (e.g. a Neon free-tier cold start) with linear backoff.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(database_url, **kwargs)
        except psycopg.OperationalError as e:
            last_exc = e
            if attempt < attempts:
                time.sleep(backoff * attempt)
    raise last_exc
