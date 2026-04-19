"""
seed_database.py — One-time database setup + seeder for Fitrah AI.

Run this ONCE before starting the server:
  python seed_database.py

What it does:
  1. Creates all Fitrah tables in the existing barakah_db PostgreSQL database.
  2. Seeds master_actions, system_configs (nafs levels, dimensions) from JSON files.

Safe to re-run — all DDL uses IF NOT EXISTS and all INSERTs use ON CONFLICT DO UPDATE.
"""

import json
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "dbname":   "barakah_db",
    "user":     "postgres",
    "password": os.getenv("DB_PASSWORD", "barakah_secret_2026"),
    "host":     "localhost",
    "port":     "5433",
}

DATA_DIR = os.path.join(os.path.dirname(__file__), "fitrah_engine", "data")


def load_json(filename: str):
    with open(os.path.join(DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


# ── DDL ───────────────────────────────────────────────────────────────────────

CREATE_TABLES_SQL = """
-- ============================================================
-- FITRAH AI — USER STATE TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS fitrah_users (
    user_id                 TEXT        PRIMARY KEY,        -- Supabase auth UUID
    archetype_key           TEXT,
    life_stage              TEXT,
    ummah_role              TEXT,
    jalali_jamali           TEXT,
    introvert_extrovert     TEXT,
    current_nafs_level      TEXT        NOT NULL DEFAULT 'nafs_e_ammarah',
    crystal_score           REAL        NOT NULL DEFAULT 0,
    streak_current          INTEGER     NOT NULL DEFAULT 0,
    streak_max              INTEGER     NOT NULL DEFAULT 0,
    tawbah_streak_current   INTEGER     NOT NULL DEFAULT 0,
    profiler_completed_at   TIMESTAMPTZ,
    last_active_at          TIMESTAMPTZ DEFAULT now(),
    created_at              TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fitrah_user_dimensions (
    user_id       TEXT  PRIMARY KEY REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    taqwa_score   REAL  NOT NULL DEFAULT 5,
    ilm_score     REAL  NOT NULL DEFAULT 5,
    tazkiya_score REAL  NOT NULL DEFAULT 5,
    ihsan_score   REAL  NOT NULL DEFAULT 5,
    nafs_score    REAL  NOT NULL DEFAULT 5,
    maal_score    REAL  NOT NULL DEFAULT 5,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fitrah_user_action_logs (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    action_key          TEXT        NOT NULL,
    points_primary      INTEGER     NOT NULL DEFAULT 0,
    dimension_primary   TEXT        NOT NULL,
    points_secondary    INTEGER,
    dimension_secondary TEXT,
    logged_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- FITRAH AI — MASTER / CONFIG TABLES  (seeded from JSON)
-- ============================================================

CREATE TABLE IF NOT EXISTS fitrah_master_actions (
    action_key  TEXT PRIMARY KEY,
    data        JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS fitrah_system_configs (
    config_key  TEXT PRIMARY KEY,
    data        JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_fitrah_action_logs_user_date
    ON fitrah_user_action_logs (user_id, action_key, logged_at);

CREATE INDEX IF NOT EXISTS idx_fitrah_action_logs_logged_at
    ON fitrah_user_action_logs (logged_at);

CREATE INDEX IF NOT EXISTS idx_fitrah_users_last_active
    ON fitrah_users (last_active_at);

-- ============================================================
-- FITRAH OS — EXTENDED SYSTEMS
-- ============================================================

-- Qalb State — daily heart check-in log
CREATE TABLE IF NOT EXISTS fitrah_qalb_logs (
    id          SERIAL      PRIMARY KEY,
    user_id     TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    qalb_state  TEXT        NOT NULL,   -- content/hopeful/anxious/broken/hardened/confused/grateful
    notes       TEXT,                   -- optional free-text from user
    logged_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_qalb_logs_user_date
    ON fitrah_qalb_logs (user_id, logged_at DESC);

-- Barakah Time sessions — task + niyyah + focus tracking
CREATE TABLE IF NOT EXISTS fitrah_barakah_sessions (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    task_description    TEXT,
    niyyah_confirmed    BOOLEAN     NOT NULL DEFAULT FALSE,
    focus_level         INTEGER,        -- 1-5
    distraction_level   INTEGER,        -- 1-5 (lower = less distracted)
    spiritual_state     TEXT,           -- state at time of session
    dimension_key       TEXT,           -- dimension to award points to
    barakah_score       REAL,           -- 0-100
    points_awarded      INTEGER,        -- 4/6/8
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_barakah_sessions_user
    ON fitrah_barakah_sessions (user_id, started_at DESC);

-- Battlefield sessions — Nafs Battlefield Visualizer logs
CREATE TABLE IF NOT EXISTS fitrah_battlefield_sessions (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    forces          JSONB,              -- {nafs, aql, qalb, shaytan} levels + labels
    intervention    JSONB,              -- {ayah, hadith, micro_action}
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- FITRAH OS — DB PATCH v2 (from fitrah db patch.sql)
-- ============================================================

-- Dua Thread — personal dua tracking
CREATE TABLE IF NOT EXISTS fitrah_dua_thread (
    id          SERIAL      PRIMARY KEY,
    user_id     TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    dua_text    TEXT        NOT NULL,
    context     TEXT,
    status      TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','answered','closed_gracefully')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at TIMESTAMPTZ,
    closed_at   TIMESTAMPTZ,
    answer_note TEXT,
    is_private  BOOLEAN     NOT NULL DEFAULT TRUE,
    fiqh_context TEXT,                              -- optional fiqh school context for the dua
    deleted_at  TIMESTAMPTZ,                        -- soft delete for privacy compliance
    -- Integrity: answered_at must be set when answered
    CONSTRAINT chk_dua_answered CHECK (
        (status = 'answered' AND answered_at IS NOT NULL) OR status != 'answered'
    ),
    -- Integrity: closed_at must be set when closed_gracefully
    CONSTRAINT chk_dua_closed CHECK (
        (status = 'closed_gracefully' AND closed_at IS NOT NULL) OR status != 'closed_gracefully'
    )
);

-- NOTE: partial indexes on deleted_at created at end of MIGRATE_SQL
--       (after deleted_at column is guaranteed to exist on existing DBs)

-- Nafs Level History — every level transition logged for Mufti Board audit
CREATE TABLE IF NOT EXISTS fitrah_nafs_level_history (
    id                          SERIAL      PRIMARY KEY,
    user_id                     TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    from_level                  TEXT,
    to_level                    TEXT        NOT NULL,
    transition_type             TEXT        CHECK (transition_type IN ('promotion','regression')),
    -- Score snapshot at transition time (full picture for audit)
    crystal_score_at_time       REAL,
    taqwa_at_transition         REAL,
    ilm_at_transition           REAL,
    tazkiya_at_transition       REAL,
    ihsan_at_transition         REAL,
    nafs_score_at_transition    REAL,
    maal_at_transition          REAL,
    days_at_previous_level      INTEGER,
    -- Audit compliance flags
    time_gate_met               BOOLEAN     NOT NULL DEFAULT TRUE,
    disclaimer_shown            BOOLEAN     NOT NULL DEFAULT FALSE,
    mufti_review_required       BOOLEAN     NOT NULL DEFAULT FALSE,
    mufti_review_status         TEXT        DEFAULT 'not_required',
    mufti_review_notes          TEXT,
    transitioned_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_nafs_history_user
    ON fitrah_nafs_level_history (user_id, transitioned_at DESC);

CREATE INDEX IF NOT EXISTS idx_nafs_history_mufti
    ON fitrah_nafs_level_history (mufti_review_required)
    WHERE mufti_review_required = TRUE;

-- Weekly Ihtisab — stored weekly summary for user review (generated by cron)
CREATE TABLE IF NOT EXISTS fitrah_weekly_ihtisab (
    id                          SERIAL      PRIMARY KEY,
    user_id                     TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    week_ending_date            DATE        NOT NULL,
    week_number                 INTEGER,
    -- 5-bucket narrative summaries
    ibadat_summary              TEXT,
    ilm_summary                 TEXT,
    akhlaq_summary              TEXT,
    khidmat_summary             TEXT,
    nafs_summary                TEXT,
    -- Action counts per bucket
    total_actions_count         INTEGER     NOT NULL DEFAULT 0,
    ibadat_actions_count        INTEGER     NOT NULL DEFAULT 0,
    ilm_actions_count           INTEGER     NOT NULL DEFAULT 0,
    akhlaq_actions_count        INTEGER     NOT NULL DEFAULT 0,
    khidmat_actions_count       INTEGER     NOT NULL DEFAULT 0,
    nafs_actions_count          INTEGER     NOT NULL DEFAULT 0,
    -- Crystal & dimension changes this week
    crystal_start               REAL,
    crystal_end                 REAL,
    crystal_change              REAL,
    taqwa_change                REAL,
    ilm_change                  REAL,
    tazkiya_change              REAL,
    ihsan_change                REAL,
    nafs_change                 REAL,
    maal_change                 REAL,
    -- Barakah metrics
    highest_barakah_day         DATE,
    highest_barakah_score       REAL,
    avg_barakah_score           REAL,
    -- Sunnah DNA snapshot
    sunnah_dna_snapshot         JSONB,
    -- Qalb state summary
    qalb_state_modes            JSONB,
    qalb_state_mode             TEXT,
    -- Purpose drift
    drift_status                TEXT,
    drift_observation           TEXT,
    drift_suggested_action      TEXT,
    -- AI narrative
    overall_narrative           TEXT,
    suggested_focus             TEXT,
    -- Vs previous week comparison
    vs_previous_trend           TEXT,
    vs_previous_note            TEXT,
    -- User review tracking
    user_reviewed               BOOLEAN     NOT NULL DEFAULT FALSE,
    user_reviewed_at            TIMESTAMPTZ,
    generated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, week_ending_date)
);

CREATE INDEX IF NOT EXISTS idx_ihtisab_user_week
    ON fitrah_weekly_ihtisab (user_id, week_ending_date DESC);

CREATE INDEX IF NOT EXISTS idx_ihtisab_unreviewed
    ON fitrah_weekly_ihtisab (user_id, user_reviewed)
    WHERE user_reviewed = FALSE;

-- Qalb State History — one entry per user per day (upsert on date)
CREATE TABLE IF NOT EXISTS fitrah_qalb_state_history (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    qalb_state      TEXT        NOT NULL
                                CHECK (qalb_state IN ('hard_heart','soft_heart','distracted','ghafil','present','broken','hopeful')),
    emotional_state TEXT
                                CHECK (emotional_state IN ('calm','anxious','happy','sad','angry','grateful','disconnected')),
    logged_date     DATE        NOT NULL,
    context_note    TEXT,
    line_id_used    TEXT,       -- opening line shown (for rotation tracking)
    UNIQUE (user_id, logged_date)
);

CREATE INDEX IF NOT EXISTS idx_qalb_history_user_date
    ON fitrah_qalb_state_history (user_id, logged_date DESC);

-- ============================================================
-- FITRAH OS v3 — NEW SYSTEM TABLES
-- ============================================================

-- Pending Nafs Promotions — two-step confirmation flow
-- log_action sets this; /nafs/confirm-promotion applies it
CREATE TABLE IF NOT EXISTS fitrah_pending_promotions (
    user_id         TEXT        PRIMARY KEY REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    from_level      TEXT        NOT NULL,
    to_level        TEXT        NOT NULL,
    crystal_at_time REAL        NOT NULL,
    taqwa_at_time   REAL,
    gate_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days'
);

-- Sunnah DNA Phase History — tracks derivation phase evolution over time
-- Phase 1 = profiler only, Phase 2 = blended at day 14, Phase 3 = mature at day 60
CREATE TABLE IF NOT EXISTS fitrah_sunnah_dna_history (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    phase           INTEGER     NOT NULL DEFAULT 1,
    ibadah_score    REAL        DEFAULT 0,
    eating_score    REAL        DEFAULT 0,
    sleeping_score  REAL        DEFAULT 0,
    social_score    REAL        DEFAULT 0,
    derived_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sunnah_dna_history_user
    ON fitrah_sunnah_dna_history (user_id, derived_at DESC);

-- Purpose Drift Log — weekly snapshots of dimension action distribution vs expected
CREATE TABLE IF NOT EXISTS fitrah_purpose_drift_log (
    id                      SERIAL      PRIMARY KEY,
    user_id                 TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    week_ending_date        DATE        NOT NULL,
    ummah_role              TEXT,
    actual_distribution     JSONB,      -- {taqwa: 30, ilm: 10, ...} as percentage
    expected_distribution   JSONB,      -- expected for this ummah_role
    drift_detected          BOOLEAN     NOT NULL DEFAULT FALSE,
    drift_dimensions        TEXT[],     -- which dimensions show > 30% delta
    checked_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, week_ending_date)
);

CREATE INDEX IF NOT EXISTS idx_purpose_drift_user
    ON fitrah_purpose_drift_log (user_id, week_ending_date DESC);

-- Ruhani Fatigue Log — tracks fatigue episodes
-- Triggered when TAQWA + TAZKIYA both < 40 for 5+ consecutive days
CREATE TABLE IF NOT EXISTS fitrah_ruhani_fatigue_log (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL REFERENCES fitrah_users(user_id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    peak_severity   INTEGER     DEFAULT 1,  -- 1=mild (5-7d), 2=moderate (8-14d), 3=severe (15d+)
    taqwa_avg       REAL,
    tazkiya_avg     REAL,
    resolved        BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_ruhani_fatigue_user
    ON fitrah_ruhani_fatigue_log (user_id, started_at DESC);
"""


MIGRATE_SQL = """
-- Safe migrations — add new columns only if they don't exist yet
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='streak_current') THEN
        ALTER TABLE fitrah_users ADD COLUMN streak_current INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='streak_max') THEN
        ALTER TABLE fitrah_users ADD COLUMN streak_max INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='tawbah_streak_current') THEN
        ALTER TABLE fitrah_users ADD COLUMN tawbah_streak_current INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='profiler_completed_at') THEN
        ALTER TABLE fitrah_users ADD COLUMN profiler_completed_at TIMESTAMPTZ;
    END IF;
    -- Fitrah OS extended columns
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='spiritual_state') THEN
        ALTER TABLE fitrah_users ADD COLUMN spiritual_state TEXT NOT NULL DEFAULT 'seeking';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='sunnah_dna') THEN
        ALTER TABLE fitrah_users ADD COLUMN sunnah_dna JSONB;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='last_qalb_state') THEN
        ALTER TABLE fitrah_users ADD COLUMN last_qalb_state TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='crystal_prev') THEN
        ALTER TABLE fitrah_users ADD COLUMN crystal_prev REAL;
    END IF;
    -- DB Patch v2 columns
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='sunnah_dna_eating') THEN
        ALTER TABLE fitrah_users ADD COLUMN sunnah_dna_eating REAL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='sunnah_dna_sleeping') THEN
        ALTER TABLE fitrah_users ADD COLUMN sunnah_dna_sleeping REAL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='sunnah_dna_social') THEN
        ALTER TABLE fitrah_users ADD COLUMN sunnah_dna_social REAL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='sunnah_dna_ibadah') THEN
        ALTER TABLE fitrah_users ADD COLUMN sunnah_dna_ibadah REAL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='barakah_score_today') THEN
        ALTER TABLE fitrah_users ADD COLUMN barakah_score_today REAL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='barakah_score_weekly_avg') THEN
        ALTER TABLE fitrah_users ADD COLUMN barakah_score_weekly_avg REAL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='spiritual_resilience_score') THEN
        ALTER TABLE fitrah_users ADD COLUMN spiritual_resilience_score REAL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='purpose_drift_days') THEN
        ALTER TABLE fitrah_users ADD COLUMN purpose_drift_days INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='last_qalb_state_logged') THEN
        ALTER TABLE fitrah_users ADD COLUMN last_qalb_state_logged DATE;
    END IF;
    -- Fix 2: Nafs level time-gate tracking
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='nafs_level_since') THEN
        ALTER TABLE fitrah_users ADD COLUMN nafs_level_since DATE;
    END IF;
    -- Fix 3: Spiritual state confirmation tracking
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='spiritual_state_confirmed_at') THEN
        ALTER TABLE fitrah_users ADD COLUMN spiritual_state_confirmed_at TIMESTAMPTZ;
    END IF;
    -- PDF §19 missing profile columns
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='fiqh_school') THEN
        ALTER TABLE fitrah_users ADD COLUMN fiqh_school TEXT NOT NULL DEFAULT 'hanafi';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='detailed_view_enabled') THEN
        ALTER TABLE fitrah_users ADD COLUMN detailed_view_enabled BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='primary_sahaba') THEN
        ALTER TABLE fitrah_users ADD COLUMN primary_sahaba TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='maqsad_fitrah_identity') THEN
        ALTER TABLE fitrah_users ADD COLUMN maqsad_fitrah_identity TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='maqsad_life_mission') THEN
        ALTER TABLE fitrah_users ADD COLUMN maqsad_life_mission TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='maqsad_ummah_role') THEN
        ALTER TABLE fitrah_users ADD COLUMN maqsad_ummah_role TEXT;
    END IF;
    -- Spiritual resilience date tracking
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='last_relapse_date') THEN
        ALTER TABLE fitrah_users ADD COLUMN last_relapse_date DATE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='last_return_date') THEN
        ALTER TABLE fitrah_users ADD COLUMN last_return_date DATE;
    END IF;
    -- Purpose drift tracking
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='purpose_drift_weeks') THEN
        ALTER TABLE fitrah_users ADD COLUMN purpose_drift_weeks INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='last_drift_check') THEN
        ALTER TABLE fitrah_users ADD COLUMN last_drift_check DATE;
    END IF;
    -- v3 alignment: spiritual state suggestion tracking
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='spiritual_state_confirmed') THEN
        ALTER TABLE fitrah_users ADD COLUMN spiritual_state_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='spiritual_state_suggested') THEN
        ALTER TABLE fitrah_users ADD COLUMN spiritual_state_suggested TEXT;
    END IF;
    -- v3 alignment: tone preference
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='tone_preference') THEN
        ALTER TABLE fitrah_users ADD COLUMN tone_preference TEXT NOT NULL DEFAULT 'urdu_english_mix';
    END IF;
    -- v3 alignment: secondary Sahaba matches
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='secondary_sahaba_1') THEN
        ALTER TABLE fitrah_users ADD COLUMN secondary_sahaba_1 TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='secondary_sahaba_2') THEN
        ALTER TABLE fitrah_users ADD COLUMN secondary_sahaba_2 TEXT;
    END IF;
    -- v3 alignment: riya detection (detailed view frequency tracking)
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='detailed_view_last_checked') THEN
        ALTER TABLE fitrah_users ADD COLUMN detailed_view_last_checked TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='detailed_view_check_streak') THEN
        ALTER TABLE fitrah_users ADD COLUMN detailed_view_check_streak INTEGER NOT NULL DEFAULT 0;
    END IF;
    -- v3 alignment: rotating qalb opening line (prevents repeat)
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='last_qalb_line_id') THEN
        ALTER TABLE fitrah_users ADD COLUMN last_qalb_line_id TEXT;
    END IF;
    -- v3 alignment: total spiritual resilience returns
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='total_returns') THEN
        ALTER TABLE fitrah_users ADD COLUMN total_returns INTEGER NOT NULL DEFAULT 0;
    END IF;
    -- v3 alignment: nafs_dua and soft-delete on dua thread
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_dua_thread' AND column_name='fiqh_context') THEN
        ALTER TABLE fitrah_dua_thread ADD COLUMN fiqh_context TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_dua_thread' AND column_name='deleted_at') THEN
        ALTER TABLE fitrah_dua_thread ADD COLUMN deleted_at TIMESTAMPTZ;
    END IF;
    -- v3 alignment: nafs level history enrichment columns
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_nafs_level_history' AND column_name='taqwa_at_transition') THEN
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN taqwa_at_transition REAL;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN ilm_at_transition REAL;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN tazkiya_at_transition REAL;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN ihsan_at_transition REAL;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN nafs_score_at_transition REAL;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN maal_at_transition REAL;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN time_gate_met BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN disclaimer_shown BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN mufti_review_required BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN mufti_review_status TEXT DEFAULT 'not_required';
        ALTER TABLE fitrah_nafs_level_history ADD COLUMN mufti_review_notes TEXT;
    END IF;
    -- v3 alignment: weekly ihtisab enrichment
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_weekly_ihtisab' AND column_name='week_number') THEN
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN week_number INTEGER;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN total_actions_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN ibadat_actions_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN ilm_actions_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN akhlaq_actions_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN khidmat_actions_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN nafs_actions_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN crystal_start REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN crystal_end REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN crystal_change REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN taqwa_change REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN ilm_change REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN tazkiya_change REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN ihsan_change REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN nafs_change REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN maal_change REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN highest_barakah_score REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN avg_barakah_score REAL;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN qalb_state_mode TEXT;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN drift_observation TEXT;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN drift_suggested_action TEXT;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN vs_previous_trend TEXT;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN vs_previous_note TEXT;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN user_reviewed BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE fitrah_weekly_ihtisab ADD COLUMN user_reviewed_at TIMESTAMPTZ;
    END IF;
END $$;

-- Fix 4: Dua status — rename replaced_by_better → closed_gracefully
UPDATE fitrah_dua_thread SET status = 'closed_gracefully' WHERE status = 'replaced_by_better';
ALTER TABLE fitrah_dua_thread DROP CONSTRAINT IF EXISTS fitrah_dua_thread_status_check;
ALTER TABLE fitrah_dua_thread ADD CONSTRAINT fitrah_dua_thread_status_check
    CHECK (status IN ('pending','answered','closed_gracefully'));

-- Fix 7: Add closed_at column to fitrah_dua_thread
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_dua_thread' AND column_name='closed_at') THEN
        ALTER TABLE fitrah_dua_thread ADD COLUMN closed_at TIMESTAMPTZ;
    END IF;
END $$;

-- v3 alignment: two-step nafs promotion + new state tracking columns
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='pending_nafs_level') THEN
        ALTER TABLE fitrah_users ADD COLUMN pending_nafs_level TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='ruhani_fatigue_active') THEN
        ALTER TABLE fitrah_users ADD COLUMN ruhani_fatigue_active BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='sunnah_dna_phase') THEN
        ALTER TABLE fitrah_users ADD COLUMN sunnah_dna_phase INTEGER NOT NULL DEFAULT 1;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='consecutive_ghafil_days') THEN
        ALTER TABLE fitrah_users ADD COLUMN consecutive_ghafil_days INTEGER NOT NULL DEFAULT 0;
    END IF;
    -- Qalb history: opening line tracking for rotation
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_qalb_state_history' AND column_name='line_id_used') THEN
        ALTER TABLE fitrah_qalb_state_history ADD COLUMN line_id_used TEXT;
    END IF;
    -- Qalb State Pattern cron: flag users with 3+ day gap in qalb logging
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='qalb_gap_flagged') THEN
        ALTER TABLE fitrah_users ADD COLUMN qalb_gap_flagged BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    -- Dua Thread Reminder cron: count of pending duas awaiting reflection
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='dua_reminder_count') THEN
        ALTER TABLE fitrah_users ADD COLUMN dua_reminder_count INTEGER NOT NULL DEFAULT 0;
    END IF;
    -- Relationship Pulse cron: consecutive days without any IHSAN action
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='relationship_neglect_days') THEN
        ALTER TABLE fitrah_users ADD COLUMN relationship_neglect_days INTEGER NOT NULL DEFAULT 0;
    END IF;
    -- PDF §10: user acknowledged drift — pause drift detection until this date
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='drift_pause_until') THEN
        ALTER TABLE fitrah_users ADD COLUMN drift_pause_until DATE;
    END IF;
    -- PDF §15: user muted Quranic Mirror push; respect user autonomy
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='fitrah_users' AND column_name='quranic_mirror_muted') THEN
        ALTER TABLE fitrah_users ADD COLUMN quranic_mirror_muted BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
END $$;

-- Fix current_nafs_level DEFAULT to use new prefixed key format
ALTER TABLE fitrah_users ALTER COLUMN current_nafs_level SET DEFAULT 'nafs_e_ammarah';

-- v3 level key migration: rename short keys ("ammarah") to prefixed keys ("nafs_e_ammarah")
UPDATE fitrah_users
   SET current_nafs_level = 'nafs_e_' || current_nafs_level
 WHERE current_nafs_level IS NOT NULL
   AND current_nafs_level NOT LIKE 'nafs_e_%';

UPDATE fitrah_users
   SET pending_nafs_level = 'nafs_e_' || pending_nafs_level
 WHERE pending_nafs_level IS NOT NULL
   AND pending_nafs_level NOT LIKE 'nafs_e_%';

UPDATE fitrah_nafs_level_history
   SET from_level = 'nafs_e_' || from_level
 WHERE from_level IS NOT NULL
   AND from_level NOT LIKE 'nafs_e_%';

UPDATE fitrah_nafs_level_history
   SET to_level = 'nafs_e_' || to_level
 WHERE to_level IS NOT NULL
   AND to_level NOT LIKE 'nafs_e_%';

-- Partial indexes on deleted_at — created here so deleted_at column is guaranteed present
-- (on existing DBs the column was just added above; on fresh installs it's in the CREATE TABLE)
CREATE INDEX IF NOT EXISTS idx_dua_thread_user
    ON fitrah_dua_thread (user_id, status, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_dua_pending_old
    ON fitrah_dua_thread (user_id, created_at)
    WHERE deleted_at IS NULL AND status = 'pending';
"""


# ============================================================
# TAWBAH OS — DDL (additive; zero impact on fitrah_* or pgvector)
# ============================================================

TAWBAH_TABLES_SQL = """
-- ============================================================
-- TAWBAH OS — MASTER CONFIG STORAGE  (seeded from JSON)
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_system_configs (
    config_key  TEXT        PRIMARY KEY,
    data        JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- TAWBAH OS — USER PROFILE & ONBOARDING
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_user_profile (
    user_id             TEXT        PRIMARY KEY,
    fiqh_school         TEXT        NOT NULL DEFAULT 'hanafi'
                                    CHECK (fiqh_school IN ('hanafi','shafi','maliki','hanbali','ahle_hadith')),
    tone_preference     TEXT        NOT NULL DEFAULT 'urdu_english_mix'
                                    CHECK (tone_preference IN ('urdu_english_mix','urdu_formal','english_formal','hindi_english_mix','arabic_emphasized')),
    country_code        TEXT,
    tier_preference     TEXT        CHECK (tier_preference IN ('light','medium','severe')),
    onboarded_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tawbah_onboarding_state (
    user_id             TEXT        PRIMARY KEY,
    current_screen      INTEGER     NOT NULL DEFAULT 1,
    profile_snapshot    JSONB,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

-- ============================================================
-- TAWBAH OS — SESSION STATE
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_sessions (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'NEW_SESSION'
                                CHECK (state IN ('NEW_SESSION','TIER_DETECTED','GOAL_SELECTED','ENGINES_ACTIVE','COMPLETED','ABANDONED')),
    tier            TEXT        CHECK (tier IN ('light','medium','severe')),
    goal_type       TEXT,
    entry_type      TEXT        NOT NULL DEFAULT 'normal',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tawbah_sessions_user
    ON tawbah_sessions (user_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_tawbah_sessions_state
    ON tawbah_sessions (state);

-- ============================================================
-- TAWBAH OS — ENGINE 0 (MUHASABA)
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_daily_muhasaba_log (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    q1_answer_enc   BYTEA,
    q2_answer_enc   BYTEA,
    q3_answer_enc   BYTEA,
    q4_answer_enc   BYTEA,
    logged_date     DATE        NOT NULL,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_daily_muhasaba_user
    ON tawbah_daily_muhasaba_log (user_id, logged_date DESC);

CREATE TABLE IF NOT EXISTS tawbah_weekly_muhasaba_deep_log (
    id                      SERIAL      PRIMARY KEY,
    user_id                 TEXT        NOT NULL,
    zuban_reflection_enc    BYTEA,
    nafs_reflection_enc     BYTEA,
    qalb_reflection_enc     BYTEA,
    amal_reflection_enc     BYTEA,
    week_ending             DATE        NOT NULL,
    logged_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_weekly_muhasaba_user
    ON tawbah_weekly_muhasaba_deep_log (user_id, week_ending DESC);

CREATE TABLE IF NOT EXISTS tawbah_sin_pattern_observations (
    id                      SERIAL      PRIMARY KEY,
    user_id                 TEXT        NOT NULL,
    pattern_type            TEXT        NOT NULL,
    signal_count            INTEGER     NOT NULL DEFAULT 0,
    pattern_description     TEXT,
    first_detected          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_sin_patterns_user
    ON tawbah_sin_pattern_observations (user_id, last_updated DESC);

CREATE TABLE IF NOT EXISTS tawbah_heart_disease_handoffs (
    id                      SERIAL      PRIMARY KEY,
    user_id                 TEXT        NOT NULL,
    disease_detected        TEXT        NOT NULL,
    signals_count           INTEGER     NOT NULL DEFAULT 0,
    handoff_offered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_response           TEXT
);

CREATE INDEX IF NOT EXISTS idx_tawbah_heart_disease_user
    ON tawbah_heart_disease_handoffs (user_id, handoff_offered_at DESC);

-- ============================================================
-- TAWBAH OS — ENGINE 1 (AQAL vs NAFS NEGOTIATION)
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_aqal_nafs_logs (
    id              SERIAL      PRIMARY KEY,
    session_id      INTEGER     REFERENCES tawbah_sessions(id) ON DELETE SET NULL,
    user_id         TEXT        NOT NULL,
    urge_text_enc   BYTEA,
    nafs_voice_enc  BYTEA,
    aqal_voice_enc  BYTEA,
    resolution      TEXT,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_aqal_nafs_user
    ON tawbah_aqal_nafs_logs (user_id, logged_at DESC);

-- ============================================================
-- TAWBAH OS — ENGINE 2 (TAWBAH ROADMAP)
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_roadmap (
    id                          SERIAL      PRIMARY KEY,
    session_id                  INTEGER     REFERENCES tawbah_sessions(id) ON DELETE SET NULL,
    user_id                     TEXT        NOT NULL,
    gunah_description_enc       BYTEA,
    requires_huquq              BOOLEAN     NOT NULL DEFAULT FALSE,
    current_step                TEXT        NOT NULL DEFAULT 'imsak'
                                            CHECK (current_step IN ('imsak','nadim','azm','huquq_ul_ibaad')),
    status                      TEXT        NOT NULL DEFAULT 'in_progress'
                                            CHECK (status IN ('in_progress','completed','abandoned')),
    started_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tawbah_roadmap_user
    ON tawbah_roadmap (user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS tawbah_roadmap_steps (
    id              SERIAL      PRIMARY KEY,
    roadmap_id      INTEGER     NOT NULL REFERENCES tawbah_roadmap(id) ON DELETE CASCADE,
    user_id         TEXT        NOT NULL,
    step            TEXT        NOT NULL
                                CHECK (step IN ('imsak','nadim','azm','huquq_ul_ibaad')),
    reflection_enc  BYTEA,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_roadmap_steps_roadmap
    ON tawbah_roadmap_steps (roadmap_id, logged_at);

-- ============================================================
-- TAWBAH OS — ENGINE 3 (HABIT BREAKING)
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_shaytan_patterns (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    trigger_time    TEXT,
    location_enc    BYTEA,
    emotion_enc     BYTEA,
    gunah_category  TEXT,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_shaytan_user
    ON tawbah_shaytan_patterns (user_id, logged_at DESC);

CREATE INDEX IF NOT EXISTS idx_tawbah_shaytan_time
    ON tawbah_shaytan_patterns (user_id, trigger_time);

CREATE TABLE IF NOT EXISTS tawbah_relapse_log (
    id                          SERIAL      PRIMARY KEY,
    session_id                  INTEGER     REFERENCES tawbah_sessions(id) ON DELETE SET NULL,
    user_id                     TEXT        NOT NULL,
    context_enc                 BYTEA,
    minutes_before_predicted    INTEGER,
    logged_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_relapse_user
    ON tawbah_relapse_log (user_id, logged_at DESC);

-- ============================================================
-- TAWBAH OS — ENGINE 4 (ISTIQAMAH + RUHANI FATIGUE)
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_istiqamah_chapters (
    user_id                 TEXT        NOT NULL,
    chapter_id              TEXT        NOT NULL,
    streak_start_date       DATE,
    current_day_count       INTEGER     NOT NULL DEFAULT 0,
    last_active_date        DATE,
    max_streak_achieved     INTEGER     NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, chapter_id)
);

CREATE TABLE IF NOT EXISTS tawbah_ruhani_fatigue_detections (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT        NOT NULL,
    signals_active      TEXT[],
    composite_weight    REAL,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_ruhani_fatigue_user
    ON tawbah_ruhani_fatigue_detections (user_id, detected_at DESC);

-- ============================================================
-- TAWBAH OS — ENGINE 5 (SPIRITUAL RESURRECTION / TAHAJJUD)
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_tahajjud_sessions (
    id              SERIAL      PRIMARY KEY,
    session_id      INTEGER     REFERENCES tawbah_sessions(id) ON DELETE SET NULL,
    user_id         TEXT        NOT NULL,
    current_step    TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tawbah_tahajjud_user
    ON tawbah_tahajjud_sessions (user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS tawbah_tahajjud_step_logs (
    id              SERIAL      PRIMARY KEY,
    tahajjud_id     INTEGER     NOT NULL REFERENCES tawbah_tahajjud_sessions(id) ON DELETE CASCADE,
    user_id         TEXT        NOT NULL,
    step            TEXT        NOT NULL,
    reflection_enc  BYTEA,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_tahajjud_steps_session
    ON tawbah_tahajjud_step_logs (tahajjud_id, logged_at);

-- ============================================================
-- TAWBAH OS — ENGINE 6 (KAFFARAT-UL-DHUNUB)
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_kaffarah_activation (
    id                              SERIAL      PRIMARY KEY,
    session_id                      INTEGER     REFERENCES tawbah_sessions(id) ON DELETE SET NULL,
    user_id                         TEXT        NOT NULL,
    activated_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_days                   INTEGER     NOT NULL
                                                CHECK (duration_days IN (30, 60, 90)),
    target_gunah_optional_enc       BYTEA,
    status                          TEXT        NOT NULL DEFAULT 'active'
                                                CHECK (status IN ('active','completed','abandoned'))
);

CREATE INDEX IF NOT EXISTS idx_tawbah_kaffarah_user
    ON tawbah_kaffarah_activation (user_id, activated_at DESC);

CREATE TABLE IF NOT EXISTS tawbah_istighfar_log (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    count           INTEGER     NOT NULL DEFAULT 0,
    type            TEXT        NOT NULL DEFAULT 'basic'
                                CHECK (type IN ('basic','sayyid_morning','sayyid_evening')),
    logged_date     DATE        NOT NULL,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_istighfar_user_date
    ON tawbah_istighfar_log (user_id, logged_date DESC);

CREATE TABLE IF NOT EXISTS tawbah_sadaqah_kaffarah_log (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT        NOT NULL,
    amount_enc          BYTEA,
    currency            TEXT        NOT NULL DEFAULT 'PKR',
    recipient_type      TEXT,
    niyyah_enc          BYTEA,
    linked_gunah_enc    BYTEA,
    is_jariyah          BOOLEAN     NOT NULL DEFAULT FALSE,
    logged_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_sadaqah_user
    ON tawbah_sadaqah_kaffarah_log (user_id, logged_at DESC);

CREATE TABLE IF NOT EXISTS tawbah_hasanat_log (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    category        TEXT        NOT NULL,
    description_enc BYTEA,
    niyyah_enc      BYTEA,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_hasanat_user
    ON tawbah_hasanat_log (user_id, logged_at DESC);

CREATE TABLE IF NOT EXISTS tawbah_musibat_sabr_log (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT        NOT NULL,
    category            TEXT        NOT NULL,
    sensitivity_level   TEXT,
    reflection_enc      BYTEA,
    logged_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_musibat_user
    ON tawbah_musibat_sabr_log (user_id, logged_at DESC);

CREATE TABLE IF NOT EXISTS tawbah_dua_for_others_log (
    id                          SERIAL      PRIMARY KEY,
    user_id                     TEXT        NOT NULL,
    mode                        TEXT        NOT NULL
                                            CHECK (mode IN ('specific_person','general_ummah','specific_group')),
    target_encrypted_optional   BYTEA,
    logged_date                 DATE        NOT NULL,
    logged_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_dua_others_user
    ON tawbah_dua_for_others_log (user_id, logged_date DESC);

CREATE TABLE IF NOT EXISTS tawbah_hajj_umrah_intentions (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    type            TEXT        NOT NULL
                                CHECK (type IN ('hajj_planned','hajj_completed','umrah_planned','umrah_completed')),
    status          TEXT        NOT NULL DEFAULT 'logged',
    year_target     INTEGER,
    niyyah_enc      BYTEA,
    reflection_enc  BYTEA,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_hajj_umrah_user
    ON tawbah_hajj_umrah_intentions (user_id, logged_at DESC);

-- ============================================================
-- TAWBAH OS — DISPLAY / AUDIT LOGS
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_helpline_display_log (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT        NOT NULL,
    country_code        TEXT,
    helpline_type       TEXT,
    trigger_context     TEXT,
    displayed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_helpline_user
    ON tawbah_helpline_display_log (user_id, displayed_at DESC);

CREATE TABLE IF NOT EXISTS tawbah_sacred_line_display_log (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    line_id         TEXT        NOT NULL,
    context         TEXT,
    displayed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_sacred_line_user
    ON tawbah_sacred_line_display_log (user_id, displayed_at DESC);

-- ============================================================
-- TAWBAH OS — CRISIS / EXIT PATHWAY LOGS
-- ============================================================

CREATE TABLE IF NOT EXISTS tawbah_crisis_detections (
    id                  SERIAL      PRIMARY KEY,
    user_id             TEXT        NOT NULL,
    session_id          INTEGER     REFERENCES tawbah_sessions(id) ON DELETE SET NULL,
    trigger_phrase_hash TEXT,
    response_offered    TEXT,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_crisis_user
    ON tawbah_crisis_detections (user_id, detected_at DESC);

CREATE TABLE IF NOT EXISTS tawbah_exit_pathways (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    session_id      INTEGER     REFERENCES tawbah_sessions(id) ON DELETE SET NULL,
    exit_type       TEXT        NOT NULL
                                CHECK (exit_type IN ('completed','abandoned','paused','mufti_handoff','tibb_handoff','mental_health_bridge')),
    notes           TEXT,
    exited_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tawbah_exit_user
    ON tawbah_exit_pathways (user_id, exited_at DESC);
"""


def run_ddl(cur) -> None:
    print("📋 Creating Fitrah tables (IF NOT EXISTS)…")
    cur.execute(CREATE_TABLES_SQL)
    print("   ✅ Fitrah tables ready.")
    print("📋 Running Fitrah migrations…")
    cur.execute(MIGRATE_SQL)
    print("   ✅ Fitrah migrations applied.")
    print("📋 Creating Tawbah OS tables (IF NOT EXISTS)…")
    cur.execute(TAWBAH_TABLES_SQL)
    print("   ✅ Tawbah OS tables ready.")


# ── Master actions seeder ─────────────────────────────────────────────────────

def seed_actions(cur) -> int:
    """
    Supports two formats in actions_master.json:
      Full format:  {"actions": [...]}          — seeds all actions
      Patch format: {"new_actions": [...]}       — upserts only the listed actions

    Auto-detects field naming convention:
      New format (v1.0): "key", "primary_dimension", "primary_points", "daily_cap"
      Old format:        "action_key", "dimension_primary", "points_primary", "max_per_day"

    Both use ON CONFLICT DO UPDATE so it is always safe to re-run.
    """
    actions_data = load_json("actions_master.json")

    if "actions" in actions_data:
        actions = actions_data["actions"]
        label = "full"
    elif "new_actions" in actions_data:
        actions = actions_data["new_actions"]
        label = "patch"
    else:
        print("   ⚠️  actions_master.json: no 'actions' or 'new_actions' key found — skipped.")
        return 0

    count = 0
    for action in actions:
        # Detect new vs old field naming convention
        is_new_format = "key" in action and "primary_points" in action

        if is_new_format:
            # New format (v1.0): store entire action object, use "key" as action_key
            action_key = action.get("key", "")
            if not action_key:
                continue
            row = action  # store full object as data
        else:
            # Old format: normalise to consistent schema
            action_key = action.get("action_key", "")
            if not action_key:
                continue
            row = {
                "key":                 action_key,
                "label_en":            action.get("action_name") or action_key,
                "source_module":       action.get("source_module", "module1"),
                "primary_dimension":   action.get("dimension_primary", "tazkiya").upper(),
                "primary_points":      action.get("points_primary", 0),
                "secondary_dimension": action.get("dimension_secondary"),
                "secondary_points":    action.get("points_secondary"),
                "daily_cap":           action.get("max_per_day", 1),
                "cap_period":          "day",
                "is_penalty":          action.get("is_penalty", False),
            }

        cur.execute(
            """INSERT INTO fitrah_master_actions (action_key, data)
               VALUES (%s, %s)
               ON CONFLICT (action_key) DO UPDATE SET data = EXCLUDED.data""",
            (action_key, json.dumps(row, ensure_ascii=False)),
        )
        count += 1

    print(f"   ✅ {count} actions upserted into fitrah_master_actions ({label} format).")
    return count


# ── System config seeder ──────────────────────────────────────────────────────

def seed_system_configs(cur) -> None:
    configs = {
        "dimensions_config":      load_json("dimensions_config.json"),
        "nafs_levels_config":     load_json("nafs_levels_config.json"),
        "maqsad_engine_prompts":  load_json("maqsad_engine_prompts.json"),
    }

    # quranic_mirror_ayaat and profiler_questions are also useful to store
    try:
        configs["quranic_mirror_ayaat"] = load_json("quranic_mirror_ayaat.json")
    except FileNotFoundError:
        pass

    try:
        configs["profiler_questions"] = load_json("profiler_questions.json")
    except FileNotFoundError:
        pass

    try:
        configs["qalb_state_opening_lines"] = load_json("qalb_state_opening_lines.json")
    except FileNotFoundError:
        pass

    try:
        configs["sunnah_dna_derivation_rules"] = load_json("sunnah_dna_derivation_rules.json")
    except FileNotFoundError:
        pass

    try:
        configs["maqsad_engine_additional_prompts"] = load_json("maqsad engine patch.json")
    except FileNotFoundError:
        pass

    try:
        configs["crisis_safe_ayaat"] = load_json("crisis_safe_ayaat.json")
    except FileNotFoundError:
        pass

    try:
        configs["sahaba_matching_config"] = load_json("sahaba_matching_config.json")
    except FileNotFoundError:
        pass

    try:
        configs["fiqh_rulings_kafarat"] = load_json("fiqh_rulings_kafarat.json")
    except FileNotFoundError:
        pass

    for key, data in configs.items():
        cur.execute(
            """INSERT INTO fitrah_system_configs (config_key, data)
               VALUES (%s, %s)
               ON CONFLICT (config_key) DO UPDATE
               SET data = EXCLUDED.data, updated_at = now()""",
            (key, json.dumps(data, ensure_ascii=False)),
        )
        print(f"   ✅ '{key}' stored in fitrah_system_configs.")


# ── Tawbah OS config seeder ───────────────────────────────────────────────────

TAWBAH_DATA_DIR = os.path.join(os.path.dirname(__file__), "tawbah_os", "data")

# Map config_key → source filename (some shared files live in fitrah_engine/data)
_TAWBAH_CONFIGS = {
    "onboarding_screens":            ("tawbah", "onboarding_screens.json"),
    "tier_detection_rules":          ("tawbah", "tier_detection_rules.json"),
    "engine_tier_allowance":         ("tawbah", "engine_tier_allowance.json"),
    "tawbah_actions_config":         ("tawbah", "tawbah_actions_config.json"),
    "muhasaba_engine_config":        ("tawbah", "muhasaba_engine_config.json"),
    "weekly_muhasaba_questions":     ("tawbah", "weekly_muhasaba_questions.json"),
    "kaffarah_engine_config":        ("tawbah", "kaffarah_engine_config.json"),
    "aqal_nafs_negotiation_config":  ("tawbah", "aqal_nafs_negotiation_config.json"),
    "bad_habits_subtypes":           ("tawbah", "bad_habits_subtypes.json"),
    "internal_dialogue_corrections": ("tawbah", "internal_dialogue_corrections.json"),
    "islamic_replacements":          ("tawbah", "islamic_replacements.json"),
    "relapse_prediction_config":     ("tawbah", "relapse_prediction_config.json"),
    "ruhani_fatigue_signals":        ("tawbah", "ruhani_fatigue_signals.json"),
    "streak_milestones":             ("tawbah", "streak_milestones.json"),
    "tawbah_nishaniyaan":            ("tawbah", "tawbah_nishaniyaan.json"),
    "sacred_lines_rotation":         ("tawbah", "sacred_lines_rotation.json"),
    "qabooliyat_times_config":       ("tawbah", "qabooliyat_times_config.json"),
    "tier3_mufti_referral_cases":    ("tawbah", "tier3_mufti_referral_cases.json"),
    "helplines_by_country":          ("tawbah", "helplines_by_country.json"),
    "crisis_detection_patterns":     ("tawbah", "crisis_detection_patterns.json"),
    "mental_health_bridge_config":   ("tawbah", "mental_health_bridge_config.json"),
    "exit_pathways_config":          ("tawbah", "exit_pathways_config.json"),
    "external_feature_links":        ("tawbah", "external_feature_links.json"),
    # Shared files — prefer fitrah copy if present, else fall back to tawbah copy
    "crisis_safe_ayaat":             ("shared", "crisis_safe_ayaat.json"),
    "qalb_state_opening_lines":      ("shared", "qalb_state_opening_lines.json"),
    "fiqh_rulings_kafarat":          ("shared", "fiqh_rulings_kafarat.json"),
}


def _load_tawbah_json(location: str, filename: str):
    if location == "shared":
        fitrah_path = os.path.join(DATA_DIR, filename)
        tawbah_path = os.path.join(TAWBAH_DATA_DIR, filename)
        path = fitrah_path if os.path.exists(fitrah_path) else tawbah_path
    else:
        path = os.path.join(TAWBAH_DATA_DIR, filename)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def seed_tawbah_configs(cur) -> int:
    count = 0
    skipped = []
    for key, (location, filename) in _TAWBAH_CONFIGS.items():
        try:
            data = _load_tawbah_json(location, filename)
        except FileNotFoundError:
            skipped.append(filename)
            continue
        cur.execute(
            """INSERT INTO tawbah_system_configs (config_key, data)
               VALUES (%s, %s)
               ON CONFLICT (config_key) DO UPDATE
               SET data = EXCLUDED.data, updated_at = now()""",
            (key, json.dumps(data, ensure_ascii=False)),
        )
        count += 1
        print(f"   ✅ '{key}' stored in tawbah_system_configs.")
    if skipped:
        print(f"   ⚠️  Skipped (file not found): {', '.join(skipped)}")
    return count


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n🚀 Fitrah AI — Database Seeder")
    print(f"   Connecting to {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}…")

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = False
        cur = conn.cursor()
        print("   ✅ Connected.\n")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    try:
        run_ddl(cur)
        print("\n📦 Seeding master data…")
        seed_actions(cur)
        seed_system_configs(cur)
        print("\n📦 Seeding Tawbah OS configs…")
        tcount = seed_tawbah_configs(cur)
        print(f"   ✅ {tcount} Tawbah configs stored.")
        conn.commit()

        print("\n✅ All done! Fitrah + Tawbah OS database is ready.")
        print("\nNext steps:")
        print("  1. Add ANTHROPIC_API_KEY to your .env file")
        print("  2. Start the server: uvicorn main:app --reload --port 8000")
        print("  3. Fitrah API docs: http://localhost:8000/docs#/Fitrah")
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Seeding failed: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
