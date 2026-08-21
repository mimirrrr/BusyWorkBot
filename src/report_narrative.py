"""Phase 5: AI narrative for the end-of-season report.

Code computes, AI narrates (see CLAUDE.md/docs/PLAN.md) -- this module never
calculates anything. It receives report_stats.py's precomputed
season_summary_stats() blob and asks Gemini to write a short summary of what
it already says. If the call fails for any reason (bad key, deprecated
model, network), call_gemini returns a canned fallback string instead of
blocking the whole report -- a narrative paragraph is a nice-to-have, not
something worth losing the report over.

Model id: gemini-3.5-flash, per explicit instruction -- not the newest flash
model available as of implementation (August 2026), by choice. GEMINI_MODEL
still overrides it at runtime, so switching later is a one-line env change,
not a code change.
"""

import json

from retry import request_with_retry

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.5-flash"

FALLBACK_NARRATIVE = (
    "Automatické shrnutí sezóny se nepodařilo vygenerovat (AI služba nebyla "
    "dostupná). Čísla a grafy výše jsou kompletní a spočítaná deterministicky "
    "-- chybí jen textový komentář."
)


def build_prompt(stats: dict) -> str:
    return (
        "Jsi asistent, který shrnuje výsledky sezónního predikčního systému "
        "pro venkovní gastro provoz (predikce návštěvnosti podle počasí). "
        "Níže je JSON se VŠEMI předpočítanými statistikami za sezónu -- "
        "přesnost predikce, matice záměn, rozdělení live/backfill dat, "
        "chyby v předpovědi počasí atd. "
        "DŮLEŽITÉ: nic nepočítej ani nedopočítávej -- žádné procento, "
        "průměr ani počet, který v datech níže není. Pouze slovně shrň, co "
        "data říkají, v češtině, ve 3-5 větách. Zmiň, pokud je vzorek dat "
        "malý (nízké n) nebo pokud jsou backfill data (zpětně doplněná z "
        "paměti) méně spolehlivá než live data. Buď věcný, ne nadšený.\n\n"
        f"Data:\n{json.dumps(stats, ensure_ascii=False)}"
    )


def call_gemini(api_key: str, model: str, prompt: str) -> str:
    try:
        r = request_with_retry(
            "POST", f"{GEMINI_API}/{model}:generateContent",
            headers={"X-goog-api-key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3},
            },
            timeout=60,
        )
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"season_report: Gemini narrative call failed, using fallback text: {e}")
        return FALLBACK_NARRATIVE
