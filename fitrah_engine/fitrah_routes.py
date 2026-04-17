"""
fitrah_routes.py — Fitrah AI Gamification Engine API

Endpoints (all under /api/fitrah):
  POST /log_action            — Log a completed Islamic action, award points
  GET  /user/{user_id}/profile — Full user profile (dimensions + nafs level)
  POST /user/setup             — Create user and set archetype
  GET  /actions                — List all available actions from master config
  GET  /nafs_levels            — List all nafs levels config
  POST /maqsad/statement       — Generate personalised Maqsad Statement (AI)
  POST /maqsad/mirror          — Quranic Mirror daily tafseer (AI)
  GET  /health                 — Health check

Auth: same JWT / static-token flow as the RAG engine (reuses verify_token).
DB:   shared PostgreSQL pool from database.py (initialised by the RAG engine).
"""

import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from openai import OpenAI
from pydantic import BaseModel, Field

from database import get_db_connection, release_db_connection
from fitrah_engine.scoring_logic import (
    ACTIONS,
    DIM_COLUMNS,
    DAILY_MAX_GAINS,
    NAFS_LEVELS,
    VALID_DIMENSIONS,
    calculate_crystal_score,
    calculate_barakah_score,
    barakah_to_points,
    calculate_resilience_score,
    determine_spiritual_state,
    get_spiritual_state_meta,
    get_cap_period_days,
    get_nafs_level,
    get_nafs_progress_pct,
    get_weakest_dimension,
    get_strongest_dimension,
    extract_sunnah_dna,
    sunnah_dna_to_scores,
)
from rag_engine.app.main import verify_token, limiter
from fitrah_engine.fitrah_middleware import (
    process_ai_response,
    check_crisis,
    build_user_context,
)

log = logging.getLogger("fitrah.routes")


def _run_middleware(user_id: str, text: str, last_user_message: str = "", action_key: Optional[str] = None) -> tuple[str, dict]:
    """Fetch user row and run PDF §23 6-layer + 4 safety-check pipeline on AI text.

    Returns (processed_text, flags). On any DB/pipeline failure, returns the
    original text unchanged so user-facing endpoints remain resilient.
    """
    if not text:
        return text, {}
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT user_id, spiritual_state_confirmed, detailed_view_enabled,
                      spiritual_state_suggested, detailed_view_check_streak
               FROM fitrah_users WHERE user_id = %s""",
            (user_id,),
        )
        row = cur.fetchone()
        user_row = {
            "user_id":                    user_id,
            "spiritual_state_confirmed":  bool(row[1]) if row else True,
            "detailed_view_enabled":      bool(row[2]) if row else False,
            "spiritual_state_suggested":  row[3] if row else None,
            "detailed_view_check_streak": int(row[4] or 0) if row else 0,
        }
        ctx = build_user_context(user_row, last_user_message=last_user_message or "")
        action_ctx = {"action_key": action_key} if action_key else None
        return process_ai_response(text, ctx, action_ctx)
    except Exception as e:
        log.warning(f"[middleware] pipeline skipped for user={user_id}: {e}")
        return text, {}
    finally:
        if conn is not None:
            release_db_connection(conn)

# ── Anthropic client (for Maqsad Engine prompts) ─────────────────────────────
_anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
anthropic_client = anthropic.Anthropic(api_key=_anthropic_key) if _anthropic_key else None

# ── OpenAI client (for pgvector embedding — must match knowledge_base vectors) ─
_openai_key  = os.getenv("OPENAI_API_KEY", "")
_openai_client = OpenAI(api_key=_openai_key) if _openai_key else None

# ── Maqsad prompts loaded from JSON ──────────────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load_json(filename: str):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


_maqsad_prompts       = _load_json("maqsad_engine_prompts.json")
_quranic_ayaat        = _load_json("quranic_mirror_ayaat.json")
_qalb_opening_lines   = _load_json("qalb_state_opening_lines.json")
_crisis_ayaat         = _load_json("crisis_safe_ayaat.json")
_sahaba_cfg           = _load_json("sahaba_matching_config.json")
_kafarat_cfg          = _load_json("fiqh_rulings_kafarat.json")

# Map our fiqh_school keys → knowledge_base.fiqh column values (LOWER match)
_FIQH_KB_MAP: dict[str, str] = {
    "hanafi":      "hanafi",
    "shafi_i":     "shafii",
    "maliki":      "maliki",
    "hanbali":     "hanbali",
    "ahle_hadith": "ahle_hadees",
}

# ── Maqsad prompt-config shim ───────────────────────────────────────────────
# The JSON was migrated to a new schema (ai_model_config + ai_prompts list).
# Older code here expects legacy keys (api_config, prompts dict, additional_ai_calls,
# error_handling.fallback_messages, plus a separate "maqsad engine patch.json"
# with new_prompts). We normalise at load time so endpoints keep working.

def _normalize_prompt(p: dict) -> dict:
    """Return a dict that answers both legacy and new field names."""
    return {
        "system_prompt":  p.get("system_prompt")  or p.get("system_prompt_ur", ""),
        "user_prompt":    p.get("user_prompt")    or p.get("user_prompt_template", ""),
        "simple_prompt":  p.get("simple_prompt")  or p.get("user_prompt_template") or p.get("user_prompt", ""),
        **p,
    }

_ai_cfg = _maqsad_prompts.get("ai_model_config", {}) or {}
_MAQSAD_CFG = _maqsad_prompts.get("api_config") or {
    "model":       _ai_cfg.get("primary_model",      "claude-sonnet-4-6"),
    "max_tokens":  _ai_cfg.get("max_tokens_default", 800),
    "temperature": _ai_cfg.get("default_temperature", 0.7),
}

# Legacy "prompts" dict → build from ai_prompts list if present
_prompts_by_key: dict[str, dict] = {}
_legacy_prompts = _maqsad_prompts.get("prompts") or {}
if isinstance(_legacy_prompts, dict):
    _prompts_by_key.update({k: _normalize_prompt(v) for k, v in _legacy_prompts.items()})
for _p in _maqsad_prompts.get("ai_prompts", []) or []:
    _key = _p.get("prompt_key") or _p.get("prompt_id")
    if _key:
        _prompts_by_key[_key] = _normalize_prompt(_p)

_EMPTY_PROMPT = {"system_prompt": "", "user_prompt": "", "simple_prompt": ""}

# Best-effort mapping: legacy slot → closest current prompt_key
_PROMPT_1 = (_prompts_by_key.get("prompt_1_maqsad_statement")
             or _prompts_by_key.get("life_mission_generator")
             or _prompts_by_key.get("fitrah_identity_generator")
             or _EMPTY_PROMPT)
_PROMPT_2 = (_prompts_by_key.get("prompt_2_quranic_mirror_tafseer")
             or _EMPTY_PROMPT)
_PROMPT_3 = (_prompts_by_key.get("prompt_3_monthly_fitrah_report")
             or _EMPTY_PROMPT)

# additional_ai_calls & error_handling shims
_ADDITIONAL_CALLS = _maqsad_prompts.get("additional_ai_calls") or {}
_fb_text = _ai_cfg.get("fallback_message_ur") or "Abhi connection mein masla hai — thodi der mein dobara try karein."
_FALLBACKS = (_maqsad_prompts.get("error_handling", {}) or {}).get("fallback_messages") or {
    "maqsad_fallback":            _fb_text,
    "mirror_fallback":            _fb_text,
    "report_fallback":            _fb_text,
    "maqsad_generation_failed":   _fb_text,
}

# _PATCH_CALLS (drift_check, qadr, life_test, sunnah_dna_analyzer) — alias from new schema
_patch_alias = {
    "purpose_drift_detector": "purpose_drift_reporter",
    "qadr_engine":            "qadr_engine_classifier",
    "life_test_classifier":   "life_test_classifier",
    "sunnah_dna_analyzer":    "sunnah_dna_analyzer",
}
try:
    _maqsad_patch = _load_json("maqsad engine patch.json")
    _PATCH_CALLS  = _maqsad_patch.get("new_prompts", {}) or {}
except Exception:
    _maqsad_patch = {}
    _PATCH_CALLS  = {}
for _legacy_key, _json_key in _patch_alias.items():
    if _legacy_key not in _PATCH_CALLS and _json_key in _prompts_by_key:
        _PATCH_CALLS[_legacy_key] = _prompts_by_key[_json_key]

router = APIRouter()


# ── Nafs Level Time Gates ─────────────────────────────────────────────────────
# Days at the *current* level required before advancing to the *target* level.
# Source: FitrahOS_Complete_Workflow.pdf
_NAFS_TIME_GATES: dict[str, dict] = {
    # target_level: min_days at current, taqwa_min, no_dim_below, tawbah_streak_min
    # Source: FitrahOS_Complete_Workflow.pdf §04
    "nafs_e_lawwamah":   {"min_days": 30,  "taqwa_min": 0,  "no_dim_below": 0,  "tawbah_streak_min": 0},
    "nafs_e_mulhama":    {"min_days": 60,  "taqwa_min": 30, "no_dim_below": 0,  "tawbah_streak_min": 0},
    "nafs_e_mutmainnah": {"min_days": 90,  "taqwa_min": 60, "no_dim_below": 50, "tawbah_streak_min": 0},
    "nafs_e_radhiya":    {"min_days": 180, "taqwa_min": 75, "no_dim_below": 65, "tawbah_streak_min": 90},
    "nafs_e_mardhiyyah": {"min_days": 365, "taqwa_min": 85, "no_dim_below": 80, "tawbah_streak_min": 0},
}

# Mandatory disclaimer for ANY nafs level change — 5 second forced display (NON-NEGOTIABLE)
NAFS_LEVEL_DISCLAIMER = (
    "Yeh Nafs level aap ke behavioral patterns ki alamat hai. "
    "Quran mein yeh spiritual stations hain jinki asal haqiqat sirf Allah ko maloom hai. "
    "Aap ki asal station aap ke aur Allah ke darmiyan hai — "
    "is app ka kaam aap ko raasta dikhana hai, diagnose karna nahi."
)


def _check_nafs_time_gate(
    target_level_key: str,
    days_at_current_level: int,
    dim_scores: dict,
    tawbah_streak: int = 0,
) -> tuple[bool, str]:
    """Returns (gate_passed, reason_if_blocked)."""
    gate = _NAFS_TIME_GATES.get(target_level_key)
    if not gate:
        return True, ""
    if days_at_current_level < gate["min_days"]:
        remaining = gate["min_days"] - days_at_current_level
        return False, f"{remaining} more days of consistency needed (gate: {gate['min_days']} days)"
    taqwa = float(dim_scores.get("taqwa", 0))
    if taqwa < gate["taqwa_min"]:
        return False, f"Taqwa must reach {gate['taqwa_min']} (currently {round(taqwa)})"
    if gate["no_dim_below"] > 0:
        weak = [d for d, v in dim_scores.items() if (v or 0) < gate["no_dim_below"]]
        if weak:
            return False, f"All dimensions must be {gate['no_dim_below']}+ (weak: {', '.join(weak)})"
    tsm = gate.get("tawbah_streak_min", 0)
    if tsm > 0 and tawbah_streak < tsm:
        return False, f"Tawbah OS streak must reach {tsm} days (currently {tawbah_streak})"
    return True, ""


# ── Request / Response models ─────────────────────────────────────────────────

class LogActionRequest(BaseModel):
    action_key: str
    user_id: Optional[str] = None  # overridden by JWT sub

_VALID_TONE_PREFS = frozenset(["urdu_english_mix", "urdu_only", "english_only"])

class UserSetupRequest(BaseModel):
    user_id: Optional[str] = None   # overridden by JWT sub
    archetype_key: Optional[str] = None
    life_stage: Optional[str] = None
    ummah_role: Optional[str] = None
    jalali_jamali: Optional[str] = None
    introvert_extrovert: Optional[str] = None
    tone_preference: Optional[str] = None        # urdu_english_mix | urdu_only | english_only
    detailed_view_enabled: Optional[bool] = None  # toggle detailed score view (riya tracking)

class MaqsadStatementRequest(BaseModel):
    user_id: Optional[str] = None
    life_stage: str = "young_adult"
    ummah_role: str = "wasatiyya"
    jalali_jamali: str = "mixed"
    introvert_extrovert: str = "ambivert"

class QuranicMirrorRequest(BaseModel):
    user_id:         Optional[str] = None
    recent_activity: str = ""    # optional context from last 3 days
    situation:       str = ""    # e.g. "anxious", "financial_problem" — matched against life_situation_tags

class MonthlyReportRequest(BaseModel):
    user_id: Optional[str] = None
    month_name: str  # e.g. "April 2026"
    namaz_completion_rate: float = 0.0
    sadaqah_count: int = 0
    tawbah_os_streak_max: int = 0


class ConfirmPromotionRequest(BaseModel):
    user_id: Optional[str] = None
    new_level: str          # level_key the user is confirming
    disclaimer_shown: bool  # must be True — frontend enforces 5-second display
    user_confirmed: bool    # must be True — user tapped "I understand"


class BarakahTrackRequest(BaseModel):
    user_id: Optional[str] = None
    task_description: str = ""
    niyyah_confirmed: bool = False
    focus_level: int = 3        # 1–5
    distraction_level: int = 3  # 1–5 (lower = less distracted = better)
    dimension_key: str = "tazkiya"  # which dimension receives barakah points


# ── Helpers ───────────────────────────────────────────────────────────────────

def _upsert_user(cur, user_id: str) -> None:
    """Create fitrah_users + fitrah_user_dimensions rows if they don't exist."""
    cur.execute(
        "INSERT INTO fitrah_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )
    cur.execute(
        "INSERT INTO fitrah_user_dimensions (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )


def _fetch_dim_scores(cur, user_id: str) -> dict:
    cur.execute(
        """SELECT taqwa_score, ilm_score, tazkiya_score, ihsan_score, nafs_score, maal_score
           FROM fitrah_user_dimensions WHERE user_id = %s""",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return {k: 0.0 for k in DIM_COLUMNS}
    return {
        "taqwa":   float(row[0] or 0),
        "ilm":     float(row[1] or 0),
        "tazkiya": float(row[2] or 0),
        "ihsan":   float(row[3] or 0),
        "nafs":    float(row[4] or 0),
        "maal":    float(row[5] or 0),
    }


def _apply_points(cur, user_id: str, dimension: str, points: int) -> int:
    """
    Add points to a single dimension, capped at:
      1. The dimension's daily_max_gain (from dimensions_config.json)
      2. The absolute ceiling of 100

    Returns the actual points awarded (may be less than requested if cap hit).
    """
    if dimension not in VALID_DIMENSIONS:
        raise ValueError(f"Invalid dimension: {dimension}")

    daily_max = DAILY_MAX_GAINS.get(dimension, 999)
    col = DIM_COLUMNS[dimension]

    # Sum of primary + secondary points already awarded today for this dimension
    cur.execute(
        """SELECT COALESCE(SUM(points_primary), 0) + COALESCE(SUM(points_secondary), 0)
           FROM fitrah_user_action_logs
           WHERE user_id = %s
             AND (dimension_primary = %s OR dimension_secondary = %s)
             AND logged_at >= CURRENT_DATE
             AND points_primary > 0""",
        (user_id, dimension, dimension),
    )
    earned_today = int(cur.fetchone()[0] or 0)
    headroom     = max(0, daily_max - earned_today)
    actual_pts   = min(points, headroom)

    if actual_pts > 0:
        cur.execute(
            f"UPDATE fitrah_user_dimensions SET {col} = LEAST({col} + %s, 100), updated_at = now() WHERE user_id = %s",
            (actual_pts, user_id),
        )
    return actual_pts


def _call_claude(system_prompt: str, user_prompt: str) -> str | None:
    """Call Claude claude-sonnet-4-6 and return the text response, or None on failure."""
    if not anthropic_client:
        log.warning("ANTHROPIC_API_KEY not set — AI features disabled.")
        return None
    try:
        msg = anthropic_client.messages.create(
            model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
            max_tokens=_MAQSAD_CFG.get("max_tokens", 1000),
            temperature=_MAQSAD_CFG.get("temperature", 0.7),
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return None


def _fill_template(template: str, variables: dict) -> str:
    """Replace {{key}} placeholders in a prompt template."""
    for key, value in variables.items():
        template = template.replace(f"{{{{{key}}}}}", str(value))
    return template


def _smart_ayah(dimension: str, situation: str = "") -> dict | None:
    """
    Pick an Ayah for the given dimension.
    If `situation` is provided, prefer ayaat whose life_situation_tags overlap.
    Falls back to a random pick from the dimension's full list.
    JSON structure: _quranic_ayaat["dimensions"][dimension]["ayaat"]
    """
    ayaat = _quranic_ayaat.get("dimensions", {}).get(dimension, {}).get("ayaat", [])
    if not ayaat:
        return None
    if situation:
        sit_lower = situation.lower()
        matches = [
            a for a in ayaat
            if any(sit_lower in tag or tag in sit_lower
                   for tag in a.get("life_situation_tags", []))
        ]
        if matches:
            return random.choice(matches)
    return random.choice(ayaat)


_CRISIS_KEYWORDS = frozenset([
    "crisis", "broken", "suicidal", "hopeless", "helpless", "giving up",
    "end it", "end my life", "no point", "can't go on", "cant go on",
    "harm myself", "hurt myself", "not worth", "worthless",
])


def _is_crisis_situation(situation: str, qalb_state: str = "") -> bool:
    """Return True when the situation text or qalb state signals a crisis."""
    if qalb_state == "broken":
        return True
    if not situation:
        return False
    sit_lower = situation.lower()
    return any(kw in sit_lower for kw in _CRISIS_KEYWORDS)


def _crisis_ayah() -> dict | None:
    """
    Pick a random ayah from the dedicated crisis_safe_ayaat pool.
    Returns a dict shaped like quranic_mirror_ayaat entries so callers
    can treat it uniformly (ayah_id, surah_number, surah_name, verse_number,
    arabic, urdu, english keys mapped across).
    """
    ayaat = _crisis_ayaat.get("ayaat", [])
    if not ayaat:
        return None
    chosen = random.choice(ayaat)
    return {
        "ayah_id":            chosen.get("id"),
        "surah_number":       chosen.get("surah_number"),
        "surah_name":         chosen.get("surah"),
        "verse_number":       chosen.get("ayah_number"),
        "arabic_text":        chosen.get("arabic"),
        "transliteration":    "",
        "urdu_translation":   chosen.get("urdu"),
        "english_translation": chosen.get("english"),
        "default_tafseer":    chosen.get("display_context", ""),
        "is_crisis_safe":     True,
        "life_situation_tags": chosen.get("life_situation_tags", []),
    }


# ── 0b. GET /profiler/status ──────────────────────────────────────────────────

@router.get("/profiler/status")
def get_profiler_status(user_id: str = "", jwt_payload: dict = Depends(verify_token)):
    """
    Returns {"completed": true/false} indicating whether this user has already
    submitted the Fitrah Profiler (i.e. ummah_role is set in fitrah_users).
    Used by the Flutter app on login to skip the profiler on reinstall.
    """
    sub = jwt_payload.get("sub")
    uid = sub if (sub and sub != "anonymous") else user_id
    if not uid or uid == "anonymous":
        raise HTTPException(400, "user_id required")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ummah_role FROM fitrah_users WHERE user_id = %s", (uid,)
        )
        row = cur.fetchone()
        completed = row is not None and row[0] is not None
        return {"completed": completed}
    except Exception as e:
        log.error(f"get_profiler_status error for {uid}: {e}")
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 0c. GET /profiler/questions ───────────────────────────────────────────────

@router.get("/profiler/questions")
def get_profiler_questions(_jwt_payload: dict = Depends(verify_token)):
    """
    Returns the full Fitrah Profiler structure from fitrah_system_configs:
      - profiler_rules (display rules, timing)
      - habit_profiler  { title, questions: [...] }   — 12 questions
      - nature_profiler { title, questions: [...] }   — 8 questions
    Flutter uses this to render the profiler screens dynamically.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT data FROM fitrah_system_configs WHERE config_key = 'profiler_questions'",
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(503, "profiler_questions not seeded — run seed_database.py")
        data = row[0]  # full profiler_questions.json object from DB

        habit_qs   = data.get("habit_profiler",  {}).get("questions", [])
        nature_qs  = data.get("nature_profiler", {}).get("questions", [])

        return {
            "profiler_rules":  data.get("profiler_rules", {}),
            "habit_profiler":  {
                "title":       data.get("habit_profiler",  {}).get("title", ""),
                "subtitle":    data.get("habit_profiler",  {}).get("subtitle", ""),
                "questions":   habit_qs,
            },
            "nature_profiler": {
                "title":       data.get("nature_profiler", {}).get("title", ""),
                "subtitle":    data.get("nature_profiler", {}).get("subtitle", ""),
                "questions":   nature_qs,
            },
            "total_questions": len(habit_qs) + len(nature_qs),
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"get_profiler_questions error: {e}")
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 1. POST /log_action ───────────────────────────────────────────────────────

@router.post("/log_action")
def log_action(req: LogActionRequest, jwt_payload: dict = Depends(verify_token)):
    user_id = jwt_payload.get("sub") or req.user_id
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user_id required for Fitrah actions.")

    action = ACTIONS.get(req.action_key)
    if not action:
        raise HTTPException(404, f"Action '{req.action_key}' not found in master config.")

    dim_primary   = action["dimension_primary"]
    pts_primary   = action["points_primary"]
    dim_secondary = action.get("dimension_secondary")
    pts_secondary = action.get("points_secondary")
    max_per_day   = action.get("max_per_day", 1)

    if dim_primary not in VALID_DIMENSIONS:
        raise HTTPException(500, f"Config error: unknown dimension '{dim_primary}'.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Auto-create user rows if this is their first action
        _upsert_user(cur, user_id)

        # Check per-action cap (daily for max_per_day>=1; period-based for max_per_day=0)
        if max_per_day == 0:
            period_days = get_cap_period_days(action)
            cur.execute(
                """SELECT COUNT(*) FROM fitrah_user_action_logs
                   WHERE user_id = %s AND action_key = %s
                   AND logged_at >= now() - make_interval(days => %s)""",
                (user_id, req.action_key, period_days),
            )
            times_in_period = cur.fetchone()[0]
            if times_in_period >= 1:
                period_label = {7: "hafte", 30: "mahine", 90: "3 mahine"}[period_days]
                raise HTTPException(
                    409,
                    {
                        "error":          "period_cap_reached",
                        "message":        f"'{action['action_name']}' is {period_label} mein pehle se log ho chuka hai.",
                        "action_key":     req.action_key,
                        "period_days":    period_days,
                        "times_in_period": times_in_period,
                    },
                )
        else:
            cur.execute(
                """SELECT COUNT(*) FROM fitrah_user_action_logs
                   WHERE user_id = %s AND action_key = %s
                   AND logged_at >= CURRENT_DATE""",
                (user_id, req.action_key),
            )
            times_today = cur.fetchone()[0]
            if times_today >= max_per_day:
                raise HTTPException(
                    409,
                    {
                        "error":       "daily_cap_reached",
                        "message":     f"'{action['action_name']}' ka aaj ka cap ({max_per_day}) pura ho gaya. Kal dobara.",
                        "action_key":  req.action_key,
                        "times_today": times_today,
                        "max_per_day": max_per_day,
                    },
                )

        # Award primary dimension points (capped by daily_max_gain)
        pts_primary_actual = _apply_points(cur, user_id, dim_primary, pts_primary)

        # Award secondary dimension points if any (also capped)
        pts_secondary_actual = 0
        if dim_secondary and pts_secondary and dim_secondary in VALID_DIMENSIONS:
            pts_secondary_actual = _apply_points(cur, user_id, dim_secondary, pts_secondary)

        # Read back updated scores
        dim_scores = _fetch_dim_scores(cur, user_id)

        # Recalculate crystal score
        new_crystal = calculate_crystal_score(dim_scores)

        # Get old nafs level + streak info before this action
        cur.execute(
            """SELECT current_nafs_level, crystal_score, last_active_at,
                      streak_current, streak_max, tawbah_streak_current,
                      nafs_level_since
               FROM fitrah_users WHERE user_id = %s""",
            (user_id,),
        )
        user_row         = cur.fetchone()
        old_level_key    = user_row[0] if user_row else "nafs_e_ammarah"
        last_active_at   = user_row[2] if user_row else None
        streak_current   = int(user_row[3] or 0) if user_row else 0
        streak_max       = int(user_row[4] or 0) if user_row else 0
        tawbah_streak    = int(user_row[5] or 0) if user_row else 0
        nafs_level_since = user_row[6] if user_row else None

        # ── Streak + tawbah streak logic ──────────────────────────────────────
        today = datetime.now(timezone.utc).date()
        if last_active_at:
            if last_active_at.tzinfo is None:
                last_active_at = last_active_at.replace(tzinfo=timezone.utc)
            last_date = last_active_at.date()
            if last_date == today:
                pass                                    # already logged today — no change
            elif last_date == today - timedelta(days=1):
                streak_current += 1                     # consecutive day
                tawbah_streak  += 1
            else:
                streak_current = 1                      # gap — reset both
                tawbah_streak  = 1
        else:
            streak_current = 1                          # first action ever
            tawbah_streak  = 1
        streak_max = max(streak_max, streak_current)

        # Determine new nafs level (subject to time gates for promotions)
        today_date              = datetime.now(timezone.utc).date()
        days_at_level           = (today_date - nafs_level_since).days if nafs_level_since else 0
        new_level_cand          = get_nafs_level(new_crystal, dim_scores["taqwa"])
        old_level_obj           = next((l for l in NAFS_LEVELS if l["level_key"] == old_level_key), NAFS_LEVELS[0])
        is_promotion            = (
            new_level_cand["level_key"] != old_level_key
            and new_level_cand["level_order"] > old_level_obj["level_order"]
        )
        gate_blocked_msg: str | None = None
        pending_promotion_level: dict | None = None  # deferred — requires /nafs/confirm-promotion

        if is_promotion:
            gate_ok, gate_reason = _check_nafs_time_gate(
                new_level_cand["level_key"], days_at_level, dim_scores, tawbah_streak
            )
            if gate_ok:
                # Two-step flow: store pending, do NOT apply yet
                pending_promotion_level = new_level_cand
                new_level  = old_level_obj   # keep current level until user confirms
                level_up   = False
            else:
                new_level        = old_level_obj
                level_up         = False
                gate_blocked_msg = gate_reason
        else:
            new_level = new_level_cand
            # level_up is True only for regressions (level order dropped)
            level_up  = (
                new_level_cand["level_key"] != old_level_key
                and new_level_cand["level_order"] < old_level_obj["level_order"]
            )

        # Persist crystal score + nafs level + streaks + activity timestamp
        if level_up:
            # Regression: apply immediately, reset nafs_level_since, clear any pending promotion
            cur.execute(
                """UPDATE fitrah_users
                   SET crystal_score         = %s,
                       current_nafs_level    = %s,
                       pending_nafs_level    = NULL,
                       streak_current        = %s,
                       streak_max            = %s,
                       tawbah_streak_current = %s,
                       nafs_level_since      = CURRENT_DATE,
                       last_active_at        = now()
                   WHERE user_id = %s""",
                (new_crystal, new_level["level_key"], streak_current, streak_max,
                 tawbah_streak, user_id),
            )
        elif pending_promotion_level:
            # Promotion gate passed: store pending, leave current_nafs_level unchanged
            cur.execute(
                """UPDATE fitrah_users
                   SET crystal_score         = %s,
                       pending_nafs_level    = %s,
                       streak_current        = %s,
                       streak_max            = %s,
                       tawbah_streak_current = %s,
                       last_active_at        = now()
                   WHERE user_id = %s""",
                (new_crystal, pending_promotion_level["level_key"], streak_current, streak_max,
                 tawbah_streak, user_id),
            )
        else:
            cur.execute(
                """UPDATE fitrah_users
                   SET crystal_score         = %s,
                       current_nafs_level    = %s,
                       streak_current        = %s,
                       streak_max            = %s,
                       tawbah_streak_current = %s,
                       last_active_at        = now()
                   WHERE user_id = %s""",
                (new_crystal, new_level["level_key"], streak_current, streak_max,
                 tawbah_streak, user_id),
            )

        # Log the action
        cur.execute(
            """INSERT INTO fitrah_user_action_logs
               (user_id, action_key, points_primary, dimension_primary, points_secondary, dimension_secondary)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, req.action_key, pts_primary, dim_primary, pts_secondary, dim_secondary),
        )

        # ── Istiqamah streak milestone bonuses ───────────────────────────────
        streak_bonus_awarded: str | None = None
        if streak_current == 7:
            cur.execute(
                """SELECT 1 FROM fitrah_user_action_logs
                   WHERE user_id = %s AND action_key = 'istiqamah_streak_7'
                     AND logged_at >= now() - INTERVAL '14 days' LIMIT 1""",
                (user_id,),
            )
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'istiqamah_streak_7', 8, 'taqwa')",
                    (user_id,),
                )
                streak_bonus_awarded = "istiqamah_streak_7"

        elif streak_current == 30:
            cur.execute(
                """SELECT 1 FROM fitrah_user_action_logs
                   WHERE user_id = %s AND action_key = 'istiqamah_streak_30'
                     AND logged_at >= now() - INTERVAL '45 days' LIMIT 1""",
                (user_id,),
            )
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'istiqamah_streak_30', 20, 'taqwa')",
                    (user_id,),
                )
                streak_bonus_awarded = "istiqamah_streak_30"

        # ── Nafs level change — history log (regressions only; promotions logged at confirm) ──
        # level_up is True only for regressions in the new two-step flow
        level_changed_type: str | None = "regression" if level_up else None

        if level_changed_type:
            cur.execute(
                """INSERT INTO fitrah_nafs_level_history
                   (user_id, from_level, to_level, transition_type,
                    crystal_score_at_time, taqwa_at_transition, ilm_at_transition,
                    tazkiya_at_transition, ihsan_at_transition, nafs_score_at_transition,
                    maal_at_transition, days_at_previous_level,
                    time_gate_met, disclaimer_shown, mufti_review_required,
                    mufti_review_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (user_id, old_level_key, new_level["level_key"], level_changed_type,
                 new_crystal,
                 dim_scores.get("taqwa", 0), dim_scores.get("ilm", 0),
                 dim_scores.get("tazkiya", 0), dim_scores.get("ihsan", 0),
                 dim_scores.get("nafs", 0), dim_scores.get("maal", 0),
                 days_at_level,
                 True,    # regressions are always applied (no gate to block)
                 False,   # disclaimer_shown — frontend handles separately
                 False,   # mufti_review not required for regressions
                 "not_required"),
            )

        conn.commit()

        progress_pct = get_nafs_progress_pct(new_crystal, new_level)

        # Build pending promotion payload (if gate passed — user must call /nafs/confirm-promotion)
        pending_promo: dict | None = None
        if pending_promotion_level:
            mufti_req = pending_promotion_level["level_key"] in ("nafs_e_radhiya", "nafs_e_mardhiyyah")
            pending_promo = {
                "level_key":                pending_promotion_level["level_key"],
                "display_name":             pending_promotion_level["display_name"],
                "arabic":                   pending_promotion_level["arabic"],
                "level_up_message":         pending_promotion_level.get("level_up_message"),
                "animation":                pending_promotion_level.get("animation"),
                "sound":                    pending_promotion_level.get("sound"),
                "disclaimer_text":          NAFS_LEVEL_DISCLAIMER,
                "disclaimer_display_seconds": 5,
                "mufti_review_required":    mufti_req,
                "confirm_endpoint":         "POST /api/fitrah/nafs/confirm-promotion",
            }

        return {
            "success": True,
            "action_name": action["action_name"],
            "points_earned": {
                "primary":   {"dimension": dim_primary,   "points": pts_primary_actual,   "requested": pts_primary,   "capped": pts_primary_actual < pts_primary},
                "secondary": {"dimension": dim_secondary, "points": pts_secondary_actual, "requested": pts_secondary, "capped": pts_secondary_actual < (pts_secondary or 0)} if dim_secondary else None,
            },
            "dimension_scores": {k: round(v, 1) for k, v in dim_scores.items()},
            "new_crystal_score": new_crystal,
            "nafs_level": {
                "level_key":           new_level["level_key"],
                "display_name":        new_level["display_name"],
                "arabic":              new_level["arabic"],
                "progress_pct":        progress_pct,
                "level_changed_type":  level_changed_type,
                "level_down_message":  new_level.get("level_down_message") if level_changed_type == "regression" else None,
                # PDF §04 Rule 3 — disclaimer MUST be shown on both up and down transitions
                "regression_disclaimer":       NAFS_LEVEL_DISCLAIMER if level_changed_type == "regression" else None,
                "disclaimer_display_seconds":  5 if level_changed_type == "regression" else None,
                "encouragement":       new_level.get("encouragement"),
                "animation":           new_level.get("animation"),
                "sound":               new_level.get("sound"),
                "time_gate_blocked":   gate_blocked_msg,
                "pending_promotion":   pending_promo,
            },
            "streak": {
                "streak_current":        streak_current,
                "streak_max":            streak_max,
                "tawbah_streak_current": tawbah_streak,
                "bonus_awarded":         streak_bonus_awarded,
            },
            "message": f"MashAllah! +{pts_primary} {dim_primary.upper()} — {action['action_name']}",
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.error(f"log_action error for {user_id}: {e}")
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 1b. POST /nafs/confirm-promotion ─────────────────────────────────────────

@router.post("/nafs/confirm-promotion")
def confirm_nafs_promotion(req: ConfirmPromotionRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Two-step nafs promotion confirmation.
    Frontend must:
      1. Show NAFS_LEVEL_DISCLAIMER for >= 5 seconds.
      2. Get explicit user tap on "I understand".
      3. Call this endpoint with disclaimer_shown=true, user_confirmed=true.
    Promotion is only written to DB here, not in log_action.
    """
    if not req.disclaimer_shown or not req.user_confirmed:
        raise HTTPException(400, {
            "error": "confirmation_required",
            "message": "Disclaimer must be shown and user must confirm to apply nafs level promotion.",
        })

    user_id = jwt_payload.get("sub") or req.user_id
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user_id required.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """SELECT u.current_nafs_level, u.pending_nafs_level, u.crystal_score,
                      u.nafs_level_since, u.tawbah_streak_current,
                      d.taqwa_score, d.ilm_score, d.tazkiya_score,
                      d.ihsan_score, d.nafs_score, d.maal_score
               FROM fitrah_users u
               LEFT JOIN fitrah_user_dimensions d ON d.user_id = u.user_id
               WHERE u.user_id = %s""",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")

        current_level_key = row[0] or "nafs_e_ammarah"
        pending_level_key = row[1]
        crystal           = float(row[2] or 0)
        nafs_level_since  = row[3]
        tawbah_streak     = int(row[4] or 0)
        dim_scores = {
            "taqwa":   float(row[5] or 0),
            "ilm":     float(row[6] or 0),
            "tazkiya": float(row[7] or 0),
            "ihsan":   float(row[8] or 0),
            "nafs":    float(row[9] or 0),
            "maal":    float(row[10] or 0),
        }

        if not pending_level_key:
            raise HTTPException(409, {
                "error": "no_pending_promotion",
                "message": "No pending nafs promotion found for this user.",
            })

        if pending_level_key != req.new_level:
            raise HTTPException(409, {
                "error": "level_mismatch",
                "message": f"Pending promotion is to '{pending_level_key}', not '{req.new_level}'.",
            })

        new_level_obj = next((l for l in NAFS_LEVELS if l["level_key"] == pending_level_key), None)
        if not new_level_obj:
            raise HTTPException(500, f"Unknown level '{pending_level_key}' in config.")

        # Re-verify time gate (crystal may have dropped since gate was checked)
        today_date    = datetime.now(timezone.utc).date()
        days_at_level = (today_date - nafs_level_since).days if nafs_level_since else 0
        gate_ok, gate_reason = _check_nafs_time_gate(
            pending_level_key, days_at_level, dim_scores, tawbah_streak
        )
        if not gate_ok:
            # Gate no longer passes — clear the pending level
            cur.execute(
                "UPDATE fitrah_users SET pending_nafs_level = NULL WHERE user_id = %s",
                (user_id,),
            )
            conn.commit()
            raise HTTPException(409, {
                "error": "gate_no_longer_met",
                "message": f"Promotion gate no longer met: {gate_reason}. Pending promotion cleared.",
            })

        # Apply the promotion
        cur.execute(
            """UPDATE fitrah_users
               SET current_nafs_level = %s,
                   pending_nafs_level = NULL,
                   nafs_level_since   = CURRENT_DATE
               WHERE user_id = %s""",
            (pending_level_key, user_id),
        )

        # Log the transition — disclaimer_shown=True confirmed here
        mufti_required = pending_level_key in ("nafs_e_radhiya", "nafs_e_mardhiyyah")
        cur.execute(
            """INSERT INTO fitrah_nafs_level_history
               (user_id, from_level, to_level, transition_type,
                crystal_score_at_time, taqwa_at_transition, ilm_at_transition,
                tazkiya_at_transition, ihsan_at_transition, nafs_score_at_transition,
                maal_at_transition, days_at_previous_level,
                time_gate_met, disclaimer_shown, mufti_review_required, mufti_review_status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, current_level_key, pending_level_key, "promotion",
             crystal,
             dim_scores["taqwa"], dim_scores["ilm"], dim_scores["tazkiya"],
             dim_scores["ihsan"], dim_scores["nafs"], dim_scores["maal"],
             days_at_level,
             True, True,  # time_gate_met=True, disclaimer_shown=True
             mufti_required,
             "pending_review" if mufti_required else "not_required"),
        )

        conn.commit()

        progress = get_nafs_progress_pct(crystal, new_level_obj)

        return {
            "success":              True,
            "from_level":           current_level_key,
            "to_level":             pending_level_key,
            "display_name":         new_level_obj["display_name"],
            "arabic":               new_level_obj["arabic"],
            "level_up_message":     new_level_obj.get("level_up_message"),
            "progress_pct":         progress,
            "mufti_review_required": mufti_required,
            "disclaimer_confirmed": True,
            "message": f"Mubarak! Aap '{new_level_obj['display_name']}' maqam par pahunch gaye.",
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.error(f"confirm_nafs_promotion error for {user_id}: {e}")
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 2. POST /user/setup ───────────────────────────────────────────────────────

@router.post("/user/setup")
def setup_user(req: UserSetupRequest, jwt_payload: dict = Depends(verify_token)):
    user_id = jwt_payload.get("sub") or req.user_id
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user_id required.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        _upsert_user(cur, user_id)

        update_fields: list[str] = []
        update_vals:   list      = []

        if req.archetype_key:
            update_fields.append("archetype_key = %s")
            update_vals.append(req.archetype_key)
        if req.life_stage:
            update_fields.append("life_stage = %s")
            update_vals.append(req.life_stage)
        if req.ummah_role:
            update_fields.append("ummah_role = %s")
            update_vals.append(req.ummah_role)
        if req.jalali_jamali:
            update_fields.append("jalali_jamali = %s")
            update_vals.append(req.jalali_jamali)
        if req.introvert_extrovert:
            update_fields.append("introvert_extrovert = %s")
            update_vals.append(req.introvert_extrovert)
        if req.tone_preference and req.tone_preference in _VALID_TONE_PREFS:
            update_fields.append("tone_preference = %s")
            update_vals.append(req.tone_preference)
        if req.detailed_view_enabled is not None:
            update_fields.append("detailed_view_enabled = %s")
            update_vals.append(req.detailed_view_enabled)

        if update_fields:
            update_vals.append(user_id)
            cur.execute(
                f"UPDATE fitrah_users SET {', '.join(update_fields)} WHERE user_id = %s",
                update_vals,
            )

        conn.commit()
        return {"success": True, "user_id": user_id, "message": "User profile updated."}

    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 3. GET /user/{user_id}/profile ────────────────────────────────────────────

@router.get("/user/{user_id}/profile")
def get_user_profile(user_id: str, jwt_payload: dict = Depends(verify_token)):
    # Users can only read their own profile (unless admin)
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """SELECT u.archetype_key, u.current_nafs_level, u.crystal_score,
                      u.last_active_at, u.created_at, u.life_stage,
                      u.ummah_role, u.jalali_jamali, u.introvert_extrovert,
                      d.taqwa_score, d.ilm_score, d.tazkiya_score,
                      d.ihsan_score, d.nafs_score, d.maal_score,
                      u.profiler_completed_at,
                      u.streak_current, u.streak_max,
                      u.qalb_gap_flagged, u.dua_reminder_count,
                      u.primary_sahaba, u.secondary_sahaba_1, u.secondary_sahaba_2,
                      u.fiqh_school,
                      u.detailed_view_enabled, u.detailed_view_last_checked,
                      u.detailed_view_check_streak,
                      u.spiritual_state_suggested, u.relationship_neglect_days,
                      u.drift_pause_until, u.quranic_mirror_muted
               FROM fitrah_users u
               LEFT JOIN fitrah_user_dimensions d ON d.user_id = u.user_id
               WHERE u.user_id = %s""",
            (user_id,),
        )
        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "User not found in Fitrah system. Call /user/setup first.")

        dim_scores = {
            "taqwa":   float(row[9]  or 0),
            "ilm":     float(row[10] or 0),
            "tazkiya": float(row[11] or 0),
            "ihsan":   float(row[12] or 0),
            "nafs":    float(row[13] or 0),
            "maal":    float(row[14] or 0),
        }
        crystal     = float(row[2] or 0)
        level       = get_nafs_level(crystal, dim_scores["taqwa"])
        progress    = get_nafs_progress_pct(crystal, level)
        weakest     = get_weakest_dimension(dim_scores)
        strongest   = get_strongest_dimension(dim_scores)

        detailed_enabled      = bool(row[24])
        dv_last_checked       = row[25]   # TIMESTAMPTZ or None
        dv_streak             = int(row[26] or 0)
        riya_warning          = None

        # ── Riya Detection (PDF §05) ──────────────────────────────────────────
        # Track how often a user views detailed scores. If they check 7+ days in
        # a row, surface a gentle ikhlas reminder. Reset streak on a missed day.
        if detailed_enabled:
            today_utc = datetime.now(timezone.utc).date()
            if dv_last_checked is None:
                new_streak = 1
            else:
                last_date = dv_last_checked.date() if hasattr(dv_last_checked, "date") else dv_last_checked
                if last_date == today_utc:
                    new_streak = dv_streak  # same day — don't double-count
                elif last_date == today_utc - timedelta(days=1):
                    new_streak = dv_streak + 1  # consecutive day
                else:
                    new_streak = 1  # gap — reset

            cur.execute(
                """UPDATE fitrah_users
                   SET detailed_view_last_checked = now(),
                       detailed_view_check_streak = %s
                   WHERE user_id = %s""",
                (new_streak, user_id),
            )
            conn.commit()
            dv_streak = new_streak

            if dv_streak >= 7:
                riya_warning = (
                    "Aap apne scores baar baar dekh rahe hain. "
                    "Kya niyyah sirf Allah ke liye hai ya numbers tension de rahe hain? "
                    "Imam al-Ghazali: 'Jab aadmi apne neki ko count karne lagta hai, "
                    "ikhlas mein kami aati hai.' "
                    "Agar chahen to ek hafte ke liye 'Simple View' mein wapas aa jaayen."
                )

        return {
            "user_id":       user_id,
            "archetype_key": row[0],
            "life_stage":    row[5],
            "ummah_role":    row[6],
            "jalali_jamali": row[7],
            "introvert_extrovert": row[8],
            "dimension_scores": {k: round(v, 1) for k, v in dim_scores.items()},
            "crystal_score": crystal,
            "weakest_dimension":  weakest,
            "strongest_dimension": strongest,
            "nafs_level": {
                "level_key":    level["level_key"],
                "display_name": level["display_name"],
                "arabic":       level["arabic"],
                "quran_ref":    level.get("quran_reference"),
                "message":      level.get("message_to_user"),
                "encouragement": level.get("encouragement"),
                "progress_pct": progress,
                "crystal_min":  level["crystal_score_min"],
                "crystal_max":  level["crystal_score_max"],
                "visual_state": level.get("crystal_visual_state"),
                "animation":    level.get("animation"),
                "sound":        level.get("sound"),
            },
            "last_active_at": row[3].isoformat() if row[3] else None,
            "joined_at":      row[4].isoformat() if row[4] else None,
            "profiler_completed":    row[15] is not None,
            "profiler_completed_at": row[15].isoformat() if row[15] else None,
            "streak_current":    int(row[16] or 0),
            "streak_max":        int(row[17] or 0),
            "qalb_gap_flagged":  bool(row[18]),
            "dua_reminder_count": int(row[19] or 0),
            "detailed_view_enabled":      detailed_enabled,
            "detailed_view_check_streak": dv_streak,
            "riya_warning":               riya_warning,
            "sahaba_match": {
                "primary":     row[20],
                "secondary_1": row[21],
                "secondary_2": row[22],
            },
            "fiqh_school": row[23] or "hanafi",
            "spiritual_state_suggested": row[27],
            "relationship_neglect_days": int(row[28] or 0),
            "drift_pause_until":    row[29].isoformat() if row[29] else None,
            "quranic_mirror_muted": bool(row[30]) if row[30] is not None else False,
        }

    except HTTPException:
        raise
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 3b. PATCH /user/settings ──────────────────────────────────────────────────

class UserSettingsRequest(BaseModel):
    detailed_view_enabled: Optional[bool] = None
    tone_preference:       Optional[str]  = None  # urdu_english_mix | urdu_only | english_only
    quranic_mirror_muted:  Optional[bool] = None  # PDF §15 — user autonomy to mute mirror pushes


@router.patch("/user/settings")
def patch_user_settings(body: UserSettingsRequest, jwt_payload: dict = Depends(verify_token)):
    """Toggle detailed_view_enabled, tone_preference, and/or quranic_mirror_muted for the authenticated user."""
    user_id = jwt_payload.get("sub")
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user_id required.")

    update_fields: list[str] = []
    update_vals:   list      = []

    if body.detailed_view_enabled is not None:
        update_fields.append("detailed_view_enabled = %s")
        update_vals.append(body.detailed_view_enabled)
        # Reset riya streak when user disables detailed view
        if not body.detailed_view_enabled:
            update_fields.append("detailed_view_check_streak = 0")

    if body.tone_preference is not None:
        if body.tone_preference not in _VALID_TONE_PREFS:
            raise HTTPException(400, f"Invalid tone_preference. Must be one of: {sorted(_VALID_TONE_PREFS)}")
        update_fields.append("tone_preference = %s")
        update_vals.append(body.tone_preference)

    if body.quranic_mirror_muted is not None:
        update_fields.append("quranic_mirror_muted = %s")
        update_vals.append(body.quranic_mirror_muted)

    if not update_fields:
        raise HTTPException(400, "No settings provided to update.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        update_vals.append(user_id)
        cur.execute(
            f"UPDATE fitrah_users SET {', '.join(update_fields)} WHERE user_id = %s",
            update_vals,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "User not found.")
        conn.commit()
        return {"success": True, "updated": {k: v for k, v in body.model_dump().items() if v is not None}}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 4. GET /actions ───────────────────────────────────────────────────────────

@router.get("/actions")
def list_actions(_: dict = Depends(verify_token)):
    """Return all actions from master config grouped by source_module."""
    grouped: dict[str, list] = {}
    for action in ACTIONS.values():
        module = action.get("source_module", "other")
        grouped.setdefault(module, []).append({
            "action_key":     action["action_key"],
            "action_name":    action["action_name"],
            "source_feature": action.get("source_feature"),
            "dimension_primary":  action["dimension_primary"],
            "points_primary":     action["points_primary"],
            "dimension_secondary": action.get("dimension_secondary"),
            "points_secondary":    action.get("points_secondary"),
            "max_per_day":    action.get("max_per_day", 1),
            "daily_cap_primary": action.get("daily_cap_primary"),
        })
    return {"total": len(ACTIONS), "actions_by_module": grouped}


# ── 5. GET /nafs_levels ───────────────────────────────────────────────────────

@router.get("/nafs_levels")
def list_nafs_levels():
    """Return all 6 nafs levels with full descriptions — no auth needed."""
    return {
        "levels": [
            {
                "level_key":    lvl["level_key"],
                "level_order":  lvl["level_order"],
                "display_name": lvl["display_name"],
                "arabic":       lvl["arabic"],
                "quran_ref":    lvl.get("quran_reference"),
                "quran_arabic": lvl.get("quran_arabic"),
                "quran_urdu":   lvl.get("quran_urdu"),
                "crystal_min":  lvl["crystal_score_min"],
                "crystal_max":  lvl["crystal_score_max"],
                "visual_state": lvl.get("crystal_visual_state"),
                "animation":    lvl.get("animation"),
                "sound":        lvl.get("sound"),
                "message":      lvl.get("message_to_user"),
            }
            for lvl in NAFS_LEVELS
        ]
    }


# ── 6. POST /maqsad/statement ─────────────────────────────────────────────────

@router.post("/maqsad/statement")
def generate_maqsad_statement(req: MaqsadStatementRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Generate a personalised 3-part Maqsad Statement (Prompt 1).
    Reads user's current dimension scores from DB, fills the Claude prompt template,
    and returns fitrah_identity / life_mission / ummah_role.
    """
    user_id = jwt_payload.get("sub") or req.user_id
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user required.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        dim_scores = _fetch_dim_scores(cur, user_id)

        cur.execute("SELECT current_nafs_level FROM fitrah_users WHERE user_id = %s", (user_id,))
        u_row = cur.fetchone()
        nafs_key = u_row[0] if u_row else "nafs_e_ammarah"
        nafs_obj = next((l for l in NAFS_LEVELS if l["level_key"] == nafs_key), NAFS_LEVELS[0])
    finally:
        release_db_connection(conn)

    strongest = get_strongest_dimension(dim_scores)
    # Second strongest: exclude the first
    second    = max((k for k in VALID_DIMENSIONS if k != strongest), key=lambda d: dim_scores.get(d, 0))
    weakest   = get_weakest_dimension(dim_scores)

    variables = {
        "dominant_dimension_1":       strongest,
        "dominant_dimension_1_score": round(dim_scores[strongest], 1),
        "dominant_dimension_2":       second,
        "dominant_dimension_2_score": round(dim_scores[second], 1),
        "weakest_dimension":          weakest,
        "ummah_role":                 req.ummah_role,
        "jalali_jamali":              req.jalali_jamali,
        "introvert_extrovert":        req.introvert_extrovert,
        "nafs_level":                 nafs_obj["display_name"],
        "life_stage":                 req.life_stage,
    }

    user_prompt = _fill_template(_PROMPT_1["user_prompt"], variables)
    raw = _call_claude(_PROMPT_1["system_prompt"], user_prompt)

    if raw:
        # Strip markdown code fences if present
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        try:
            result = json.loads(raw)
            return {"success": True, "maqsad": result}
        except json.JSONDecodeError:
            pass

    # Fallback
    return {"success": False, "maqsad": _FALLBACKS["maqsad_fallback"]}


# ── 7. POST /maqsad/mirror ────────────────────────────────────────────────────

@router.post("/maqsad/mirror")
def quranic_mirror(req: QuranicMirrorRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Quranic Mirror daily tafseer (Prompt 2).
    Detects the user's weakest dimension, picks a relevant Ayah, and generates
    a personalised 2-line tafseer via Claude.
    """
    user_id = jwt_payload.get("sub") or req.user_id
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user required.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        dim_scores = _fetch_dim_scores(cur, user_id)
        cur.execute(
            "SELECT current_nafs_level, life_stage, quranic_mirror_muted FROM fitrah_users WHERE user_id = %s",
            (user_id,),
        )
        u_row = cur.fetchone()
        nafs_key       = u_row[0] if u_row else "nafs_e_ammarah"
        life_stage     = u_row[1] if u_row else "young_adult"
        mirror_muted   = bool(u_row[2]) if u_row else False
    finally:
        release_db_connection(conn)

    # PDF §15 — user muted Quranic Mirror push; respect autonomy
    if mirror_muted:
        return {
            "success": False,
            "muted":   True,
            "message": "Quranic Mirror push muted. Settings mein unmute kar sakte hain.",
        }

    nafs_obj  = next((l for l in NAFS_LEVELS if l["level_key"] == nafs_key), NAFS_LEVELS[0])
    weakest   = get_weakest_dimension(dim_scores)

    if _is_crisis_situation(req.situation):
        ayah = _crisis_ayah()
    else:
        ayah = _smart_ayah(weakest, req.situation)

    if not ayah:
        return {
            "success": False,
            "tafseer": {"line_1": _FALLBACKS["mirror_fallback"], "line_2": ""},
            "ayah": None,
        }

    variables = {
        "weakest_dimension":       weakest,
        "weakest_dimension_score": round(dim_scores[weakest], 1),
        "ayah_surah":              f"{ayah.get('surah_name', '')} {ayah.get('surah_number', '')}:{ayah.get('verse_number', '')}",
        "ayah_verse":              str(ayah.get("verse_number", "")),
        "ayah_arabic":             ayah.get("arabic_text", ""),
        "ayah_translation_urdu":   ayah.get("urdu_translation", ""),
        "user_nafs_level":         nafs_obj["display_name"],
        "user_life_stage":         life_stage or "young_adult",
        "recent_activity":         req.recent_activity or "available nahi",
    }

    user_prompt = _fill_template(_PROMPT_2["user_prompt"], variables)
    raw = _call_claude(_PROMPT_2["system_prompt"], user_prompt)

    ayah_payload = {
        "ayah_id":            ayah.get("ayah_id"),
        "surah_number":       ayah.get("surah_number"),
        "surah_name":         ayah.get("surah_name"),
        "verse_number":       ayah.get("verse_number"),
        "arabic_text":        ayah.get("arabic_text"),
        "transliteration":    ayah.get("transliteration"),
        "urdu_translation":   ayah.get("urdu_translation"),
        "english_translation": ayah.get("english_translation"),
        "default_tafseer":    ayah.get("default_tafseer_2lines"),
        "weakest_dimension":  weakest,
    }

    if raw:
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        try:
            result = json.loads(raw)
            return {"success": True, "tafseer": result, "ayah": ayah_payload}
        except json.JSONDecodeError:
            pass

    return {
        "success": False,
        "tafseer": {"line_1": _FALLBACKS["mirror_fallback"], "line_2": ""},
        "ayah":    ayah_payload,
    }


# ── 8. POST /maqsad/report ────────────────────────────────────────────────────

@router.post("/maqsad/report")
def monthly_report(req: MonthlyReportRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Monthly Fitrah Report (Prompt 3).
    Queries the last 30 days of action logs, computes averages, and generates
    an AI report via Claude.
    """
    user_id = jwt_payload.get("sub") or req.user_id
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user required.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Averages of dimension scores over the last 30 days
        # We aggregate points earned per dimension per day from action logs
        cur.execute(
            """SELECT
                 AVG(CASE WHEN dimension_primary = 'taqwa'   THEN points_primary END) AS taqwa_avg,
                 AVG(CASE WHEN dimension_primary = 'ilm'     THEN points_primary END) AS ilm_avg,
                 AVG(CASE WHEN dimension_primary = 'tazkiya' THEN points_primary END) AS tazkiya_avg,
                 AVG(CASE WHEN dimension_primary = 'ihsan'   THEN points_primary END) AS ihsan_avg,
                 AVG(CASE WHEN dimension_primary = 'nafs'    THEN points_primary END) AS nafs_avg,
                 AVG(CASE WHEN dimension_primary = 'maal'    THEN points_primary END) AS maal_avg,
                 COUNT(*) AS total_actions
               FROM fitrah_user_action_logs
               WHERE user_id = %s AND logged_at >= now() - INTERVAL '30 days'""",
            (user_id,),
        )
        stats = cur.fetchone()

        cur.execute(
            "SELECT current_nafs_level, crystal_score FROM fitrah_users WHERE user_id = %s",
            (user_id,),
        )
        u_row = cur.fetchone()
        current_nafs = u_row[0] if u_row else "nafs_e_ammarah"
        crystal_end  = float(u_row[1] or 0) if u_row else 0

        dim_scores = _fetch_dim_scores(cur, user_id)
    finally:
        release_db_connection(conn)

    def _avg(v) -> float:
        return round(float(v), 1) if v is not None else 0.0

    strongest = get_strongest_dimension(dim_scores)
    weakest   = get_weakest_dimension(dim_scores)

    variables = {
        "month_name":            req.month_name,
        "taqwa_avg":             _avg(stats[0]),
        "ilm_avg":               _avg(stats[1]),
        "tazkiya_avg":           _avg(stats[2]),
        "ihsan_avg":             _avg(stats[3]),
        "nafs_avg":              _avg(stats[4]),
        "maal_avg":              _avg(stats[5]),
        "crystal_score_start":   "—",          # would need historical data
        "crystal_score_end":     crystal_end,
        "nafs_level_start":      "—",
        "nafs_level_end":        current_nafs,
        "strongest_dimension":   strongest,
        "weakest_dimension":     weakest,
        "best_day_score":        "—",
        "best_day_date":         "—",
        "worst_day_score":       "—",
        "total_actions_count":   int(stats[6] or 0),
        "namaz_completion_rate": req.namaz_completion_rate,
        "sadaqah_count":         req.sadaqah_count,
        "tawbah_os_streak_max":  req.tawbah_os_streak_max,
    }

    user_prompt = _fill_template(_PROMPT_3["user_prompt"], variables)
    raw = _call_claude(_PROMPT_3["system_prompt"], user_prompt)

    if raw:
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        try:
            result = json.loads(raw)
            return {"success": True, "report": result, "stats": variables}
        except json.JSONDecodeError:
            pass

    return {"success": False, "report": _FALLBACKS["report_fallback"]}


# ── 9. GET /health ────────────────────────────────────────────────────────────

@router.get("/health")
def fitrah_health():
    return {
        "status":        "ok",
        "engine":        "Fitrah AI Gamification",
        "actions_loaded": len(ACTIONS),
        "nafs_levels":   len(NAFS_LEVELS),
        "dimensions":    list(VALID_DIMENSIONS),
        "ai_enabled":    anthropic_client is not None,
    }


# ── 10. POST /profiler/submit ─────────────────────────────────────────────────

from fitrah_engine.scoring_logic import calculate_profiler_scores  # noqa: E402

class ProfilerAnswer(BaseModel):
    question_id:    str
    selected_label: str

class ProfilerSubmitRequest(BaseModel):
    user_id:    Optional[str] = None
    answers:    list[ProfilerAnswer]
    life_stage: str = "young_adult"

@router.post("/profiler/submit")
def submit_profiler(req: ProfilerSubmitRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Process all 20 profiler answers (12 Habit + 8 Nature).
    Calculates initial 6 dimension scores using normalization from JSON config.
    Determines ummah_role, jalali_jamali, introvert_extrovert.
    Saves everything to DB and marks profiler as complete.
    """
    user_id = jwt_payload.get("sub") or req.user_id
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user required.")

    if not req.answers:
        raise HTTPException(400, "No answers provided.")

    # Calculate scores from answers (pure function, no DB)
    answers_raw = [{"question_id": a.question_id, "selected_label": a.selected_label}
                   for a in req.answers]
    result = calculate_profiler_scores(answers_raw)

    dim_scores   = result["dimension_scores"]
    ummah_role   = result["ummah_role"]
    jalali_jamali       = result["jalali_jamali"]
    introvert_extrovert = result["introvert_extrovert"]

    crystal = calculate_crystal_score(dim_scores)
    level   = get_nafs_level(crystal, dim_scores["taqwa"])

    # Sunnah DNA — string labels + numeric 0-100 scores
    dna_labels = extract_sunnah_dna(answers_raw)
    dna_scores = sunnah_dna_to_scores(dna_labels)

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Create user rows if first time
        _upsert_user(cur, user_id)

        # Save initial dimension scores
        cur.execute(
            """UPDATE fitrah_user_dimensions
               SET taqwa_score   = %s, ilm_score     = %s, tazkiya_score = %s,
                   ihsan_score   = %s, nafs_score    = %s, maal_score    = %s,
                   updated_at    = now()
               WHERE user_id = %s""",
            (dim_scores["taqwa"], dim_scores["ilm"],   dim_scores["tazkiya"],
             dim_scores["ihsan"], dim_scores["nafs"],  dim_scores["maal"],
             user_id),
        )

        # Save ummah_role, mizaj, crystal score, nafs level, life_stage + Sunnah DNA columns
        cur.execute(
            """UPDATE fitrah_users
               SET ummah_role            = %s,
                   jalali_jamali         = %s,
                   introvert_extrovert   = %s,
                   life_stage            = %s,
                   crystal_score         = %s,
                   current_nafs_level    = %s,
                   profiler_completed_at = now(),
                   sunnah_dna            = %s,
                   sunnah_dna_ibadah     = %s,
                   sunnah_dna_eating     = %s,
                   sunnah_dna_sleeping   = %s,
                   sunnah_dna_social     = %s
               WHERE user_id = %s""",
            (ummah_role, jalali_jamali, introvert_extrovert,
             req.life_stage, crystal, level["level_key"],
             json.dumps(dna_labels),
             dna_scores["ibadah"], dna_scores["eating"],
             dna_scores["sleeping"], dna_scores["social"],
             user_id),
        )

        conn.commit()

        weakest   = get_weakest_dimension(dim_scores)
        strongest = get_strongest_dimension(dim_scores)

        return {
            "success":           True,
            "user_id":           user_id,
            "dimension_scores":  {k: round(v, 1) for k, v in dim_scores.items()},
            "crystal_score":     crystal,
            "weakest_dimension": weakest,
            "strongest_dimension": strongest,
            "ummah_role":        ummah_role,
            "jalali_jamali":     jalali_jamali,
            "introvert_extrovert": introvert_extrovert,
            "life_stage":        req.life_stage,
            "nafs_level": {
                "level_key":    level["level_key"],
                "display_name": level["display_name"],
                "arabic":       level["arabic"],
                "message":      level.get("message_to_user"),
                "encouragement": level.get("encouragement"),
            },
            "sunnah_dna": {**dna_labels, "scores": dna_scores},
            "message": "Profiler complete! Aapki Fitrah reveal ho gayi.",
        }

    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 11. GET /user/{user_id}/onboarding_status ─────────────────────────────────

@router.get("/user/{user_id}/onboarding_status")
def onboarding_status(user_id: str, jwt_payload: dict = Depends(verify_token)):
    """
    Called after login to check if user has completed the profiler.
    Flutter uses this to decide whether to show profiler questions.
    Reassessment is due every 3 months (as per profiler_questions.json config).
    """
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT profiler_completed_at FROM fitrah_users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()

        if not row or row[0] is None:
            return {
                "profiler_completed":   False,
                "profiler_date":        None,
                "reassessment_due":     False,
                "message":              "Profiler abhi complete nahi hua — questions show karein.",
            }

        completed_at = row[0]
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)

        # Reassessment every 90 days
        days_since   = (datetime.now(timezone.utc) - completed_at).days
        reassess_due = days_since >= 90

        return {
            "profiler_completed":   True,
            "profiler_date":        completed_at.date().isoformat(),
            "days_since_profiler":  days_since,
            "reassessment_due":     reassess_due,
            "reassessment_message": "3 mahine guzar gaye — dobara profiler chalayein?" if reassess_due else None,
        }
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 12. GET /user/{user_id}/today_summary ────────────────────────────────────

@router.get("/user/{user_id}/today_summary")
def today_summary(user_id: str, jwt_payload: dict = Depends(verify_token)):
    """
    Everything the Flutter dashboard needs in one call:
    - Points earned per dimension today
    - Actions logged today
    - Current crystal score + nafs level
    - Streak
    - Remaining daily caps for actions already started
    """
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Today's logs
        cur.execute(
            """SELECT action_key, points_primary, dimension_primary,
                      points_secondary, dimension_secondary, logged_at
               FROM fitrah_user_action_logs
               WHERE user_id = %s AND logged_at >= CURRENT_DATE
               ORDER BY logged_at DESC""",
            (user_id,),
        )
        logs = cur.fetchall()

        # Sum points per dimension
        today_pts: dict[str, float] = {d: 0.0 for d in DIM_COLUMNS}
        action_counts: dict[str, int] = {}
        actions_logged = []

        for row in logs:
            ak, pp, dp, ps, ds, ts = row
            if dp and dp in today_pts:
                today_pts[dp] += float(pp or 0)
            if ds and ds in today_pts:
                today_pts[ds] += float(ps or 0)
            action_counts[ak] = action_counts.get(ak, 0) + 1
            actions_logged.append({
                "action_key":        ak,
                "action_name":       ACTIONS.get(ak, {}).get("action_name", ak),
                "points_primary":    pp,
                "dimension_primary": dp,
                "logged_at":         ts.isoformat() if ts else None,
            })

        # User state
        cur.execute(
            """SELECT crystal_score, current_nafs_level,
                      streak_current, streak_max
               FROM fitrah_users WHERE user_id = %s""",
            (user_id,),
        )
        u = cur.fetchone()
        crystal        = float(u[0] or 0) if u else 0.0
        streak_current = int(u[2] or 0) if u else 0
        streak_max     = int(u[3] or 0) if u else 0

        dim_scores = _fetch_dim_scores(cur, user_id)
        level      = get_nafs_level(crystal, dim_scores["taqwa"])
        progress   = get_nafs_progress_pct(crystal, level)

        # Remaining caps — only show actions the user has already started today
        remaining_caps = {
            ak: max(0, ACTIONS[ak]["max_per_day"] - action_counts[ak])
            for ak in action_counts
            if ak in ACTIONS and ACTIONS[ak].get("max_per_day", 1) > 0
        }

        return {
            "date":                  datetime.now(timezone.utc).date().isoformat(),
            "total_actions_today":   len(logs),
            "today_points":          {k: round(v, 1) for k, v in today_pts.items()},
            "total_points_today":    round(sum(today_pts.values()), 1),
            "actions_logged":        actions_logged,
            "crystal_score":         crystal,
            "nafs_level": {
                "level_key":    level["level_key"],
                "display_name": level["display_name"],
                "arabic":       level["arabic"],
                "progress_pct": progress,
                "visual_state": level.get("crystal_visual_state"),
                "encouragement": level.get("encouragement"),
                "animation":    level.get("animation"),
                "sound":        level.get("sound"),
            },
            "streak": {
                "streak_current": streak_current,
                "streak_max":     streak_max,
            },
            "remaining_caps": remaining_caps,
        }
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 13. GET /user/{user_id}/action_logs ──────────────────────────────────────

@router.get("/user/{user_id}/action_logs")
def get_action_logs(
    user_id: str,
    days: int = 7,
    jwt_payload: dict = Depends(verify_token),
):
    """
    Returns the last N days of action logs grouped by date.
    Default: last 7 days. Max: 30 days.
    """
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    days = min(max(days, 1), 30)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT action_key, points_primary, dimension_primary,
                      points_secondary, dimension_secondary, logged_at
               FROM fitrah_user_action_logs
               WHERE user_id = %s
                 AND logged_at >= now() - INTERVAL '%s days'
               ORDER BY logged_at DESC""",
            (user_id, days),
        )
        rows = cur.fetchall()

        # Group by date
        grouped: dict[str, list] = {}
        for ak, pp, dp, ps, ds, ts in rows:
            date_key = ts.date().isoformat() if ts else "unknown"
            grouped.setdefault(date_key, []).append({
                "action_key":         ak,
                "action_name":        ACTIONS.get(ak, {}).get("action_name", ak),
                "source_module":      ACTIONS.get(ak, {}).get("source_module"),
                "points_primary":     pp,
                "dimension_primary":  dp,
                "points_secondary":   ps,
                "dimension_secondary": ds,
                "logged_at":          ts.isoformat() if ts else None,
            })

        return {
            "user_id":     user_id,
            "days":        days,
            "total_logs":  len(rows),
            "logs_by_date": grouped,
        }
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 14. GET /actions/module/{module_name} ─────────────────────────────────────

@router.get("/actions/module/{module_name}")
def actions_by_module(module_name: str, _: dict = Depends(verify_token)):
    """
    Returns all actions for a specific app module.
    module_name examples: dashboard, module1, module2, module3, module4
    """
    filtered = [
        {
            "action_key":          a["action_key"],
            "action_name":         a["action_name"],
            "source_feature":      a.get("source_feature"),
            "dimension_primary":   a["dimension_primary"],
            "points_primary":      a["points_primary"],
            "dimension_secondary": a.get("dimension_secondary"),
            "points_secondary":    a.get("points_secondary"),
            "max_per_day":         a.get("max_per_day", 1),
            "notes":               a.get("notes", ""),
        }
        for a in ACTIONS.values()
        if a.get("source_module") == module_name
    ]

    if not filtered:
        raise HTTPException(404, f"No actions found for module '{module_name}'.")

    return {
        "module":  module_name,
        "total":   len(filtered),
        "actions": filtered,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── 15. POST /log_penalty ─────────────────────────────────────────────────────

class PenaltyRequest(BaseModel):
    user_id:    Optional[str] = None
    action_key: str                    # must exist in ACTIONS with points_primary < 0


@router.post("/log_penalty")
def log_penalty(body: PenaltyRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Deduct dimension points for a negative/relapse action.

    The action_key must exist in ACTIONS and have points_primary < 0 to be
    treated as a penalty.  Example: tawbah_relapse, riba_transaction.

    Returns the updated crystal_score, nafs_level, and a tawbah_streak update
    when the action is tawbah-related.
    """
    jwt_sub  = jwt_payload.get("sub", "anonymous")
    user_id  = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    action = ACTIONS.get(body.action_key)
    if not action:
        raise HTTPException(404, f"Unknown action_key: '{body.action_key}'")

    pts_primary   = int(action.get("points_primary", 0))
    dim_primary   = action.get("dimension_primary", "")
    pts_secondary = action.get("points_secondary") or 0
    dim_secondary = action.get("dimension_secondary")

    if pts_primary >= 0 and pts_secondary >= 0:
        raise HTTPException(
            400,
            f"'{body.action_key}' is not a penalty action (points are non-negative). "
            "Use /log_action for positive actions."
        )

    if dim_primary and dim_primary not in VALID_DIMENSIONS:
        raise HTTPException(400, f"Invalid dimension_primary: '{dim_primary}'")
    if dim_secondary and dim_secondary not in VALID_DIMENSIONS:
        raise HTTPException(400, f"Invalid dimension_secondary: '{dim_secondary}'")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Ensure user exists
        cur.execute("SELECT user_id FROM fitrah_users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"User '{user_id}' not found. Call /user/setup first.")

        # Fetch current dimension scores
        dim_scores = _fetch_dim_scores(cur, user_id)

        # Apply deductions (floor at 0 for each dimension)
        if dim_primary in dim_scores:
            dim_scores[dim_primary] = max(0.0, dim_scores[dim_primary] + pts_primary)
        if dim_secondary and dim_secondary in dim_scores:
            dim_scores[dim_secondary] = max(0.0, dim_scores[dim_secondary] + pts_secondary)

        new_crystal = calculate_crystal_score(dim_scores)
        new_level   = get_nafs_level(new_crystal, dim_scores.get("taqwa", 0))
        progress    = get_nafs_progress_pct(new_crystal, new_level)

        # Tawbah streak: if this is a tawbah-type relapse, reset the streak
        is_tawbah_related = "tawbah" in body.action_key.lower()
        # Persist dimension scores
        set_clauses = ", ".join(
            f"{DIM_COLUMNS[d]} = %s" for d in VALID_DIMENSIONS
        )
        vals = [dim_scores[d] for d in VALID_DIMENSIONS]

        if is_tawbah_related:
            cur.execute(
                f"""UPDATE fitrah_user_dimensions
                    SET {set_clauses}, updated_at = now()
                    WHERE user_id = %s""",
                vals + [user_id],
            )
            cur.execute(
                """UPDATE fitrah_users
                   SET crystal_score = %s,
                       current_nafs_level = %s,
                       tawbah_streak_current = 0,
                       last_active_at = now()
                   WHERE user_id = %s""",
                (new_crystal, new_level["level_key"], user_id),
            )
        else:
            cur.execute(
                f"""UPDATE fitrah_user_dimensions
                    SET {set_clauses}, updated_at = now()
                    WHERE user_id = %s""",
                vals + [user_id],
            )
            cur.execute(
                """UPDATE fitrah_users
                   SET crystal_score = %s,
                       current_nafs_level = %s,
                       last_active_at = now()
                   WHERE user_id = %s""",
                (new_crystal, new_level["level_key"], user_id),
            )

        # Log the penalty in action_logs
        cur.execute(
            """INSERT INTO fitrah_user_action_logs
               (user_id, action_key, points_primary, dimension_primary,
                points_secondary, dimension_secondary)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, body.action_key,
             pts_primary, dim_primary,
             pts_secondary if dim_secondary else None,
             dim_secondary),
        )

        conn.commit()

        response = {
            "action_key":    body.action_key,
            "action_name":   action.get("action_name", body.action_key),
            "points_applied": {
                dim_primary: pts_primary,
            },
            "crystal_score": new_crystal,
            "nafs_level": {
                "level_key":    new_level["level_key"],
                "display_name": new_level["display_name"],
                "arabic":       new_level["arabic"],
                "progress_pct": progress,
                "encouragement": new_level.get("encouragement"),
            },
            "dimension_scores": {k: round(v, 1) for k, v in dim_scores.items()},
        }
        if dim_secondary:
            response["points_applied"][dim_secondary] = pts_secondary
        if is_tawbah_related:
            response["tawbah_streak_reset"] = True

        return response

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 16. GET /user/{user_id}/deed_suggestions ─────────────────────────────────

@router.get("/user/{user_id}/deed_suggestions")
def deed_suggestions(
    user_id: str,
    limit: int = 5,
    jwt_payload: dict = Depends(verify_token),
):
    """
    Returns a list of recommended actions (deeds) tailored to the user's
    weakest dimension.  Only actions with max_per_day >= 1 are included.
    The response also surfaces the weakest and strongest dimensions.
    """
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    limit = min(max(limit, 1), 20)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM fitrah_users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"User '{user_id}' not found.")

        dim_scores = _fetch_dim_scores(cur, user_id)
        weakest    = get_weakest_dimension(dim_scores)
        strongest  = get_strongest_dimension(dim_scores)

        # Find actions whose primary dimension matches weakest, max_per_day >= 1
        suggestions = [
            {
                "action_key":          a["action_key"],
                "action_name":         a["action_name"],
                "source_module":       a.get("source_module"),
                "source_feature":      a.get("source_feature"),
                "dimension_primary":   a["dimension_primary"],
                "points_primary":      a["points_primary"],
                "dimension_secondary": a.get("dimension_secondary"),
                "points_secondary":    a.get("points_secondary"),
                "max_per_day":         a.get("max_per_day", 1),
            }
            for a in ACTIONS.values()
            if a.get("dimension_primary") == weakest
            and int(a.get("points_primary", 0)) > 0
            and int(a.get("max_per_day", 1)) >= 1
        ][:limit]

        return {
            "user_id":          user_id,
            "weakest_dimension":  weakest,
            "strongest_dimension": strongest,
            "dimension_scores": {k: round(v, 1) for k, v in dim_scores.items()},
            "suggestions":      suggestions,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 16b. POST /balance/check ──────────────────────────────────────────────────

@router.post("/balance/check")
def balance_check(user_id: Optional[str] = None, jwt_payload: dict = Depends(verify_token)):
    """
    Logs a dimension balance check (balance_check_done, +3 ILM, max 1/day).
    Returns a full dimension balance analysis: scores, ratios, recommendations.
    Flutter calls this when user taps the Balance Check feature.
    """
    jwt_sub = jwt_payload.get("sub", "anonymous")
    uid = user_id or jwt_sub
    if not uid or uid == "anonymous":
        raise HTTPException(400, "Authenticated user_id required.")
    if jwt_sub not in ("anonymous", uid):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, crystal_score FROM fitrah_users WHERE user_id = %s", (uid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        crystal = float(row[1] or 0)

        dim_scores = _fetch_dim_scores(cur, uid)
        weakest    = get_weakest_dimension(dim_scores)
        strongest  = get_strongest_dimension(dim_scores)

        # Award balance_check_done (+3 ILM) once per day
        cur.execute(
            """SELECT 1 FROM fitrah_user_action_logs
               WHERE user_id = %s AND action_key = 'balance_check_done'
                 AND logged_at >= CURRENT_DATE LIMIT 1""",
            (uid,),
        )
        points_awarded = 0
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'balance_check_done', 3, 'ilm')",
                (uid,),
            )
            # Apply the +3 ILM point to dimension table
            cur.execute(
                "UPDATE fitrah_user_dimensions SET ilm_score = LEAST(ilm_score + 3, 100), updated_at = now() WHERE user_id = %s",
                (uid,),
            )
            points_awarded = 3
        conn.commit()

        # Build balance analysis
        total = sum(dim_scores.values()) or 1
        ratios = {k: round(v / total * 100, 1) for k, v in dim_scores.items()}

        # Flag dimensions that are significantly below average
        avg = total / len(dim_scores)
        needs_attention = [k for k, v in dim_scores.items() if v < avg * 0.7]

        return {
            "user_id":         uid,
            "crystal_score":   crystal,
            "dimension_scores": {k: round(v, 1) for k, v in dim_scores.items()},
            "dimension_ratios": ratios,
            "strongest":       strongest,
            "weakest":         weakest,
            "needs_attention": needs_attention,
            "balance_status":  "balanced" if not needs_attention else "needs_work",
            "points_awarded":  points_awarded,
            "action_logged":   points_awarded > 0,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 17. POST /maqsad/nafs_message ────────────────────────────────────────────

class NafsMessageRequest(BaseModel):
    user_id:       Optional[str] = None
    new_level_key: str
    old_level_key: str


@router.post("/maqsad/nafs_message")
def nafs_message(body: NafsMessageRequest, jwt_payload: dict = Depends(verify_token)):
    """
    AI-generated personalised message for a nafs level change (up or down).
    Returns a short celebratory/encouragement message with an ayah reference.
    """
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")

    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    new_lvl = next((l for l in NAFS_LEVELS if l["level_key"] == body.new_level_key), None)
    old_lvl = next((l for l in NAFS_LEVELS if l["level_key"] == body.old_level_key), None)

    if not new_lvl or not old_lvl:
        raise HTTPException(400, "Invalid level_key value(s).")

    direction = "ascended" if new_lvl["level_order"] > old_lvl["level_order"] else "descended"

    system_prompt = (
        "You are a compassionate Islamic scholar and spiritual guide. "
        "Respond ONLY with a JSON object — no markdown, no extra text."
    )
    user_msg = (
        f"A Muslim has {direction} in their nafs level.\n"
        f"Old level: {old_lvl['display_name']} ({old_lvl['arabic']})\n"
        f"New level: {new_lvl['display_name']} ({new_lvl['arabic']})\n\n"
        "Return a JSON object with exactly these keys:\n"
        '  "message": a warm, personal 2-3 sentence message in English acknowledging this change\n'
        '  "arabic_dua": a short Arabic dua or ayah relevant to this transition\n'
        '  "dua_translation": English translation of the dua/ayah\n'
        '  "ayah_reference": surah and ayah number (e.g., "Al-Fajr 89:27-28")\n'
        '  "action_tip": one concrete deed they can do today to maintain/regain momentum'
    )

    try:
        resp = anthropic_client.messages.create(
            model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
            max_tokens=_MAQSAD_CFG.get("max_tokens", 800),
            messages=[{"role": "user", "content": user_msg}],
            system=system_prompt,
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "message":        _FALLBACKS.get("maqsad_generation_failed", "Keep striving."),
            "arabic_dua":     "",
            "dua_translation": "",
            "ayah_reference": "",
            "action_tip":     "",
        }
    except Exception as e:
        raise HTTPException(502, f"AI service error: {e}")

    return {
        "user_id":       user_id,
        "direction":     direction,
        "old_level_key": body.old_level_key,
        "new_level_key": body.new_level_key,
        **result,
    }


# ── 18. POST /maqsad/streak_break ────────────────────────────────────────────

class StreakBreakRequest(BaseModel):
    user_id:        Optional[str] = None
    streak_lost:    int            # how many days the streak was before it broke
    days_inactive:  int = 1        # how many days inactive


@router.post("/maqsad/streak_break")
def streak_break_message(body: StreakBreakRequest, jwt_payload: dict = Depends(verify_token)):
    """
    AI-generated encouragement message when a user's streak is broken.
    Motivates them to start fresh without shame.
    """
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")

    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    system_prompt = (
        "You are a compassionate Islamic life coach who understands that tawbah "
        "(repentance and return) is central to Islamic spirituality. "
        "Respond ONLY with a JSON object — no markdown, no extra text."
    )
    user_msg = (
        f"A Muslim's streak of {body.streak_lost} consecutive days has been broken "
        f"after {body.days_inactive} inactive day(s).\n\n"
        "Return a JSON object with exactly these keys:\n"
        '  "message": a warm, non-shaming 2-3 sentence encouragement in English '
        "reminding them that every day is a fresh start in Islam\n"
        '  "arabic_quote": a relevant Arabic hadith or ayah about tawbah or fresh starts\n'
        '  "quote_translation": English translation\n'
        '  "quote_reference": source reference\n'
        '  "restart_tip": one simple action they can do RIGHT NOW to restart their streak'
    )

    try:
        resp = anthropic_client.messages.create(
            model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
            max_tokens=_MAQSAD_CFG.get("max_tokens", 600),
            messages=[{"role": "user", "content": user_msg}],
            system=system_prompt,
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "message":           _FALLBACKS.get("maqsad_generation_failed", "Keep going."),
            "arabic_quote":      "",
            "quote_translation": "",
            "quote_reference":   "",
            "restart_tip":       "",
        }
    except Exception as e:
        raise HTTPException(502, f"AI service error: {e}")

    return {
        "user_id":       user_id,
        "streak_lost":   body.streak_lost,
        "days_inactive": body.days_inactive,
        **result,
    }


# ── 19. GET /user/{user_id}/weekly_summary ───────────────────────────────────

@router.get("/user/{user_id}/weekly_summary")
def weekly_summary(
    user_id: str,
    jwt_payload: dict = Depends(verify_token),
):
    """
    Returns a 7-day summary of dimension points earned, total actions logged,
    most active dimension, and daily breakdown.
    """
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM fitrah_users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"User '{user_id}' not found.")

        cur.execute(
            """SELECT action_key, points_primary, dimension_primary,
                      points_secondary, dimension_secondary, logged_at
               FROM fitrah_user_action_logs
               WHERE user_id = %s
                 AND logged_at >= now() - INTERVAL '7 days'
                 AND points_primary > 0
               ORDER BY logged_at ASC""",
            (user_id,),
        )
        rows = cur.fetchall()

        dim_totals: dict[str, float] = {d: 0.0 for d in VALID_DIMENSIONS}
        daily:      dict[str, dict]  = {}

        for ak, pp, dp, ps, ds, ts in rows:
            if ts:
                day = ts.date().isoformat()
                daily.setdefault(day, {"actions": 0, "points": {d: 0.0 for d in VALID_DIMENSIONS}})
                daily[day]["actions"] += 1
                if dp and dp in dim_totals:
                    dim_totals[dp] += float(pp or 0)
                    daily[day]["points"][dp] += float(pp or 0)
                if ds and ds in dim_totals:
                    dim_totals[ds] += float(ps or 0)
                    daily[day]["points"][ds] += float(ps or 0)

        most_active_dim = max(dim_totals, key=dim_totals.get) if any(dim_totals.values()) else None

        return {
            "user_id":          user_id,
            "period_days":      7,
            "total_actions":    len(rows),
            "dimension_totals": {k: round(v, 1) for k, v in dim_totals.items()},
            "most_active_dimension": most_active_dim,
            "daily_breakdown":  {
                day: {
                    "actions": data["actions"],
                    "points":  {k: round(v, 1) for k, v in data["points"].items()},
                }
                for day, data in sorted(daily.items())
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 19b. POST /maqsad/weekly_summary — AI Jumuah insight ─────────────────────

@router.post("/maqsad/weekly_summary")
def maqsad_weekly_summary(user_id: Optional[str] = None, jwt_payload: dict = Depends(verify_token)):
    """
    AI-powered Jumuah weekly insight — 2 lines Urdu:
      line_1: this week's biggest spiritual win
      line_2: one focus for next week
    Uses weekly_dimension_summary prompt from maqsad_engine_prompts.json.
    """
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")

    jwt_sub = jwt_payload.get("sub", "anonymous")
    uid = user_id or jwt_sub
    if jwt_sub not in ("anonymous", uid):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM fitrah_users WHERE user_id = %s", (uid,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found.")

        # Aggregate 7-day dimension totals
        cur.execute(
            """SELECT dimension_primary, SUM(points_primary),
                      dimension_secondary, SUM(points_secondary)
               FROM fitrah_user_action_logs
               WHERE user_id = %s AND logged_at >= now() - INTERVAL '7 days'
                 AND points_primary > 0
               GROUP BY dimension_primary, dimension_secondary""",
            (uid,),
        )
        dim_totals: dict[str, float] = {d: 0.0 for d in VALID_DIMENSIONS}
        for dp, pp, ds, ps in cur.fetchall():
            if dp and dp in dim_totals:
                dim_totals[dp] += float(pp or 0)
            if ds and ds in dim_totals:
                dim_totals[ds] += float(ps or 0)

        strongest = max(dim_totals, key=dim_totals.get)
        weakest   = min(dim_totals, key=dim_totals.get)

        _tmpl = _ADDITIONAL_CALLS["weekly_dimension_summary"]["simple_prompt"]
        user_msg = _fill_template(_tmpl, {
            "t":        str(round(dim_totals.get("taqwa",   0), 1)),
            "i":        str(round(dim_totals.get("ilm",     0), 1)),
            "tz":       str(round(dim_totals.get("tazkiya", 0), 1)),
            "ih":       str(round(dim_totals.get("ihsan",   0), 1)),
            "n":        str(round(dim_totals.get("nafs",    0), 1)),
            "m":        str(round(dim_totals.get("maal",    0), 1)),
            "strongest": strongest,
            "weakest":   weakest,
        })

        try:
            resp = anthropic_client.messages.create(
                model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
                max_tokens=300,
                messages=[{"role": "user", "content": user_msg}],
                system="Tum ek Islamic roohani guide ho. Sirf valid JSON return karo — no markdown.",
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw)
        except Exception:
            result = {
                "line_1": "Is hafte apne aamaal par nazar daalen — Allah ne dekha.",
                "line_2": "Agle hafte ek dimension choose karen aur uspe focus karen.",
            }

        return {
            "user_id":           uid,
            "dimension_totals":  {k: round(v, 1) for k, v in dim_totals.items()},
            "strongest":         strongest,
            "weakest":           weakest,
            **result,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ══════════════════════════════════════════════════════════════════════════════
# FITRAH OS — EXTENDED SYSTEMS
# ══════════════════════════════════════════════════════════════════════════════

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_spiritual_ctx(cur, user_id: str) -> dict:
    cur.execute(
        """SELECT crystal_score, crystal_prev, streak_current,
                  tawbah_streak_current, spiritual_state
           FROM fitrah_users WHERE user_id = %s""",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    cur.execute(
        """SELECT 1 FROM fitrah_user_action_logs
           WHERE user_id = %s AND points_primary < 0
             AND logged_at >= now() - INTERVAL '7 days'
           LIMIT 1""",
        (user_id,),
    )
    return {
        "crystal_score":         float(row[0] or 0),
        "crystal_prev":          float(row[1] or 0),
        "streak_current":        int(row[2] or 0),
        "tawbah_streak_current": int(row[3] or 0),
        "recent_penalty":        cur.fetchone() is not None,
        "stored_state":          row[4] or "seeking",
    }


def _compute_spiritual_state(cur, user_id: str, dim_scores: dict) -> str:
    """Computes suggested spiritual state — does NOT save to DB (user must confirm)."""
    ctx = _get_spiritual_ctx(cur, user_id)
    if not ctx:
        return "seeking"
    return determine_spiritual_state(
        crystal_score=ctx["crystal_score"],
        dim_scores=dim_scores,
        streak_current=ctx["streak_current"],
        tawbah_streak_current=ctx["tawbah_streak_current"],
        recent_penalty=ctx["recent_penalty"],
        crystal_prev=ctx["crystal_prev"],
    )


# ── 20. GET /user/{user_id}/spiritual_state ───────────────────────────────────

@router.get("/user/{user_id}/spiritual_state")
def get_spiritual_state(user_id: str, jwt_payload: dict = Depends(verify_token)):
    """
    Current Spiritual State (one of 7) + nafs level.
    Combined display: 'Nafs Level: Mutmainnah | Spiritual State: Rising \U0001f331'
    """
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM fitrah_users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found.")
        dim_scores = _fetch_dim_scores(cur, user_id)
        state      = _compute_spiritual_state(cur, user_id, dim_scores)
        meta       = get_spiritual_state_meta(state)
        cur.execute("SELECT crystal_score FROM fitrah_users WHERE user_id = %s", (user_id,))
        row     = cur.fetchone()
        crystal = float(row[0] or 0) if row else 0.0
        level   = get_nafs_level(crystal, dim_scores["taqwa"])
        return {
            "user_id": user_id,
            "suggested_state": {
                "state_key": state, "label": meta["label"],
                "tone": meta["tone"], "urdu": meta["urdu"], "emoji": meta["emoji"],
            },
            "nafs_level": {
                "level_key":     level["level_key"],
                "display_name":  level["display_name"],
                "arabic":        level["arabic"],
                "encouragement": level.get("encouragement"),
            },
            "combined_display": (
                f"Nafs Level: {level['display_name']} | "
                f"Spiritual State: {meta['label']} {meta['emoji']}"
            ),
            "note": "Suggestion only — use POST /api/fitrah/spiritual_state/confirm to save your state.",
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 20b. POST /spiritual_state/confirm ───────────────────────────────────────

_VALID_SPIRITUAL_STATES = frozenset([
    "seeking", "struggling", "healing", "rising", "serving", "consistent", "recovering"
])

class SpiritualStateConfirmRequest(BaseModel):
    user_id:   Optional[str] = None
    state_key: str   # one of the 7 states, or "ask_later" to defer


@router.post("/spiritual_state/confirm")
def confirm_spiritual_state(body: SpiritualStateConfirmRequest, jwt_payload: dict = Depends(verify_token)):
    """
    User confirms their spiritual state (or defers with 'ask_later').
    Saves confirmed state to fitrah_users + records spiritual_state_confirmed_at.
    """
    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    if body.state_key == "ask_later":
        return {"user_id": user_id, "status": "deferred", "message": "Koi baat nahi — baad mein try karo."}

    if body.state_key not in _VALID_SPIRITUAL_STATES:
        raise HTTPException(400, f"Invalid state_key. Choose from: {sorted(_VALID_SPIRITUAL_STATES)}")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, spiritual_state_confirmed_at FROM fitrah_users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        # PDF §09: max 1 state change per week (stability important)
        last_confirmed = row[1]
        if last_confirmed:
            if last_confirmed.tzinfo is None:
                last_confirmed = last_confirmed.replace(tzinfo=timezone.utc)
            days_since = (datetime.now(timezone.utc) - last_confirmed).days
            if days_since < 7:
                return {
                    "user_id":  user_id,
                    "status":   "rate_limited",
                    "message":  f"Spiritual state sirf 1 baar per hafte change ho sakti hai. {7 - days_since} din baaki hain.",
                    "next_change_in_days": 7 - days_since,
                }
        cur.execute(
            """UPDATE fitrah_users
               SET spiritual_state = %s, spiritual_state_confirmed_at = now()
               WHERE user_id = %s""",
            (body.state_key, user_id),
        )
        # Log spiritual_state_confirmed → TAZKIYA +3 (1/week per PDF §18)
        cur.execute(
            """INSERT INTO fitrah_user_action_logs
               (user_id, action_key, points_primary, dimension_primary)
               VALUES (%s, 'spiritual_state_confirmed', 3, 'tazkiya')""",
            (user_id,),
        )
        conn.commit()
        meta = get_spiritual_state_meta(body.state_key)
        return {
            "user_id":    user_id,
            "state_key":  body.state_key,
            "label":      meta["label"],
            "urdu":       meta["urdu"],
            "emoji":      meta["emoji"],
            "message":    f"State confirmed: {meta['label']} {meta['emoji']}",
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 21. POST /qalb/log ────────────────────────────────────────────────────────

# Qalb states aligned with DB patch v2 (fitrah_qalb_state_history)
_QALB_STATES = {
    "hard_heart":  {"urdu": "Dil sakht hai",        "ai_opener": "Dil sakht lagta hai — kya hua aaj? Kuch tha jo takleef di?"},
    "soft_heart":  {"urdu": "Dil naram hai",         "ai_opener": "Al-hamdulillah — naram dil Allah ki rahmat hai. Kya share karna chahenge?"},
    "distracted":  {"urdu": "Dil bhatka hua hai",    "ai_opener": "Tawajjuh bikhri hui hai — kya cheez hai jo zehen mein ghoom rahi hai?"},
    "ghafil":      {"urdu": "Ghaflat mein hoon",     "ai_opener": "Ghaflat aam hai — koi baat nahi. Kya ek choti si baat share karen?"},
    "present":     {"urdu": "Dil haazir hai",        "ai_opener": "MashAllah — dil haazir hai aaj. Kya iss lamhe mein kuch hai jo batana hai?"},
    "broken":      {"urdu": "Dil toota hua hai",     "ai_opener": "Allah jaanta hai jo tum nahi keh sakte — batao, yahan safe ho."},
    "hopeful":     {"urdu": "Umeed hai",             "ai_opener": "Umeed hai — kya plan hai aage ka?"},
}
VALID_QALB_STATES = frozenset(_QALB_STATES.keys())

VALID_EMOTIONAL_STATES = frozenset(
    ["calm", "anxious", "happy", "sad", "angry", "grateful", "disconnected"]
)


class QalbLogRequest(BaseModel):
    user_id:         Optional[str] = None
    qalb_state:      str                      # one of 7 spiritual states
    emotional_state: Optional[str] = None     # calm/anxious/happy/sad/angry/grateful/disconnected
    notes:           Optional[str] = None


def _pick_opening_line(qalb_state: str, last_line_id: str | None) -> dict:
    """
    Pick a rotating opening line from qalb_state_opening_lines.json.
    Avoids repeating the last used line (least-recently-used strategy).
    Falls back to static meta opener if JSON not loaded or state not found.
    """
    state_data = next(
        (s for s in _qalb_opening_lines.get("states", [])
         if s["qalb_state_key"] == qalb_state),
        None,
    )
    if not state_data:
        meta = _QALB_STATES.get(qalb_state, {})
        return {"line_id": None, "line_ur": meta.get("ai_opener", "Assalamu Alaikum — kya haal hai?"), "tone": "warm"}

    lines = state_data.get("opening_lines", [])
    if not lines:
        meta = _QALB_STATES.get(qalb_state, {})
        return {"line_id": None, "line_ur": meta.get("ai_opener", ""), "tone": "warm"}

    # Filter out the last used line to avoid immediate repeat
    candidates = [l for l in lines if l.get("line_id") != last_line_id]
    if not candidates:
        candidates = lines  # all used → reset rotation

    chosen = random.choice(candidates)
    return {"line_id": chosen.get("line_id"), "line_ur": chosen.get("line_ur", ""), "tone": chosen.get("tone", "warm")}


@router.post("/qalb/log")
def log_qalb_state(body: QalbLogRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Log today's Qalb state (one of 7). Returns rotating AI opener for Akhlaq AI chat.
    - Upserts one record per user per day in fitrah_qalb_state_history.
    - Tracks consecutive ghafil days on fitrah_users (used by spiritual state engine).
    - Awards qalb_state_logged (+3 TAZKIYA) once per day.
    """
    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")
    if body.qalb_state not in VALID_QALB_STATES:
        raise HTTPException(400, f"Invalid qalb_state. Must be one of: {sorted(VALID_QALB_STATES)}")
    if body.emotional_state and body.emotional_state not in VALID_EMOTIONAL_STATES:
        raise HTTPException(400, f"Invalid emotional_state. Choose from: {sorted(VALID_EMOTIONAL_STATES)}")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_qalb_line_id, consecutive_ghafil_days FROM fitrah_users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        last_line_id         = row[0]
        consecutive_ghafil   = int(row[1] or 0)

        # Update consecutive ghafil day counter
        if body.qalb_state == "ghafil":
            consecutive_ghafil += 1
        else:
            consecutive_ghafil = 0  # reset on any non-ghafil state

        # Pick rotating opening line from JSON config
        chosen_line = _pick_opening_line(body.qalb_state, last_line_id)

        # Upsert — one entry per user per day
        cur.execute(
            """INSERT INTO fitrah_qalb_state_history
               (user_id, qalb_state, emotional_state, logged_date, context_note, line_id_used)
               VALUES (%s, %s, %s, CURRENT_DATE, %s, %s)
               ON CONFLICT (user_id, logged_date)
               DO UPDATE SET qalb_state      = EXCLUDED.qalb_state,
                             emotional_state = EXCLUDED.emotional_state,
                             context_note    = EXCLUDED.context_note,
                             line_id_used    = EXCLUDED.line_id_used""",
            (user_id, body.qalb_state, body.emotional_state, body.notes, chosen_line["line_id"]),
        )

        # Update user profile: qalb state, last line used, ghafil counter
        cur.execute(
            """UPDATE fitrah_users
               SET last_qalb_state         = %s,
                   last_qalb_state_logged  = CURRENT_DATE,
                   last_qalb_line_id       = %s,
                   consecutive_ghafil_days = %s
               WHERE user_id = %s""",
            (body.qalb_state, chosen_line["line_id"], consecutive_ghafil, user_id),
        )

        # Award qalb_state_logged (+3 TAZKIYA) once per day
        cur.execute(
            """SELECT 1 FROM fitrah_user_action_logs
               WHERE user_id = %s AND action_key = 'qalb_state_logged'
                 AND logged_at >= CURRENT_DATE LIMIT 1""",
            (user_id,),
        )
        points_awarded = 0
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'qalb_state_logged', 3, 'tazkiya')",
                (user_id,),
            )
            points_awarded = 3
            if body.emotional_state:
                cur.execute(
                    "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'emotional_state_logged', 3, 'tazkiya')",
                    (user_id,),
                )
                points_awarded += 3

        conn.commit()

        meta = _QALB_STATES[body.qalb_state]
        resp: dict = {
            "qalb_state":            body.qalb_state,
            "urdu_label":            meta["urdu"],
            "opening_line":          chosen_line["line_ur"],
            "opening_line_id":       chosen_line["line_id"],
            "opening_line_tone":     chosen_line["tone"],
            "consecutive_ghafil":    consecutive_ghafil,
            "points_awarded":        points_awarded,
            "action":                "open_akhlaq_ai_chat",
        }
        if _is_crisis_situation("", body.qalb_state):
            resp["crisis_ayah"] = _crisis_ayah()
            resp["crisis_safe"] = True
        elif body.qalb_state == "hopeful":
            # Serve an encouraging tazkiya/ihsan ayah to amplify the hopeful state
            resp["encouragement_ayah"] = _smart_ayah("tazkiya", "hopeful")
        return resp
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 22. GET /user/{user_id}/qalb_history ─────────────────────────────────────

@router.get("/user/{user_id}/qalb_history")
def qalb_history(user_id: str, days: int = 7, jwt_payload: dict = Depends(verify_token)):
    """Last N days of qalb state logs (default 7, max 30)."""
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")
    days = min(max(days, 1), 30)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT qalb_state, emotional_state, context_note, logged_date
               FROM fitrah_qalb_state_history
               WHERE user_id = %s AND logged_date >= CURRENT_DATE - INTERVAL '%s days'
               ORDER BY logged_date DESC""",
            (user_id, days),
        )
        rows = cur.fetchall()
        return {
            "user_id": user_id, "days": days,
            "logs": [
                {
                    "qalb_state":      r[0],
                    "urdu_label":      _QALB_STATES.get(r[0], {}).get("urdu", r[0]),
                    "emotional_state": r[1],
                    "context_note":    r[2],
                    "logged_date":     r[3].isoformat() if r[3] else None,
                }
                for r in rows
            ],
        }
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 23. POST /battlefield/analyze ────────────────────────────────────────────

class BattlefieldRequest(BaseModel):
    user_id:       Optional[str] = None
    struggle_text: Optional[str] = Field(None, max_length=2000)


@router.post("/battlefield/analyze")
@limiter.limit("10/minute")
def battlefield_analyze(request: Request, body: BattlefieldRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Nafs Battlefield Visualizer. AI identifies 4 forces from recent activity.
    Awards TAZKIYA +5 (nafs_battlefield_session).
    """
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")
    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    # PDF §23 Safety Check A — crisis keywords override
    if body.struggle_text and check_crisis(body.struggle_text):
        from fitrah_engine.fitrah_middleware import CRISIS_RESOURCE_TEXT
        return {
            "user_id":        user_id,
            "crisis":         True,
            "crisis_message": CRISIS_RESOURCE_TEXT,
        }

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM fitrah_users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found.")
        cur.execute(
            """SELECT action_key, dimension_primary FROM fitrah_user_action_logs
               WHERE user_id = %s AND logged_at >= now() - INTERVAL '7 days'
               ORDER BY logged_at DESC LIMIT 20""",
            (user_id,),
        )
        recent = [{"action_key": r[0], "dimension": r[1]} for r in cur.fetchall()]
        dim_scores = _fetch_dim_scores(cur, user_id)
        weakest    = get_weakest_dimension(dim_scores)

        ctx = f"Weakest dimension: {weakest}\nRecent actions: {recent[:10]}\n"
        if body.struggle_text:
            ctx = f"User says: \"{body.struggle_text}\"\n\n" + ctx

        user_msg = (
            ctx + "\nIdentify the 4 forces active right now. Return JSON:\n"
            "{\n"
            "  \"forces\": {\n"
            "    \"nafs\":    {\"level\": 1-10, \"label\": \"...\"},\n"
            "    \"aql\":     {\"level\": 1-10, \"label\": \"...\"},\n"
            "    \"qalb\":    {\"level\": 1-10, \"label\": \"...\"},\n"
            "    \"shaytan\": {\"level\": 1-10, \"label\": \"...\"}\n"
            "  },\n"
            "  \"battle_summary\": \"one sentence\",\n"
            "  \"intervention\": {\n"
            "    \"ayah_arabic\": \"...\", \"ayah_translation\": \"...\", \"ayah_reference\": \"...\",\n"
            "    \"hadith\": \"...\", \"micro_action\": \"one thing under 2 minutes\"\n"
            "  }\n"
            "}"
        )
        _fallback = {
            "forces": {
                "nafs":    {"level": 5, "label": "Desires pulling"},
                "aql":     {"level": 5, "label": "Reason resisting"},
                "qalb":    {"level": 5, "label": "Heart seeking clarity"},
                "shaytan": {"level": 5, "label": "Whispers of delay"},
            },
            "battle_summary": "The battlefield is active — return to Allah.",
            "intervention": {
                "ayah_arabic":      "\u0648\u064e\u0645\u064e\u0646 \u064a\u064e\u062a\u064e\u0651\u0642\u0650 \u0627\u0644\u0644\u064e\u0651\u0647\u064e \u064a\u064e\u062c\u0652\u0639\u064e\u0644 \u0644\u064e\u0651\u0647\u064f \u0645\u064e\u062e\u0652\u0631\u064e\u062c\u064b\u0627",
                "ayah_translation": "Whoever fears Allah, He will make a way out for them.",
                "ayah_reference":   "Al-Talaq 65:2",
                "hadith":           "The strong person controls himself in anger. (Bukhari)",
                "micro_action":     "Say A'udhu billahi 3 times and take 3 deep breaths.",
            },
        }
        try:
            resp = anthropic_client.messages.create(
                model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
                max_tokens=900,
                messages=[{"role": "user", "content": user_msg}],
                system="You are an Islamic spiritual psychologist. Respond ONLY with valid JSON — no markdown.",
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            ai_result = json.loads(raw)
        except Exception:
            ai_result = _fallback

        cur.execute(
            "INSERT INTO fitrah_battlefield_sessions (user_id, forces, intervention) VALUES (%s, %s, %s)",
            (user_id, json.dumps(ai_result.get("forces", {})), json.dumps(ai_result.get("intervention", {}))),
        )
        dim_scores["tazkiya"] = min(100.0, dim_scores["tazkiya"] + 5)
        new_crystal = calculate_crystal_score(dim_scores)
        new_level   = get_nafs_level(new_crystal, dim_scores["taqwa"])
        sc = ", ".join(f"{DIM_COLUMNS[d]} = %s" for d in VALID_DIMENSIONS)
        cur.execute(f"UPDATE fitrah_user_dimensions SET {sc}, updated_at=now() WHERE user_id=%s",
                    [dim_scores[d] for d in VALID_DIMENSIONS] + [user_id])
        cur.execute("UPDATE fitrah_users SET crystal_score=%s, current_nafs_level=%s WHERE user_id=%s",
                    (new_crystal, new_level["level_key"], user_id))
        cur.execute(
            "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'nafs_battlefield_session', 5, 'tazkiya')",
            (user_id,),
        )
        conn.commit()

        # Run middleware on free-form text fields
        battle_summary = ai_result.get("battle_summary", "")
        intervention   = ai_result.get("intervention", {})
        if battle_summary:
            battle_summary, _ = _run_middleware(
                user_id, battle_summary,
                last_user_message=body.struggle_text or "",
                action_key="nafs_battlefield_session",
            )
        for _fld in ("ayah_translation", "hadith", "micro_action"):
            if intervention.get(_fld):
                _safe, _ = _run_middleware(
                    user_id, intervention[_fld],
                    last_user_message=body.struggle_text or "",
                    action_key="nafs_battlefield_session",
                )
                intervention[_fld] = _safe

        return {
            "user_id":        user_id,
            "battle_summary": battle_summary,
            "forces":         ai_result.get("forces", {}),
            "intervention":   intervention,
            "points_awarded": {"tazkiya": 5},
            "crystal_score":  new_crystal,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 24. POST /barakah/session/start ──────────────────────────────────────────

class BarakahStartRequest(BaseModel):
    user_id:          Optional[str] = None
    task_description: Optional[str] = None
    niyyah_confirmed: bool = False
    dimension_key:    str  = "taqwa"


@router.post("/barakah/session/start")
def barakah_session_start(body: BarakahStartRequest, jwt_payload: dict = Depends(verify_token)):
    """Start a Barakah Time session after niyyah. Returns session_id."""
    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")
    if body.dimension_key not in VALID_DIMENSIONS:
        raise HTTPException(400, f"Invalid dimension_key. Choose from: {sorted(VALID_DIMENSIONS)}")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT spiritual_state FROM fitrah_users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        cur.execute(
            """INSERT INTO fitrah_barakah_sessions
               (user_id, task_description, niyyah_confirmed, dimension_key, spiritual_state)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (user_id, body.task_description, body.niyyah_confirmed,
             body.dimension_key, row[0] or "seeking"),
        )
        session_id = cur.fetchone()[0]
        conn.commit()
        return {
            "session_id":       session_id,
            "niyyah_confirmed": body.niyyah_confirmed,
            "dimension_key":    body.dimension_key,
            "message": "Bismillah — kaam shuru karo." if body.niyyah_confirmed else "Session started.",
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 25. POST /barakah/session/complete ───────────────────────────────────────

class BarakahCompleteRequest(BaseModel):
    session_id:        int
    user_id:           Optional[str] = None
    focus_level:       int   # 1-5
    distraction_level: int   # 1-5


@router.post("/barakah/session/complete")
def barakah_session_complete(body: BarakahCompleteRequest, jwt_payload: dict = Depends(verify_token)):
    """Complete Barakah session. HIGH>=70=+8pts, MEDIUM>=40=+6pts, LOW=+4pts."""
    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")
    if not (1 <= body.focus_level <= 5) or not (1 <= body.distraction_level <= 5):
        raise HTTPException(400, "focus_level and distraction_level must be 1-5.")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, niyyah_confirmed, dimension_key, spiritual_state, completed_at FROM fitrah_barakah_sessions WHERE id = %s",
            (body.session_id,),
        )
        s = cur.fetchone()
        if not s:
            raise HTTPException(404, "Session not found.")
        if s[0] != user_id:
            raise HTTPException(403, "Session does not belong to you.")
        if s[4] is not None:
            raise HTTPException(400, "Session already completed.")

        bs  = calculate_barakah_score(bool(s[1]), body.focus_level, body.distraction_level, s[3] or "seeking")
        pts = barakah_to_points(bs)
        dk  = s[2] or "taqwa"

        dim_scores = _fetch_dim_scores(cur, user_id)
        dim_scores[dk] = min(100.0, dim_scores[dk] + pts)
        nc  = calculate_crystal_score(dim_scores)
        nl  = get_nafs_level(nc, dim_scores["taqwa"])
        sc  = ", ".join(f"{DIM_COLUMNS[d]} = %s" for d in VALID_DIMENSIONS)
        cur.execute(f"UPDATE fitrah_user_dimensions SET {sc}, updated_at=now() WHERE user_id=%s",
                    [dim_scores[d] for d in VALID_DIMENSIONS] + [user_id])
        cur.execute("UPDATE fitrah_users SET crystal_score=%s, current_nafs_level=%s WHERE user_id=%s",
                    (nc, nl["level_key"], user_id))
        cur.execute(
            "UPDATE fitrah_barakah_sessions SET focus_level=%s, distraction_level=%s, barakah_score=%s, points_awarded=%s, completed_at=now() WHERE id=%s",
            (body.focus_level, body.distraction_level, bs, pts, body.session_id),
        )
        cur.execute(
            "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, %s, %s, %s)",
            (user_id, "barakah_time_tracked_high" if bs >= 70 else "barakah_time_tracked_low", pts, dk),
        )
        conn.commit()
        q = "HIGH" if bs >= 70 else ("MEDIUM" if bs >= 40 else "LOW")
        return {
            "session_id": body.session_id, "barakah_score": bs, "barakah_quality": q,
            "points_awarded": pts, "dimension_key": dk, "crystal_score": nc,
            "message": f"Barakah {q} — {pts} {dk} points.",
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 25b. POST /barakah/track ─────────────────────────────────────────────────
# Single-step endpoint (JS v3 style): niyyah + focus + distraction → score → points → done.
# Replaces the two-step start/complete flow for simple use-cases.

@router.post("/barakah/track")
def barakah_track(body: BarakahTrackRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Single-step Barakah tracker (v3 API).
    1. Reads user's current spiritual_state.
    2. Computes barakah_score from niyyah + focus + distraction + state.
    3. Awards points to dimension_key.
    4. Stores session in fitrah_barakah_sessions.
    5. Updates barakah_score_today on fitrah_users.
    """
    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")
    if body.dimension_key not in VALID_DIMENSIONS:
        raise HTTPException(400, f"Invalid dimension_key. Choose from: {sorted(VALID_DIMENSIONS)}")
    if not (1 <= body.focus_level <= 5):
        raise HTTPException(400, "focus_level must be 1-5.")
    if not (1 <= body.distraction_level <= 5):
        raise HTTPException(400, "distraction_level must be 1-5.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT spiritual_state FROM fitrah_users WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        spiritual_state = row[0] or "seeking"

        bs  = calculate_barakah_score(
            body.niyyah_confirmed, body.focus_level, body.distraction_level, spiritual_state
        )
        pts = barakah_to_points(bs)

        # Award points to the chosen dimension
        pts_actual = _apply_points(cur, user_id, body.dimension_key, pts) if pts > 0 else 0

        # Log barakah session (completed immediately)
        cur.execute(
            """INSERT INTO fitrah_barakah_sessions
               (user_id, task_description, niyyah_confirmed, focus_level, distraction_level,
                spiritual_state, dimension_key, barakah_score, points_awarded,
                started_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
               RETURNING id""",
            (user_id, body.task_description, body.niyyah_confirmed,
             body.focus_level, body.distraction_level,
             spiritual_state, body.dimension_key, bs, pts_actual),
        )
        session_id = cur.fetchone()[0]

        # Update barakah_score_today (running average)
        cur.execute(
            """UPDATE fitrah_users
               SET barakah_score_today = ROUND(
                   (COALESCE(barakah_score_today, 0) + %s) / 2.0, 1
               )
               WHERE user_id = %s""",
            (bs, user_id),
        )

        # Log to action log for streak/dimension tracking
        action_key = "barakah_time_tracked_high" if bs >= 70 else "barakah_time_tracked_low"
        cur.execute(
            "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, %s, %s, %s)",
            (user_id, action_key, pts_actual, body.dimension_key),
        )

        conn.commit()

        quality = "HIGH" if bs >= 70 else ("MEDIUM" if bs >= 40 else "LOW")
        return {
            "session_id":      session_id,
            "barakah_score":   bs,
            "barakah_quality": quality,
            "spiritual_state": spiritual_state,
            "points_awarded":  pts_actual,
            "dimension_key":   body.dimension_key,
            "niyyah":          body.niyyah_confirmed,
            "message": (
                f"Barakah {quality} — +{pts_actual} {body.dimension_key.upper()} points. "
                "Innama al-amal bi-n-niyyaat."
            ) if body.niyyah_confirmed else "No niyyah — 0 points. Actions are by intention.",
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.error(f"barakah_track error for {user_id}: {e}")
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 26. GET /user/{user_id}/barakah_report ───────────────────────────────────

@router.get("/user/{user_id}/barakah_report")
def barakah_report(user_id: str, jwt_payload: dict = Depends(verify_token)):
    """Weekly Barakah report — best day, avg score, total sessions."""
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT barakah_score, dimension_key, completed_at FROM fitrah_barakah_sessions WHERE user_id=%s AND completed_at >= now() - INTERVAL '7 days' ORDER BY completed_at ASC",
            (user_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return {"user_id": user_id, "period_days": 7, "total_sessions": 0}
        daily: dict = {}
        for bs, dk, ca in rows:
            if ca:
                daily.setdefault(ca.date().isoformat(), []).append(float(bs or 0))
        avg_by_day = {d: round(sum(v) / len(v), 1) for d, v in daily.items()}
        best_day   = max(avg_by_day, key=avg_by_day.get) if avg_by_day else None
        avg_score  = round(sum(float(r[0] or 0) for r in rows) / len(rows), 1)
        return {
            "user_id": user_id, "period_days": 7,
            "total_sessions": len(rows), "avg_barakah_score": avg_score,
            "best_day": best_day, "best_day_score": avg_by_day.get(best_day) if best_day else None,
            "daily_averages": avg_by_day,
            "summary": f"Is hafte aapka highest barakah wala din {best_day} tha." if best_day else "Koi session complete nahi hua.",
        }
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 27. GET /user/{user_id}/resilience ───────────────────────────────────────

@router.get("/user/{user_id}/resilience")
def spiritual_resilience(user_id: str, jwt_payload: dict = Depends(verify_token)):
    """
    Spiritual Resilience Engine — relapse/recovery tracker.
    3+ recoveries: awards spiritual_resilience_milestone (+10 TAZKIYA, once per day).
    """
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT tawbah_streak_current FROM fitrah_users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        tawbah_streak = int(row[0] or 0)
        cur.execute("SELECT logged_at FROM fitrah_user_action_logs WHERE user_id=%s AND points_primary<0 ORDER BY logged_at ASC", (user_id,))
        penalty_ts = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT logged_at FROM fitrah_user_action_logs WHERE user_id=%s AND points_primary>0 ORDER BY logged_at ASC", (user_id,))
        positive_ts = [r[0] for r in cur.fetchall()]
        relapses = [
            {"relapse_at": pt, "recovered_at": next((p for p in positive_ts if p > pt), None)}
            for pt in penalty_ts
        ]
        result = calculate_resilience_score(relapses)
        milestone_awarded = False
        if result["recovered_count"] >= 3:
            cur.execute(
                "SELECT 1 FROM fitrah_user_action_logs WHERE user_id=%s AND action_key='spiritual_resilience_milestone' AND logged_at >= CURRENT_DATE LIMIT 1",
                (user_id,),
            )
            if not cur.fetchone():
                dim_scores = _fetch_dim_scores(cur, user_id)
                dim_scores["tazkiya"] = min(100.0, dim_scores["tazkiya"] + 10)
                nc = calculate_crystal_score(dim_scores)
                nl = get_nafs_level(nc, dim_scores["taqwa"])
                sc = ", ".join(f"{DIM_COLUMNS[d]} = %s" for d in VALID_DIMENSIONS)
                cur.execute(f"UPDATE fitrah_user_dimensions SET {sc}, updated_at=now() WHERE user_id=%s",
                            [dim_scores[d] for d in VALID_DIMENSIONS] + [user_id])
                cur.execute("UPDATE fitrah_users SET crystal_score=%s, current_nafs_level=%s WHERE user_id=%s",
                            (nc, nl["level_key"], user_id))
                cur.execute("INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'spiritual_resilience_milestone', 10, 'tazkiya')", (user_id,))
                conn.commit()
                milestone_awarded = True
        return {
            "user_id": user_id, "tawbah_streak": tawbah_streak,
            "resilience": result, "milestone_awarded": milestone_awarded,
            "milestone_message": "Aap 3 baar gire aur 3 baar uthay — yeh Awwaboon ka raasta hai. +10 TAZKIYA" if milestone_awarded else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 28. POST /maqsad/drift_check ─────────────────────────────────────────────

@router.post("/maqsad/drift_check")
def maqsad_drift_check(user_id: Optional[str] = None, jwt_payload: dict = Depends(verify_token)):
    """Purpose Drift Detector — weekly AI check against Ummah Role + Archetype."""
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")
    jwt_sub = jwt_payload.get("sub", "anonymous")
    uid = user_id or jwt_sub
    if jwt_sub not in ("anonymous", uid):
        raise HTTPException(403, "Access denied.")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ummah_role, life_stage, archetype_key, drift_pause_until FROM fitrah_users WHERE user_id=%s",
            (uid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        ummah_role, life_stage, archetype, drift_pause_until = row

        # PDF §10 — user has acknowledged this as conscious_choice; pause detection
        if drift_pause_until and drift_pause_until >= datetime.now(timezone.utc).date():
            return {
                "user_id":          uid,
                "drift_detected":   False,
                "paused_until":     drift_pause_until.isoformat(),
                "message":          "Drift detection paused at your request. Resumes after pause period.",
            }
        cur.execute(
            "SELECT action_key, dimension_primary, COUNT(*) FROM fitrah_user_action_logs WHERE user_id=%s AND logged_at >= now() - INTERVAL '14 days' AND points_primary>0 GROUP BY action_key, dimension_primary ORDER BY COUNT(*) DESC",
            (uid,),
        )
        recent = [{"action_key": r[0], "dimension": r[1], "count": r[2]} for r in cur.fetchall()]
        if not recent:
            return {"user_id": uid, "drift_detected": False,
                    "message": "Koi activity nahi mili last 14 din mein — app use shuru karein."}

        top_actions = ", ".join(
            f"{r['action_key']}×{r['count']}" for r in recent[:5]
        )
        _drift_tmpl = _PATCH_CALLS["purpose_drift_detector"]["simple_prompt"]
        _drift_sys  = _PATCH_CALLS["purpose_drift_detector"]["system_prompt"]
        user_msg = _fill_template(_drift_tmpl, {
            "life_mission":  f"Ummah Role: {ummah_role}, Life Stage: {life_stage}, Archetype: {archetype}",
            "ummah_role":    ummah_role or "unknown",
            "top_actions":   top_actions,
        })
        try:
            resp = anthropic_client.messages.create(
                model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
                max_tokens=600,
                messages=[{"role": "user", "content": user_msg}],
                system=_drift_sys,
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            ai_result = json.loads(raw)
        except Exception:
            ai_result = {
                "drift_detected": False,
                "observation_ur": "Jaiza lene mein masla aaya — apna safar jari rakhen.",
                "alignment_action_ur": None,
            }
        return {"user_id": uid, **ai_result}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── POST /maqsad/drift_acknowledge ───────────────────────────────────────────

# PDF §10 — the three user response options to a detected drift
_DRIFT_RESPONSES = frozenset(["realign", "reassess", "conscious_choice"])


class DriftAcknowledgeRequest(BaseModel):
    user_id:  Optional[str] = None
    response: str  # realign | reassess | conscious_choice


@router.post("/maqsad/drift_acknowledge")
def maqsad_drift_acknowledge(
    body: DriftAcknowledgeRequest,
    jwt_payload: dict = Depends(verify_token),
):
    """
    User's response to a detected purpose drift (PDF §10):
      - realign           → user wants to realign; we just reset drift counter
      - reassess          → user wants to re-do Nature Profiler; caller flow decides
      - conscious_choice  → user says this is intentional; pause drift detection 30 days
    Awards purpose_drift_acknowledged (+4 ILM +3 TAZKIYA), once per week.
    """
    jwt_sub = jwt_payload.get("sub", "anonymous")
    uid = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", uid):
        raise HTTPException(403, "Access denied.")
    if body.response not in _DRIFT_RESPONSES:
        raise HTTPException(400, f"response must be one of: {sorted(_DRIFT_RESPONSES)}")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM fitrah_users WHERE user_id = %s", (uid,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found.")

        pause_until = None
        if body.response == "conscious_choice":
            pause_until = datetime.now(timezone.utc).date() + timedelta(days=30)
            cur.execute(
                """UPDATE fitrah_users
                   SET drift_pause_until = %s, purpose_drift_days = 0
                   WHERE user_id = %s""",
                (pause_until, uid),
            )
        else:
            cur.execute(
                "UPDATE fitrah_users SET purpose_drift_days = 0 WHERE user_id = %s",
                (uid,),
            )

        # Award purpose_drift_acknowledged — once per week
        cur.execute(
            """SELECT 1 FROM fitrah_user_action_logs
               WHERE user_id = %s AND action_key = 'purpose_drift_acknowledged'
                 AND logged_at >= now() - INTERVAL '7 days' LIMIT 1""",
            (uid,),
        )
        awarded = False
        if not cur.fetchone():
            cur.execute(
                """INSERT INTO fitrah_user_action_logs
                   (user_id, action_key, points_primary, dimension_primary,
                    points_secondary, dimension_secondary)
                   VALUES (%s, 'purpose_drift_acknowledged', 4, 'ilm', 3, 'tazkiya')""",
                (uid,),
            )
            awarded = True

        conn.commit()

        messages = {
            "realign":          "Wapas aana hi asli niyyah hai — alignment actions suggest ki ja rahi hain.",
            "reassess":         "Thik hai — Nature Profiler dobara karke apna maqsad re-assess kar sakte hain.",
            "conscious_choice": "Aapki choice respected — agle 30 din drift detection paused.",
        }
        return {
            "user_id":        uid,
            "response":       body.response,
            "pause_until":    pause_until.isoformat() if pause_until else None,
            "points_awarded": awarded,
            "message":        messages[body.response],
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── POST /maqsad/habit_simulate ──────────────────────────────────────────────

class HabitSimulateRequest(BaseModel):
    user_id:      Optional[str] = None
    habit_key:    Optional[str] = Field(None, max_length=100)
    custom_habit: Optional[str] = Field(None, max_length=300)
    duration_days: int = 30              # 7 / 14 / 30 / 90


@router.post("/maqsad/habit_simulate")
def maqsad_habit_simulate(body: HabitSimulateRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Habit Formation Simulator — §14 of FitrahOS spec.

    AI projects what a user might experience at key milestones (7 / 14 / 30 days)
    if they maintain the chosen habit consistently.

    FRAMING RULE (strictly enforced in system prompt):
      ❌  "Agar 30 din karo ge, yeh changes aayenge"  (deterministic — Islamically wrong)
      ✅  "In sha Allah, agar consistent raho — historically users yeh report karte hain"

    Disclaimer is always appended: outcome is Allah's — not guaranteed.
    """
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")

    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    # Resolve habit label
    habit_label = body.custom_habit or ""
    if body.habit_key and not habit_label:
        action = ACTIONS.get(body.habit_key)
        habit_label = action.get("action_name", body.habit_key) if action else body.habit_key
    if not habit_label:
        raise HTTPException(400, "Provide habit_key or custom_habit.")

    # Clamp duration to valid options
    valid_durations = [7, 14, 30, 90]
    duration = min(valid_durations, key=lambda x: abs(x - body.duration_days))

    # Build milestones list based on duration
    if duration <= 7:
        milestones = [3, 7]
    elif duration <= 14:
        milestones = [7, 14]
    elif duration <= 30:
        milestones = [7, 14, 30]
    else:
        milestones = [14, 30, 60, 90]

    milestones_str = ", ".join(str(m) for m in milestones)

    # Fetch user context for personalisation
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT spiritual_state, current_nafs_level, ummah_role FROM fitrah_users WHERE user_id=%s",
            (user_id,),
        )
        row = cur.fetchone()
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)

    spiritual_state  = (row[0] if row else None) or "seeking"
    nafs_level       = (row[1] if row else None) or "nafs_e_ammarah"
    ummah_role       = (row[2] if row else None) or "wasatiyya"

    system_prompt = (
        "Aap Fitrah OS ka Habit Formation Simulator hain.\n\n"
        "STRICT FRAMING RULE — NEVER violate:\n"
        "❌ FORBIDDEN: 'Agar X din karo ge, yeh ZAROOR hoga' — deterministic guarantees Islamically haram hain.\n"
        "✅ REQUIRED: 'In sha Allah, agar consistent raho — historically log yeh report karte hain.'\n\n"
        "Har milestone ke liye:\n"
        "- 1 line: spiritual/psychological shift jo historically observe kiya gaya\n"
        "- Urdu/English mix — warm, hopeful tone\n"
        "- No guarantees — always conditional on Allah's mercy and user's consistency\n\n"
        "End with a 1-line Disclaimer: 'Yeh past observations hain — asal barkat Allah ki rahmat se milti hai.'\n\n"
        "Return valid JSON only:\n"
        "{\n"
        '  "habit": "<habit name>",\n'
        '  "duration_days": <int>,\n'
        '  "milestones": [\n'
        '    {"day": <int>, "observation_ur": "<In sha Allah framing — 1 sentence>"},\n'
        "    ...\n"
        "  ],\n"
        '  "disclaimer_ur": "<1-sentence disclaimer>",\n'
        '  "opening_dua": "<short dua for starting this habit — Arabic + Urdu>"\n'
        "}"
    )

    user_msg = (
        f"Habit: {habit_label}\n"
        f"Duration: {duration} din\n"
        f"Milestones to cover: Day {milestones_str}\n"
        f"User spiritual state: {spiritual_state}\n"
        f"User nafs level: {nafs_level}\n"
        f"User ummah role: {ummah_role}\n\n"
        "Is habit ke liye historical observations generate karo — strict 'In sha Allah' framing ke saath."
    )

    try:
        resp = anthropic_client.messages.create(
            model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
            max_tokens=700,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
    except Exception:
        # Graceful fallback — pre-built generic milestones
        result = {
            "habit":        habit_label,
            "duration_days": duration,
            "milestones": [
                {"day": d, "observation_ur": f"In sha Allah, {d} din baad aap apne dil mein farq mehsoos kar sakte hain."}
                for d in milestones
            ],
            "disclaimer_ur": "Yeh past observations hain — asal barkat Allah ki rahmat se milti hai. In sha Allah.",
            "opening_dua": "Allahumma a'inni 'ala dhikrika wa shukrika wa husni 'ibadatik — اے اللہ! مجھے اپنا ذکر، شکر اور اچھی عبادت کرنے میں مدد فرما۔",
        }

    return {
        "user_id":   user_id,
        "habit_key": body.habit_key,
        **result,
        # Hard-coded safety note — never removed by AI
        "islamic_note": "Spiritual transformation ka wada sirf Allah de sakta hai — yeh app nahi. Apna amal Allah ke liye rakhein, results par nahi.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# DB PATCH v2 — DUA THREAD + NEW AI ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── 29. POST /dua/add ─────────────────────────────────────────────────────────

class DuaAddRequest(BaseModel):
    user_id:      Optional[str] = None
    dua_text:     str           = Field(..., min_length=1, max_length=1000)
    context:      Optional[str] = Field(None, max_length=500)
    is_private:   bool = True
    fiqh_context: Optional[str] = Field(None, max_length=100)


@router.post("/dua/add")
def dua_add(body: DuaAddRequest, jwt_payload: dict = Depends(verify_token)):
    """Add a personal dua to the user's Dua Thread."""
    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM fitrah_users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found.")
        cur.execute(
            """INSERT INTO fitrah_dua_thread (user_id, dua_text, context, is_private, fiqh_context)
               VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at""",
            (user_id, body.dua_text, body.context, body.is_private, body.fiqh_context),
        )
        row = cur.fetchone()
        # Award dua_thread_entry_saved (+4 TAQWA), max 5/day
        cur.execute(
            """SELECT COUNT(*) FROM fitrah_user_action_logs
               WHERE user_id = %s AND action_key = 'dua_thread_entry_saved'
                 AND logged_at >= CURRENT_DATE""",
            (user_id,),
        )
        if (cur.fetchone()[0] or 0) < 5:
            cur.execute(
                "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'dua_thread_entry_saved', 4, 'taqwa')",
                (user_id,),
            )
        conn.commit()
        return {
            "dua_id":     row[0],
            "dua_text":   body.dua_text,
            "status":     "pending",
            "created_at": row[1].isoformat() if row[1] else None,
            "message":    "Dua thread mein add ho gayi. Allah qabool kare — Ameen.",
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 30. GET /user/{user_id}/duas ─────────────────────────────────────────────

@router.get("/user/{user_id}/duas")
def get_duas(
    user_id: str,
    status: Optional[str] = None,
    jwt_payload: dict = Depends(verify_token),
):
    """
    List user's duas. Optional status filter: pending / answered / closed_gracefully.
    """
    jwt_sub = jwt_payload.get("sub")
    if jwt_sub and jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    valid_statuses = {"pending", "answered", "closed_gracefully"}
    if status and status not in valid_statuses:
        raise HTTPException(400, f"Invalid status. Choose from: {sorted(valid_statuses)}")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if status:
            cur.execute(
                """SELECT id, dua_text, context, status, created_at, answered_at, closed_at, answer_note
                   FROM fitrah_dua_thread WHERE user_id = %s AND status = %s
                   ORDER BY created_at DESC""",
                (user_id, status),
            )
        else:
            cur.execute(
                """SELECT id, dua_text, context, status, created_at, answered_at, closed_at, answer_note
                   FROM fitrah_dua_thread WHERE user_id = %s
                   ORDER BY created_at DESC""",
                (user_id,),
            )
        rows = cur.fetchall()
        return {
            "user_id": user_id,
            "total":   len(rows),
            "duas": [
                {
                    "dua_id":      r[0],
                    "dua_text":    r[1],
                    "context":     r[2],
                    "status":      r[3],
                    "created_at":  r[4].isoformat() if r[4] else None,
                    "answered_at": r[5].isoformat() if r[5] else None,
                    "closed_at":   r[6].isoformat() if r[6] else None,
                    "answer_note": r[7],
                }
                for r in rows
            ],
        }
    except Exception as e:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 31. PATCH /dua/{dua_id}/status ───────────────────────────────────────────

class DuaStatusUpdate(BaseModel):
    user_id:     Optional[str] = None
    status:      str            # answered / closed_gracefully
    answer_note: Optional[str] = None


@router.patch("/dua/{dua_id}/status")
def update_dua_status(dua_id: int, body: DuaStatusUpdate, jwt_payload: dict = Depends(verify_token)):
    """Mark a dua as answered or closed_gracefully (Allah gave something better)."""
    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    valid_statuses = {"answered", "closed_gracefully"}
    if body.status not in valid_statuses:
        raise HTTPException(400, f"status must be one of: {sorted(valid_statuses)}")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM fitrah_dua_thread WHERE id = %s", (dua_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Dua {dua_id} not found.")
        if row[0] != user_id:
            raise HTTPException(403, "This dua does not belong to you.")

        cur.execute(
            """UPDATE fitrah_dua_thread
               SET status      = %s,
                   answer_note = %s,
                   answered_at = CASE WHEN %s = 'answered'         THEN now() ELSE NULL END,
                   closed_at   = CASE WHEN %s = 'closed_gracefully' THEN now() ELSE NULL END
               WHERE id = %s""",
            (body.status, body.answer_note, body.status, body.status, dua_id),
        )
        # Award dua_answered_logged (+8 TAQWA) when marked answered
        if body.status == "answered":
            cur.execute(
                "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s, 'dua_answered_logged', 8, 'taqwa')",
                (user_id,),
            )
        conn.commit()
        msg = (
            "Al-hamdulillah — Allah ne qabool kiya."
            if body.status == "answered"
            else (
                "Allah har dua sunta hai. Kabhi waisa jawab milta hai jo hum chahte hain, "
                "kabhi kuch aur — sirf Allah hi jaanta hai. Aap ka sabr aur tawakkul hi ibadah hai."
            )
        )
        return {"dua_id": dua_id, "status": body.status, "message": msg}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 32. POST /maqsad/qadr ─────────────────────────────────────────────────────

class QadrRequest(BaseModel):
    user_id:   Optional[str] = None
    situation: str = Field(..., min_length=1, max_length=2000)


@router.post("/maqsad/qadr")
@limiter.limit("10/minute")
def qadr_engine(request: Request, body: QadrRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Qadr Engine — classifies a life situation as one of:
    Test (Imtihan) / Training (Tarbiyat) / Consequence (Natijah) /
    Warning (Tanbeeh) / Elevation (Raf'a)

    Returns Quran + Seerah evidence and a practical path forward.
    """
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")

    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    # PDF §23 Safety Check A — crisis keywords in user input override everything
    if check_crisis(body.situation or ""):
        from fitrah_engine.fitrah_middleware import CRISIS_RESOURCE_TEXT
        return {
            "user_id":        user_id,
            "situation":      body.situation,
            "crisis":         True,
            "crisis_message": CRISIS_RESOURCE_TEXT,
        }

    _qadr_tmpl = _PATCH_CALLS["qadr_engine"]["simple_prompt"]
    user_msg = _fill_template(_qadr_tmpl, {"situation": body.situation})
    try:
        resp = anthropic_client.messages.create(
            model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
            max_tokens=700,
            messages=[{"role": "user", "content": user_msg}],
            system=(
                "Tum ek Islamic scholar ho — Quran aur Seerah ka gehra ilm rakhte ho. "
                "Tone compassionate ho — judgment bilkul nahi. "
                "Sirf valid JSON return karo — no markdown, no extra text."
            ),
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        result = {
            "classification":    "Test",
            "classification_ur": "Imtihan",
            "explanation_ur":    "Har mushkil mein Allah ka hikmat hai. Sabar karo — rasta nikalta hai.",
            "quran_ref":         "Al-Baqarah 2:155",
            "seerah_ref":        None,
            "action_ur":         "Aaj 2 rakat nafl parhein aur apni situation Allah ke saath share karein.",
        }

    # Log qadr_engine_used (+6 ILM +4 TAZKIYA) once per day
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT 1 FROM fitrah_user_action_logs
               WHERE user_id = %s AND action_key = 'qadr_engine_used'
                 AND logged_at >= CURRENT_DATE LIMIT 1""",
            (user_id,),
        )
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary, points_secondary, dimension_secondary) VALUES (%s, 'qadr_engine_used', 6, 'ilm', 4, 'tazkiya')",
                (user_id,),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        log.warning("fitrah_routes: swallowed exception after rollback", exc_info=True)
    finally:
        release_db_connection(conn)

    # Apply Layer 6 (qadr claim filter) + Safety Check C (comparison filter) to free-form fields
    for _fld in ("explanation_ur", "action_ur"):
        if result.get(_fld):
            _safe, _ = _run_middleware(user_id, result[_fld], last_user_message=body.situation or "", action_key="qadr_engine_used")
            result[_fld] = _safe

    return {"user_id": user_id, "situation": body.situation, **result}


# ── 33. POST /maqsad/life_test ───────────────────────────────────────────────

class LifeTestRequest(BaseModel):
    user_id:  Optional[str] = None
    problem:  str = Field(..., min_length=1, max_length=2000)


@router.post("/maqsad/life_test")
@limiter.limit("10/minute")
def life_test_classifier(request: Request, body: LifeTestRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Life Test Classifier — for Akhlaq AI chat integration.
    Classifies a specific problem with the user's spiritual context.
    Returns test_type, Quran/Hadith basis, and a sabr action.
    """
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")

    jwt_sub = jwt_payload.get("sub", "anonymous")
    user_id = body.user_id or jwt_sub
    if jwt_sub not in ("anonymous", user_id):
        raise HTTPException(403, "Access denied.")

    # PDF §23 Safety Check A — crisis keywords in user input override everything
    if check_crisis(body.problem or ""):
        from fitrah_engine.fitrah_middleware import CRISIS_RESOURCE_TEXT
        return {
            "user_id":        user_id,
            "problem":        body.problem,
            "crisis":         True,
            "crisis_message": CRISIS_RESOURCE_TEXT,
        }

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT spiritual_state FROM fitrah_users WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")
        spiritual_state = row[0] or "seeking"
        dim_scores      = _fetch_dim_scores(cur, user_id)
        tazkiya         = round(dim_scores.get("tazkiya", 5), 1)
    except HTTPException:
        release_db_connection(conn)
        raise
    except Exception as e:
        release_db_connection(conn)
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")

    _lt_tmpl = _PATCH_CALLS["life_test_classifier"]["simple_prompt"]
    _lt_sys  = _PATCH_CALLS["life_test_classifier"]["system_prompt"] if "system_prompt" in _PATCH_CALLS["life_test_classifier"] else "Tum ek Islamic spiritual guide ho. Sirf valid JSON return karo — no markdown."
    user_msg = _fill_template(_lt_tmpl, {
        "problem":  body.problem,
        "tazkiya":  str(tazkiya),
        "state":    spiritual_state,
    })
    try:
        resp = anthropic_client.messages.create(
            model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
            max_tokens=600,
            messages=[{"role": "user", "content": user_msg}],
            system=_lt_sys,
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
    except Exception:
        result = {
            "test_type":         "Test",
            "test_type_ur":      "Imtihan",
            "explanation_ur":    "Yeh ek aazmaish hai — Allah ne tumhein choose kiya hai is ke liye.",
            "quran_or_hadith":   "لَا يُكَلِّفُ اللَّهُ نَفْسًا إِلَّا وُسْعَهَا",
            "source_reference":  "Al-Baqarah 2:286",
            "sabr_action_ur":    "Abhi ek deep breath lein aur Hasbunallah 3 baar parhen.",
        }

    # Log life_test_classified (+6 ILM +4 TAZKIYA) once per day
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT 1 FROM fitrah_user_action_logs
               WHERE user_id = %s AND action_key = 'life_test_classified'
                 AND logged_at >= CURRENT_DATE LIMIT 1""",
            (user_id,),
        )
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary, points_secondary, dimension_secondary) VALUES (%s, 'life_test_classified', 6, 'ilm', 4, 'tazkiya')",
                (user_id,),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        log.warning("fitrah_routes: swallowed exception after rollback", exc_info=True)
    finally:
        release_db_connection(conn)

    for _fld in ("explanation_ur", "sabr_action_ur"):
        if result.get(_fld):
            _safe, _ = _run_middleware(user_id, result[_fld], last_user_message=body.problem or "", action_key="life_test_classified")
            result[_fld] = _safe

    return {"user_id": user_id, "problem": body.problem, **result}


# ── 34. POST /maqsad/sunnah_dna_refresh ──────────────────────────────────────

@router.post("/maqsad/sunnah_dna_refresh")
def sunnah_dna_refresh(user_id: Optional[str] = None, jwt_payload: dict = Depends(verify_token)):
    """
    AI-powered Sunnah DNA analysis using the sunnah_dna_analyzer prompt.
    Reads the user's stored sunnah_dna numeric scores, calls Claude for a
    qualitative analysis, and updates the sunnah_dna JSONB column with the result.
    Trigger: onboarding complete OR weekly refresh.
    Returns: {eating, sleeping, social, ibadah} labels + summary_ur.
    """
    if not anthropic_client:
        raise HTTPException(503, "Anthropic API key not configured.")

    jwt_sub = jwt_payload.get("sub", "anonymous")
    uid = user_id or jwt_sub
    if not uid or uid == "anonymous":
        raise HTTPException(400, "Authenticated user_id required.")
    if jwt_sub not in ("anonymous", uid):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT sunnah_dna_eating, sunnah_dna_sleeping,
                      sunnah_dna_social, sunnah_dna_ibadah
               FROM fitrah_users WHERE user_id = %s""",
            (uid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found.")

        eating, sleeping, social, ibadah = (float(v or 0) for v in row)

        # If all zeros, scores haven't been set from profiler yet
        if eating == 0 and sleeping == 0 and social == 0 and ibadah == 0:
            return {
                "user_id": uid,
                "message": "Profiler answers not found — please complete the profiler first.",
                "sunnah_dna": None,
            }

        _tmpl = _PATCH_CALLS["sunnah_dna_analyzer"]["simple_prompt"]
        _sys  = _PATCH_CALLS["sunnah_dna_analyzer"]["system_prompt"]
        user_msg = _fill_template(_tmpl, {
            "eating_score":  str(int(eating)),
            "sleeping_score": str(int(sleeping)),
            "social_score":  str(int(social)),
            "ibadah_score":  str(int(ibadah)),
        })

        try:
            resp = anthropic_client.messages.create(
                model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
                max_tokens=400,
                messages=[{"role": "user", "content": user_msg}],
                system=_sys,
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            ai_result = json.loads(raw)
        except Exception:
            ai_result = {
                "eating":    "Growing",
                "sleeping":  "Growing",
                "social":    "Growing",
                "ibadah":    "Growing",
                "summary_ur": "Aapka Sunnah DNA analyze ho raha hai — keep going.",
            }

        # Persist AI analysis back to sunnah_dna JSONB column
        merged = {
            **ai_result,
            "scores": {
                "eating": eating, "sleeping": sleeping,
                "social": social, "ibadah":   ibadah,
            },
        }
        cur.execute(
            "UPDATE fitrah_users SET sunnah_dna = %s WHERE user_id = %s",
            (json.dumps(merged), uid),
        )
        conn.commit()

        return {"user_id": uid, "sunnah_dna": merged}

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 35. POST /onboarding/complete ────────────────────────────────────────────

def _match_sahaba(
    dim_scores: dict,
    jalali_jamali: str = "mixed",
    introvert_extrovert: str = "ambivert",
    ummah_role: str = "wasatiyya",
    spiritual_state: str = "seeking",
    nafs_level: str = "nafs_e_ammarah",
    tawbah_streak: int = 0,
) -> list[dict]:
    """
    5-dimension Sahaba matching per §17 of FitrahOS spec + sahaba_matching_config.json.
    Final score = 0.35*personality + 0.30*life_aim + 0.15*spiritual_state
                + 0.10*habit_strength + 0.10*struggle
    Returns list of dicts sorted by match_score descending.
    """
    profiles = _sahaba_cfg.get("sahaba_profiles", [])
    alg      = _sahaba_cfg.get("scoring_algorithm", {})
    ps       = alg.get("personality_scoring", {})
    ls       = alg.get("life_aim_scoring", {})
    ss       = alg.get("spiritual_state_scoring", {})
    hs       = alg.get("habit_strength_scoring", {})

    # Pre-compute user's top-2 dimensions by score
    sorted_dims = sorted(dim_scores.items(), key=lambda x: x[1], reverse=True)
    top_dim     = sorted_dims[0][0] if sorted_dims else ""
    second_dim  = sorted_dims[1][0] if len(sorted_dims) > 1 else ""

    results = []
    for profile in profiles:
        p_personality = profile.get("personality", {})
        s_jj  = p_personality.get("jalali_jamali", "mixed")
        s_ie  = p_personality.get("introvert_extrovert", "ambivert")

        # ── Personality match (weight 0.35) ─────────────────────────────────
        jj_score = 0.0
        if jalali_jamali == s_jj:
            jj_score = float(ps.get("exact_jalali_jamali_match", 0.6))
        elif jalali_jamali == "mixed" or s_jj == "mixed":
            jj_score = float(ps.get("exact_jalali_jamali_match", 0.6)) * 0.5

        ie_score = 0.0
        if introvert_extrovert == s_ie:
            ie_score = float(ps.get("exact_introvert_extrovert_match", 0.4))
        elif introvert_extrovert == "ambivert" or s_ie == "ambivert":
            ie_score = float(ps.get("exact_introvert_extrovert_match", 0.4)) * 0.5

        personality_score = min(1.0, jj_score + ie_score)

        # ── Life aim match (weight 0.30) ─────────────────────────────────────
        life_aims = profile.get("life_aims", [])
        if life_aims and ummah_role == life_aims[0]:
            life_aim_score = float(ls.get("primary_role_match", 1.0))
        elif ummah_role in life_aims:
            life_aim_score = float(ls.get("secondary_role_match", 0.5))
        else:
            life_aim_score = 0.0

        # ── Spiritual state match (weight 0.15) ──────────────────────────────
        aligned_states = profile.get("spiritual_states_aligned", [])
        aligned_levels = profile.get("nafs_levels_aligned", [])
        if spiritual_state in aligned_states:
            state_score = float(ss.get("exact_state_match", 1.0))
        elif nafs_level in aligned_levels:
            state_score = float(ss.get("adjacent_state_match", 0.5))
        else:
            state_score = 0.0

        # ── Habit strength match (weight 0.10) ───────────────────────────────
        habit_strengths = profile.get("habit_strengths", [])
        if top_dim in habit_strengths:
            habit_score = float(hs.get("if_top_dim_in_sahaba_strengths", 1.0))
        elif second_dim in habit_strengths:
            habit_score = float(hs.get("if_secondary_dim_in_sahaba_strengths", 0.5))
        else:
            habit_score = 0.0

        # ── Struggle match (weight 0.10) ─────────────────────────────────────
        if tawbah_streak > 7:
            struggle_score = 0.8
        elif tawbah_streak > 0:
            struggle_score = 0.5
        else:
            struggle_score = 0.2

        # ── Final weighted score ──────────────────────────────────────────────
        final = (
            0.35 * personality_score
            + 0.30 * life_aim_score
            + 0.15 * state_score
            + 0.10 * habit_score
            + 0.10 * struggle_score
        )

        results.append({
            "key":         profile["key"],
            "name":        profile["display_name"],
            "trait":       profile.get("mission_tagline", ""),
            "match_score": round(final, 3),
            "breakdown": {
                "personality":    round(personality_score, 2),
                "life_aim":       round(life_aim_score, 2),
                "spiritual_state": round(state_score, 2),
                "habit_strength": round(habit_score, 2),
                "struggle":       round(struggle_score, 2),
            },
        })

    return sorted(results, key=lambda x: x["match_score"], reverse=True)


_VALID_FIQH_SCHOOLS = frozenset(["hanafi", "shafi_i", "maliki", "hanbali", "ahle_hadith"])


class OnboardingCompleteRequest(BaseModel):
    fiqh_school: Optional[str] = None  # user's preferred fiqh school; defaults to "hanafi"


@router.post("/onboarding/complete")
def onboarding_complete(
    body: OnboardingCompleteRequest = OnboardingCompleteRequest(),
    jwt_payload: dict = Depends(verify_token),
):
    """
    Final onboarding step after /profiler/submit.
    Runs Sahaba archetype matching, caps nafs level at mulhama (JS v3 rule),
    awards onboarding_complete action, stores Sahaba on user profile.
    Accepts optional fiqh_school (hanafi/shafi_i/maliki/hanbali/ahle_hadith).
    """
    user_id = jwt_payload.get("sub")
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user required.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT u.profiler_completed_at, u.crystal_score, u.current_nafs_level,
                      d.taqwa_score, d.ilm_score, d.tazkiya_score,
                      d.ihsan_score, d.nafs_score, d.maal_score,
                      u.jalali_jamali, u.introvert_extrovert, u.ummah_role,
                      u.spiritual_state, u.tawbah_streak_current
               FROM fitrah_users u
               JOIN fitrah_user_dimensions d ON d.user_id = u.user_id
               WHERE u.user_id = %s""",
            (user_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            raise HTTPException(400, {
                "error":   "profiler_not_complete",
                "message": "Please complete the Fitrah Profiler first (/profiler/submit).",
            })

        dim_scores    = {"taqwa": float(row[3] or 0), "ilm": float(row[4] or 0),
                         "tazkiya": float(row[5] or 0), "ihsan": float(row[6] or 0),
                         "nafs": float(row[7] or 0), "maal": float(row[8] or 0)}
        crystal            = float(row[1] or 0)
        current_level      = row[2] or "nafs_e_ammarah"
        jalali_jamali      = row[9]  or "mixed"
        introvert_extrovert = row[10] or "ambivert"
        ummah_role         = row[11] or "wasatiyya"
        spiritual_state    = row[12] or "seeking"
        tawbah_streak      = int(row[13] or 0)

        # Cap at nafs_e_mulhama on initial onboarding
        mulhama_obj    = next((l for l in NAFS_LEVELS if l["level_key"] == "nafs_e_mulhama"), None)
        computed_level = get_nafs_level(crystal, dim_scores["taqwa"])
        capped         = bool(mulhama_obj and computed_level["level_order"] > mulhama_obj["level_order"])
        capped_level   = mulhama_obj if capped else computed_level

        if capped_level["level_key"] != current_level:
            cur.execute(
                "UPDATE fitrah_users SET current_nafs_level=%s, nafs_level_since=CURRENT_DATE WHERE user_id=%s",
                (capped_level["level_key"], user_id),
            )

        # Fiqh school — validate and default to hanafi
        fiqh_school = (body.fiqh_school or "hanafi").lower()
        if fiqh_school not in _VALID_FIQH_SCHOOLS:
            fiqh_school = "hanafi"
        cur.execute(
            "UPDATE fitrah_users SET fiqh_school=%s WHERE user_id=%s",
            (fiqh_school, user_id),
        )

        # Sahaba matching
        ranked = _match_sahaba(
            dim_scores,
            jalali_jamali=jalali_jamali,
            introvert_extrovert=introvert_extrovert,
            ummah_role=ummah_role,
            spiritual_state=spiritual_state,
            nafs_level=capped_level["level_key"],
            tawbah_streak=tawbah_streak,
        )
        # Guard: pad to 3 if fewer profiles matched
        _fallback = {"key": "abu_bakr", "name": "Abu Bakr As-Siddiq (RA)", "trait": "As-Siddiq", "match_score": 0.0, "breakdown": {}}
        while len(ranked) < 3:
            ranked.append(ranked[-1] if ranked else _fallback)
        primary, sec1, sec2 = ranked[0], ranked[1], ranked[2]
        cur.execute(
            "UPDATE fitrah_users SET primary_sahaba=%s, secondary_sahaba_1=%s, secondary_sahaba_2=%s WHERE user_id=%s",
            (primary["key"], sec1["key"], sec2["key"], user_id),
        )

        # One-time onboarding bonus
        cur.execute(
            "SELECT 1 FROM fitrah_user_action_logs WHERE user_id=%s AND action_key='onboarding_complete' LIMIT 1",
            (user_id,),
        )
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO fitrah_user_action_logs (user_id, action_key, points_primary, dimension_primary) VALUES (%s,'onboarding_complete',10,'taqwa')",
                (user_id,),
            )

        conn.commit()

        # Fetch spiritual_state_suggested (written by cron; may be None at onboarding)
        cur.execute("SELECT spiritual_state_suggested FROM fitrah_users WHERE user_id = %s", (user_id,))
        sss_row = cur.fetchone()
        spiritual_state_suggested = sss_row[0] if sss_row else None

        return {
            "success":   True,
            "user_id":   user_id,
            "nafs_level": {
                "level_key":         capped_level["level_key"],
                "display_name":      capped_level["display_name"],
                "arabic":            capped_level["arabic"],
                "capped_at_mulhama": capped,
            },
            "sahaba_match": {
                "primary":   {"key": primary["key"], "name": primary["name"],
                              "trait": primary["trait"], "match_score": primary["match_score"]},
                "secondary": [{"key": sec1["key"], "name": sec1["name"], "trait": sec1["trait"]},
                              {"key": sec2["key"], "name": sec2["name"], "trait": sec2["trait"]}],
            },
            "dimension_scores": {k: round(v, 1) for k, v in dim_scores.items()},
            "crystal_score":    crystal,
            "fiqh_school":      fiqh_school,
            "spiritual_state_suggested": spiritual_state_suggested,
            "next_steps": [
                "POST /maqsad/statement — generate your 3-part Maqsad",
                "POST /maqsad/sunnah_dna_refresh — analyse your Sunnah DNA",
                "POST /qalb/log — log your first Qalb state",
            ],
            "message": f"Marhaba! Aapka Sahaba archetype '{primary['name']}' hai — {primary['trait']}.",
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        log.error(f"onboarding_complete error for {user_id}: {exc}")
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 36. GET /ihtisab/weekly ───────────────────────────────────────────────────

@router.get("/ihtisab/weekly")
def get_weekly_ihtisab(user_id: Optional[str] = None, jwt_payload: dict = Depends(verify_token)):
    """
    Returns the most recent weekly Ihtisab.
    Generates AI narrative on first fetch; marks user_reviewed=TRUE on return.
    """
    jwt_sub = jwt_payload.get("sub", "anonymous")
    uid     = user_id or jwt_sub
    if not uid or uid == "anonymous":
        raise HTTPException(400, "user_id required.")
    if jwt_sub not in ("anonymous", uid):
        raise HTTPException(403, "Access denied.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, week_ending_date, week_number,
                      total_actions_count, ibadat_actions_count, ilm_actions_count,
                      akhlaq_actions_count, khidmat_actions_count, nafs_actions_count,
                      crystal_start, crystal_end, crystal_change,
                      avg_barakah_score, qalb_state_mode, overall_narrative,
                      suggested_focus, user_reviewed, generated_at
               FROM fitrah_weekly_ihtisab
               WHERE user_id=%s ORDER BY week_ending_date DESC LIMIT 1""",
            (uid,),
        )
        row = cur.fetchone()
        if not row:
            return {"user_id": uid, "available": False,
                    "message": "Koi Ihtisab record nahi mila — pehle hafte baad wapas aayein."}

        (rec_id, week_end, week_num, total, ibadat, ilm_cnt, akhlaq, khidmat, nafs_cnt,
         crystal_start, crystal_end, crystal_change, avg_barakah, qalb_mode,
         narrative, suggested_focus, user_reviewed, generated_at) = row

        if not narrative and anthropic_client:
            try:
                prompt = (
                    f"Weekly spiritual Ihtisab (Urdu/English mix, 3-4 sentences, end with dua). "
                    f"{total} Islamic actions this week — Ibadat:{ibadat}, Ilm:{ilm_cnt}, "
                    f"Akhlaq:{akhlaq}, Khidmat:{khidmat}. Crystal change:{crystal_change:+.1f}. "
                    f"Qalb:{qalb_mode or 'unknown'}. Barakah avg:{avg_barakah:.0f}/100. "
                    "Warm tone, no points/scores language, no gamification."
                )
                ai_r = anthropic_client.messages.create(
                    model=_MAQSAD_CFG.get("model", "claude-sonnet-4-6"),
                    max_tokens=300,
                    messages=[{"role": "user", "content": prompt}],
                )
                narrative = ai_r.content[0].text.strip()
            except Exception as exc:
                log.warning(f"[Ihtisab] AI failed: {exc}")
                narrative = (
                    f"Is hafte aap ne {total} amal kiye — har amal qeemat rakhta hai. "
                    "Istiqamat hi asli kamyabi hai. Allah aap ke ikhlas qabool farmaye. Ameen."
                )
            cur.execute(
                "UPDATE fitrah_weekly_ihtisab SET overall_narrative=%s, user_reviewed=TRUE, user_reviewed_at=now() WHERE id=%s",
                (narrative, rec_id),
            )
            conn.commit()
        elif not user_reviewed:
            cur.execute(
                "UPDATE fitrah_weekly_ihtisab SET user_reviewed=TRUE, user_reviewed_at=now() WHERE id=%s",
                (rec_id,),
            )
            conn.commit()

        return {
            "user_id":           uid,
            "available":         True,
            "week_ending_date":  week_end.isoformat() if week_end else None,
            "week_number":       week_num,
            "actions":           {"total": total or 0, "ibadat": ibadat or 0, "ilm": ilm_cnt or 0,
                                  "akhlaq": akhlaq or 0, "khidmat": khidmat or 0, "nafs": nafs_cnt or 0},
            "crystal":           {"start": round(float(crystal_start or 0), 1),
                                  "end": round(float(crystal_end or 0), 1),
                                  "change": round(float(crystal_change or 0), 2)},
            "avg_barakah_score": round(float(avg_barakah or 0), 1),
            "qalb_state_mode":   qalb_mode,
            "overall_narrative": narrative,
            "suggested_focus":   suggested_focus,
            "generated_at":      generated_at.isoformat() if generated_at else None,
        }

    except HTTPException:
        raise
    except Exception as exc:
        log.error(f"get_weekly_ihtisab error for {uid}: {exc}")
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 37. GET /ihtisab/history ──────────────────────────────────────────────────

@router.get("/ihtisab/history")
def get_ihtisab_history(
    user_id:     Optional[str] = None,
    limit:       int = 8,
    offset:      int = 0,
    jwt_payload: dict = Depends(verify_token),
):
    """Paginated list of past weekly Ihtisab records (max 52 = 1 year)."""
    jwt_sub = jwt_payload.get("sub", "anonymous")
    uid     = user_id or jwt_sub
    if not uid or uid == "anonymous":
        raise HTTPException(400, "user_id required.")
    if jwt_sub not in ("anonymous", uid):
        raise HTTPException(403, "Access denied.")

    limit  = min(max(limit, 1), 52)
    offset = max(offset, 0)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT week_ending_date, week_number, total_actions_count, crystal_change,
                      qalb_state_mode, overall_narrative, user_reviewed, generated_at
               FROM fitrah_weekly_ihtisab WHERE user_id=%s
               ORDER BY week_ending_date DESC LIMIT %s OFFSET %s""",
            (uid, limit, offset),
        )
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM fitrah_weekly_ihtisab WHERE user_id=%s", (uid,))
        total_count = cur.fetchone()[0]

        return {
            "user_id": uid, "total": total_count, "limit": limit, "offset": offset,
            "records": [
                {
                    "week_ending_date": r[0].isoformat() if r[0] else None,
                    "week_number":      r[1],
                    "total_actions":    r[2] or 0,
                    "crystal_change":   round(float(r[3] or 0), 2),
                    "qalb_mode":        r[4],
                    "narrative_preview": (r[5] or "")[:120] + "…" if r[5] and len(r[5]) > 120 else r[5],
                    "user_reviewed":    r[6],
                    "generated_at":     r[7].isoformat() if r[7] else None,
                }
                for r in rows
            ],
        }

    except Exception as exc:
        log.exception("fitrah_routes DB error"); raise HTTPException(500, "Internal server error.")
    finally:
        release_db_connection(conn)


# ── 38a. GET /kafarat/scenarios ───────────────────────────────────────────────

@router.get("/kafarat/scenarios")
def kafarat_scenarios(_jwt_payload: dict = Depends(verify_token)):
    """Return all supported kafarat scenario types for discovery."""
    scenarios = [
        {
            "key":            s.get("key", ""),
            "display_name_en": s.get("display_name_en", ""),
            "display_name_ur": s.get("display_name_ur", ""),
            "quran_ref":      s.get("quran_ref", ""),
        }
        for s in _kafarat_cfg.get("scenarios", [])
        if s.get("key")
    ]
    return {"scenarios": scenarios, "count": len(scenarios)}


# ── 38. POST /kafarat/ask ─────────────────────────────────────────────────────

_KAFARAT_SCENARIO_MAP: dict[str, str] = {
    s["key"]: f"{s['display_name_en']} — {s.get('quran_ref', '')}"
    for s in _kafarat_cfg.get("scenarios", [])
    if s.get("key") and s.get("display_name_en")
}


def _rag_search(query: str, fiqh_kb_value: str, top_k: int = 6) -> str:
    """
    Embed `query` with text-embedding-3-large, then run pgvector cosine search
    on knowledge_base filtered to the given fiqh school.
    Returns a concatenated context string ready to pass into a prompt.
    Returns "" if embedding or DB fails.
    """
    if not _openai_client:
        return ""
    try:
        emb = _openai_client.embeddings.create(
            input=query, model="text-embedding-3-large"
        )
        vector_str = "[" + ",".join(map(str, emb.data[0].embedding)) + "]"
    except Exception as exc:
        log.warning(f"kafarat embed error: {exc}")
        return ""

    conn = get_db_connection()
    if not conn:
        return ""
    try:
        cur = conn.cursor()
        cur.execute("SET local work_mem = '256MB';")
        cur.execute("SET local hnsw.ef_search = 60;")
        cur.execute(
            """
            SELECT metadata->>'title' AS title,
                   metadata->>'author' AS author,
                   text
            FROM knowledge_base
            WHERE LOWER(fiqh) = LOWER(%s)
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (fiqh_kb_value, vector_str, top_k),
        )
        rows = cur.fetchall()
        if not rows:
            # Broaden to 'general' fiqh chunks as fallback
            cur.execute(
                """
                SELECT metadata->>'title' AS title,
                       metadata->>'author' AS author,
                       text
                FROM knowledge_base
                WHERE LOWER(fiqh) IN (LOWER(%s), 'general')
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (fiqh_kb_value, vector_str, top_k),
            )
            rows = cur.fetchall()
        context = ""
        for r in rows:
            title  = r[0] or "Classical Fiqh Book"
            author = r[1] or "Unknown Scholar"
            context += f"[{title} by {author}]\n{r[2]}\n\n"
        return context
    except Exception as exc:
        log.warning(f"kafarat pgvector error: {exc}")
        return ""
    finally:
        release_db_connection(conn)


def _static_kafarat(scenario_key: str, school: str) -> dict | None:
    """Return the static ruling from the JSON config as a fallback."""
    scenario = next(
        (s for s in _kafarat_cfg.get("scenarios", []) if s["key"] == scenario_key),
        None,
    )
    if not scenario:
        return None
    ruling = scenario.get("rulings", {}).get(school)
    if not ruling:
        return None
    return {
        "scenario_key":    scenario_key,
        "display_name_en": scenario["display_name_en"],
        "display_name_ur": scenario["display_name_ur"],
        "quran_ref":       scenario.get("quran_ref", ""),
        "arabic_ref":      scenario.get("arabic_ref", ""),
        "fiqh_school":     school,
        "ruling": {
            "options":   ruling.get("options_in_order", []),
            "sequence":  ruling.get("sequence", ""),
            "if_unable": ruling.get("if_unable", ""),
            "notes":     ruling.get("notes", ""),
        },
        "source": "static_json",
        "scholar_review": _kafarat_cfg.get("review_status", ""),
        "general_principles": _kafarat_cfg.get("general_principles", {}),
    }


class KafaratAskRequest(BaseModel):
    scenario_key: Optional[str] = Field(None, max_length=100)
    question:     Optional[str] = Field(None, max_length=1000)


@router.post("/kafarat/ask")
def kafarat_ask(body: KafaratAskRequest, jwt_payload: dict = Depends(verify_token)):
    """
    Look up kafarat ruling from the RAG knowledge base for the user's saved
    fiqh_school.  Falls back to static JSON if the knowledge base returns no
    relevant chunks or AI is unavailable.

    Priority: scenario_key (structured) > question (free-text).
    At least one of the two must be provided.
    """
    user_id = jwt_payload.get("sub")
    if not user_id or user_id == "anonymous":
        raise HTTPException(400, "Authenticated user required.")

    if not body.scenario_key and not body.question:
        raise HTTPException(400, "Provide scenario_key or question.")

    if body.scenario_key and body.scenario_key not in _KAFARAT_SCENARIO_MAP:
        valid = list(_KAFARAT_SCENARIO_MAP.keys())
        raise HTTPException(400, {"error": "unknown_scenario", "valid_keys": valid})

    # ── Fetch user's fiqh school ──────────────────────────────────────────────
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT fiqh_school FROM fitrah_users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    finally:
        release_db_connection(conn)

    school = (row[0] if row and row[0] else "hanafi").lower()
    if school not in _VALID_FIQH_SCHOOLS:
        school = "hanafi"
    fiqh_kb_value = _FIQH_KB_MAP.get(school, "hanafi")

    # ── Build search query ────────────────────────────────────────────────────
    if body.scenario_key:
        scenario_label = _KAFARAT_SCENARIO_MAP[body.scenario_key]
        search_query = (
            f"kafarat ruling expiation {scenario_label} "
            f"according to {school} fiqh school"
        )
    else:
        search_query = body.question  # type: ignore[assignment]

    # ── RAG: embed → pgvector search ─────────────────────────────────────────
    rag_context = _rag_search(search_query, fiqh_kb_value)

    # ── Static fallback (always computed — used if AI fails or no context) ───
    static = _static_kafarat(body.scenario_key, school) if body.scenario_key else None

    if not rag_context:
        # No RAG chunks — return static JSON immediately
        if static:
            return {**static, "rag_used": False}
        raise HTTPException(503, "No kafarat data available for this school.")

    # ── Claude: generate structured ruling from RAG context ──────────────────
    school_display = school.replace("_", " ").title()
    scenario_label_str = (
        _KAFARAT_SCENARIO_MAP.get(body.scenario_key, "")
        if body.scenario_key
        else (body.question or "")
    )

    system_prompt = (
        "You are an Islamic Fiqh scholar specialising in kafarat (expiation) rulings. "
        "Answer STRICTLY from the provided classical Fiqh context. "
        "If the context does not contain enough detail, say so honestly — do NOT invent rulings. "
        "Always append: 'For major kafaraat, please consult a qualified scholar.'"
    )
    user_prompt = (
        f"Fiqh School: {school_display}\n"
        f"Topic: {scenario_label_str}\n\n"
        f"[CLASSICAL FIQH CONTEXT FROM KNOWLEDGE BASE]:\n{rag_context}\n\n"
        "Return a JSON object with these exact keys:\n"
        "{\n"
        '  "ruling_summary": "2-3 sentence summary of the kafarat",\n'
        '  "steps": ["step 1", "step 2", ...],\n'
        '  "sequence_type": "ordered OR free_choice",\n'
        '  "quran_hadith_ref": "primary reference",\n'
        '  "important_notes": "school-specific nuances",\n'
        '  "sources": ["Book Title by Author", ...]\n'
        "}\n"
        "Output valid JSON only — no markdown, no explanation outside the JSON."
    )

    raw = _call_claude(system_prompt, user_prompt)

    if raw:
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        try:
            ai_ruling = json.loads(raw)
            return {
                "scenario_key":    body.scenario_key,
                "question":        body.question,
                "fiqh_school":     school,
                "rag_used":        True,
                "ai_ruling":       ai_ruling,
                "static_fallback": static,
                "scholar_note":    "DRAFT — verify with a qualified scholar before acting.",
            }
        except json.JSONDecodeError:
            pass

    # AI call failed / bad JSON — fall back to static
    if static:
        return {**static, "rag_used": False, "rag_context_available": True}
    raise HTTPException(503, "Kafarat ruling unavailable. Please try again.")
