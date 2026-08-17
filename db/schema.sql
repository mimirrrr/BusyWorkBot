-- Shift Forecast Bot — Postgres schema (Neon)
--
-- Two lookup tables (číselníky) + two fact tables:
--   weathers            lookup: weather code -> Czech label (src/forecast.py WEATHER_CODES)
--   visited             lookup: busyness scale, shared by BOTH the bot's prediction
--                        and my logged reality (same 6-point scale either way)
--   weather_prediction  fact:  what the bot predicted (written by the Thu/Fri GitHub Action)
--   user_input           fact:  what actually happened (written by the Cloudflare Worker
--                        when I tap a busyness button, via the Monday logging message)
--
-- Run once against the database DATABASE_URL points at.

-- ============================================================
-- weathers — lookup, weather code label
-- ============================================================
CREATE TABLE weathers (
    id_w   INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name_w TEXT NOT NULL UNIQUE
);

-- ============================================================
-- visited — lookup, busyness scale (bot verdict AND my log use this)
-- ============================================================
CREATE TABLE visited (
    id_v   INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name_v TEXT NOT NULL UNIQUE
);

-- ============================================================
-- weather_prediction — what the bot predicted
--
-- `den` is the Saturday/Sunday being forecast; `predikce_den` is the day
-- the job actually ran (Thursday or Friday). Two dates instead of an
-- enum: Thursday and Friday both insert a row for the same `den` without
-- colliding, and the pair stays a plain DATE — no run-label to keep in
-- sync with the cron schedule.
-- ============================================================
CREATE TABLE weather_prediction (
    id_wp                    INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    den                      DATE NOT NULL,
    predikce_den             DATE NOT NULL,
    pocasi_id                INT NOT NULL REFERENCES weathers(id_w),
    chance_rain              INT NOT NULL CHECK (chance_rain BETWEEN 0 AND 100),
    srazky                   NUMERIC(4,1) NOT NULL,   -- mm; decimal so light rain isn't rounded to 0
    teplota_min              INT NOT NULL,
    teplota_max              INT NOT NULL,
    wind_speed               INT NOT NULL,
    predikce_navstevnost_id  INT NOT NULL REFERENCES visited(id_v),  -- the bot's verdict (rule engine output, phase 3)
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (den, predikce_den)
);

-- ============================================================
-- user_input — what actually happened, logged by me via Discord buttons
--
-- One row per real day; clicking a button again is an upsert on `den`
-- (misclick recovery per the plan).
-- ============================================================
CREATE TABLE user_input (
    id_up        INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    den          DATE NOT NULL UNIQUE,
    visited_id   INT NOT NULL REFERENCES visited(id_v),
    sold_product INT,                     -- units sold that day; entered via modal, optional
    poznamka     TEXT,                    -- free-text note from the modal, optional
    source       TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live', 'backfill')),
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- season_config — singleton row: this year's work-season window.
--
-- Every scheduled script (forecast, Monday log, completeness sweep) checks
-- this first and quietly no-ops outside [season_start, season_end]. If this
-- table is empty (never configured yet), scripts fail OPEN — run as if
-- always in season — so nothing silently breaks before it's ever been set.
-- Set/updated via a Discord modal (src/season_reminder.py + the Worker's
-- season_modal handler), not edited by hand.
-- ============================================================
CREATE TABLE season_config (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    season_start DATE NOT NULL,
    season_end   DATE NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Seed data
-- ============================================================
INSERT INTO visited (name_v) VALUES
    ('velmi slabe'), ('slabe'), ('stredni'), ('hodne'), ('naval');

-- Collapses the WMO codes in WEATHER_CODES (src/forecast.py) down to their
-- distinct Czech labels (several codes share one label, e.g. 51/53/55 -> mrholení).
INSERT INTO weathers (name_w) VALUES
    ('slunečno'), ('lehce zataženo'), ('oblačno'), ('zataženo'),
    ('mlha'), ('mrholení'), ('mírný déšť'), ('déšť'), ('silný déšť'),
    ('ledový déšť'), ('sníh'), ('přeháňky'), ('silné přeháňky'),
    ('sněhové přeháňky'), ('bouřky');
