"""
scoring_logic.py — Pure scoring functions for Fitrah AI.

All config is loaded from JSON files at module startup into memory so every
scoring call is O(1) — no DB lookups for config data.
"""
import json
import os
from datetime import datetime, timezone

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load(filename: str) -> dict | list:
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


# ── Config loaded once at startup ────────────────────────────────────────────
_dims_raw  = _load("dimensions_config.json")
_lvls_raw  = _load("nafs_levels_config.json")
_acts_raw  = _load("actions_master.json")


def _normalize_dimension(d: dict) -> dict:
    """Normalize v1.0 and legacy dimension config dicts to a unified schema."""
    if "key" in d and "weight" in d:
        # New format (v1.0) — key is uppercase like "TAQWA"
        return {
            "dimension_key":          d["key"].lower(),
            "weight_in_crystal":      float(d.get("weight", 0)),
            "daily_max_gain":         int(d.get("max_single_day_gain", d.get("daily_max_gain", 999))),
            "decay_rate_per_day":     float(d.get("decay_per_day", d.get("decay_rate_per_day", 0))),
            "decay_starts_after_days": float(d.get("decay_starts_after_days", 0)),
            "floor_score":            float(d.get("floor_score", 0)),
            "show_numbers":           bool(d.get("show_numbers", False)),
        }
    return d


def _normalize_level(l: dict) -> dict:
    """Normalize v1.0 and legacy nafs level config dicts to a unified schema."""
    if "key" in l and "crystal_range" in l:
        # New format (v1.0) — key like "nafs_e_ammarah"
        cr = l.get("crystal_range", {})
        return {
            "level_key":          l["key"],
            "level_order":        int(l.get("level_number", l.get("level_order", 0))),
            "display_name":       l.get("short_name") or l.get("name_transliterated") or l["key"],
            "arabic":             l.get("name_ar") or l.get("arabic", l["key"]),
            "crystal_score_min":  int(cr.get("min", 0)),
            "crystal_score_max":  int(cr.get("max", 100)),
            "quran_reference":    l.get("quran_ref") or l.get("quran_reference"),
            "quran_arabic":       l.get("quran_ayah_ar") or l.get("quran_arabic"),
            "quran_urdu":         l.get("quran_ayah_ur") or l.get("quran_urdu"),
            "message_to_user":    l.get("description_ur") or l.get("message_to_user"),
            "encouragement":      l.get("app_tone_context") or l.get("encouragement"),
            "animation":          l.get("animation"),
            "sound":              l.get("sound"),
            "crystal_visual_state": l.get("crystal_visual_state"),
            "level_up_message":   l.get("level_up_message"),
            "level_down_message": l.get("level_down_message"),
        }
    return l


# Dimensions: {dim_key (lowercase) -> normalized config_dict}
DIMENSIONS: dict[str, dict] = {
    _normalize_dimension(d)["dimension_key"]: _normalize_dimension(d)
    for d in _dims_raw.get("dimensions", [])
}

# Nafs levels sorted by level_order ascending
_lvls_list = _lvls_raw.get("nafs_levels") or _lvls_raw.get("levels", [])
NAFS_LEVELS: list[dict] = sorted(
    [_normalize_level(l) for l in _lvls_list],
    key=lambda x: x["level_order"],
)

# Actions: support both full format {"actions":[...]} and patch format {"new_actions":[...]}
_acts_list: list[dict] = _acts_raw.get("actions") or _acts_raw.get("new_actions") or []


def _normalize_action(a: dict) -> dict:
    """
    Normalize v1.0 (new) and legacy action dicts to a unified schema.

    New format (v1.0):  "key", "primary_points", "primary_dimension" (UPPERCASE),
                        "daily_cap", "cap_period", "label_en"
    Old format:         "action_key", "points_primary", "dimension_primary" (lowercase),
                        "max_per_day", "action_name"
    """
    if "key" in a and "primary_points" in a:
        sec_dim = (a.get("secondary_dimension") or "").lower() or None
        daily_cap = a.get("daily_cap")
        return {
            "action_key":          a["key"],
            "action_name":         a.get("label_en") or a["key"],
            "source_module":       a.get("source_module", "module1"),
            "source_feature":      a.get("source_feature"),
            "category":            a.get("category"),
            "dimension_primary":   (a.get("primary_dimension") or "tazkiya").lower(),
            "points_primary":      int(a.get("primary_points") or 0),
            "dimension_secondary": sec_dim,
            "points_secondary":    int(a.get("secondary_points") or 0) if sec_dim else None,
            "max_per_day":         int(daily_cap) if daily_cap is not None else 1,
            "is_penalty":          bool(a.get("is_penalty", False)),
            "notes":               a.get("description") or "",
            "cap_period":          (a.get("cap_period") or "day").lower(),
            "variable_points_rule": a.get("variable_points_rule"),
        }
    return a


ACTIONS: dict[str, dict] = {}
for _raw_act in _acts_list:
    _norm_act = _normalize_action(_raw_act)
    if _norm_act.get("action_key"):
        ACTIONS[_norm_act["action_key"]] = _norm_act

# Daily max gain per dimension (from dimensions_config.json)
# Schema uses "key" + "max_single_day_gain" (legacy names also accepted)
DAILY_MAX_GAINS: dict[str, int] = {
    d.get("dimension_key") or d["key"]:
        int(d.get("daily_max_gain") or d.get("max_single_day_gain") or 999)
    for d in _dims_raw["dimensions"]
}

# Safe column mapping: dimension_key -> SQL column name in fitrah_user_dimensions
DIM_COLUMNS: dict[str, str] = {
    "taqwa":   "taqwa_score",
    "ilm":     "ilm_score",
    "tazkiya": "tazkiya_score",
    "ihsan":   "ihsan_score",
    "nafs":    "nafs_score",
    "maal":    "maal_score",
}

VALID_DIMENSIONS: frozenset = frozenset(DIM_COLUMNS.keys())

# Taqwa floor: if taqwa_score < this threshold, nafs level is capped at "nafs_e_mulhama"
TAQWA_FLOOR_THRESHOLD: int = 26  # taqwa must be >= 26 to go above nafs_e_mulhama


# ── Scoring functions ─────────────────────────────────────────────────────────

def calculate_crystal_score(dim_scores: dict) -> float:
    """
    Returns crystal_score (0.0 – 100.0) using the weighted formula:
      (taqwa*0.25) + (ilm*0.15) + (tazkiya*0.20) + (ihsan*0.15) + (nafs*0.10) + (maal*0.15)
    """
    total = 0.0
    for dim_key, cfg in DIMENSIONS.items():
        score = max(0.0, min(100.0, dim_scores.get(dim_key, 0) or 0))
        total += score * cfg["weight_in_crystal"]
    return round(total, 2)


def get_nafs_level(crystal_score: float, taqwa_score: float) -> dict:
    """
    Returns the nafs level dict that matches crystal_score.
    Applies taqwa floor rule: if taqwa_score < TAQWA_FLOOR_THRESHOLD,
    the nafs level is capped at 'nafs_e_mulhama' regardless of crystal_score.
    """
    # Walk levels in ascending order; the last one whose min <= score wins
    matched = NAFS_LEVELS[0]
    for level in NAFS_LEVELS:
        if crystal_score >= level["crystal_score_min"]:
            matched = level

    # Taqwa floor rule
    if taqwa_score < TAQWA_FLOOR_THRESHOLD:
        mulhama = next((l for l in NAFS_LEVELS if l["level_key"] == "nafs_e_mulhama"), None)
        if mulhama and matched["level_order"] > mulhama["level_order"]:
            return mulhama

    return matched


def get_nafs_progress_pct(crystal_score: float, level: dict) -> float:
    """Progress percentage within the current nafs level (0.0 – 100.0)."""
    low  = level["crystal_score_min"]
    high = level["crystal_score_max"]
    if high <= low:
        return 100.0
    pct = (crystal_score - low) / (high - low) * 100
    return round(max(0.0, min(100.0, pct)), 1)


def apply_decay(dim_scores: dict, last_active_at: datetime) -> dict:
    """
    Returns updated dimension scores after applying time-based decay.
    Each dimension has its own decay_rate_per_day and decay_starts_after_days.
    Scores never go below floor_score.
    """
    now = datetime.now(timezone.utc)
    if last_active_at.tzinfo is None:
        last_active_at = last_active_at.replace(tzinfo=timezone.utc)

    hours_inactive = (now - last_active_at).total_seconds() / 3600
    new_scores = {k: float(v or 0) for k, v in dim_scores.items()}

    for dim_key, cfg in DIMENSIONS.items():
        decay_start_days = float(cfg.get("decay_starts_after_days", 1))
        decay_rate       = float(cfg.get("decay_rate_per_day", 0))
        floor            = float(cfg.get("floor_score", 0))

        days_eligible = (hours_inactive / 24.0) - decay_start_days
        if days_eligible > 0 and decay_rate > 0:
            decay_amount = decay_rate * days_eligible
            current = new_scores.get(dim_key, 0.0)
            new_scores[dim_key] = max(floor, current - decay_amount)

    return new_scores


def get_weakest_dimension(dim_scores: dict) -> str:
    """Returns the key of the dimension with the lowest score."""
    return min(VALID_DIMENSIONS, key=lambda d: dim_scores.get(d, 0) or 0)


def get_strongest_dimension(dim_scores: dict) -> str:
    """Returns the key of the dimension with the highest score."""
    return max(VALID_DIMENSIONS, key=lambda d: dim_scores.get(d, 0) or 0)


# ── Spiritual State Engine ────────────────────────────────────────────────────

# Priority order (highest → lowest): present_with_allah, recovering, healing, consistent,
#   serving, rising, seeking, ghafil, struggling
SPIRITUAL_STATES = [
    "present_with_allah",
    "recovering", "healing", "consistent", "serving", "rising",
    "seeking", "ghafil", "struggling",
]

_STATE_META = {
    "present_with_allah": {"label": "Present with Allah", "tone": "Reverent",        "urdu": "Haziri mein rehein", "emoji": "✨"},
    "seeking":            {"label": "Seeking",            "tone": "Exploratory",     "urdu": "Dhundhte raho",      "emoji": "🔍"},
    "struggling":         {"label": "Struggling",         "tone": "Compassionate",   "urdu": "Koshish karna hi amal hai", "emoji": "🌧️"},
    "healing":            {"label": "Healing",            "tone": "Warm",            "urdu": "Wapas aa rahe ho",   "emoji": "🌱"},
    "rising":             {"label": "Rising",             "tone": "Encouraging",     "urdu": "Aage badho",         "emoji": "📈"},
    "serving":            {"label": "Serving",            "tone": "Purpose-driven",  "urdu": "Khidmat mein ho",    "emoji": "🤲"},
    "consistent":         {"label": "Consistent",         "tone": "Celebratory",     "urdu": "MashAllah",          "emoji": "⭐"},
    "recovering":         {"label": "Recovering",         "tone": "No-shame",        "urdu": "Al-Awwaboon",        "emoji": "💚"},
    "ghafil":             {"label": "Ghafil",             "tone": "Gentle-reminder", "urdu": "Wapas aa jao",       "emoji": "😴"},
}


def determine_spiritual_state(
    crystal_score: float,
    dim_scores: dict,
    streak_current: int,
    tawbah_streak_current: int,
    recent_penalty: bool,            # any penalty logged in last 7 days
    crystal_prev: float = 0.0,       # previous crystal score (0 = unknown)
    consecutive_ghafil_days: int = 0, # days in a row user logged ghafil qalb state
) -> str:
    """
    Determines the user's current Spiritual State from 9 options (v3-aligned).
    Priority: present_with_allah > recovering > healing > consistent > serving
              > rising > seeking > ghafil > struggling
    """
    ihsan = dim_scores.get("ihsan", 0)

    # Highest station: sustained high crystal + very high IHSAN
    if crystal_score >= 85 and ihsan >= 80:
        return "present_with_allah"

    if recent_penalty:
        return "recovering"

    # 3+ consecutive ghafil qalb states → ghafil spiritual state
    if consecutive_ghafil_days >= 3:
        return "ghafil"

    if tawbah_streak_current > 0 and (crystal_prev == 0.0 or crystal_score >= crystal_prev):
        return "healing"

    if streak_current >= 7:
        return "consistent"

    if ihsan >= 70:
        return "serving"

    if 65 <= crystal_score <= 80 and streak_current >= 3:
        return "rising"

    if crystal_score <= 50 or (crystal_prev > 0 and crystal_score < crystal_prev - 3):
        return "struggling"

    return "seeking"


def get_spiritual_state_meta(state: str) -> dict:
    """Returns display metadata for a spiritual state key."""
    return _STATE_META.get(state, _STATE_META["seeking"])


# ── Barakah Score ─────────────────────────────────────────────────────────────

# Barakah formula: niyyah_mult × (focus + (6 − distraction) + state_mult)
# Multipliers aligned with JS v3 fitrah_ai_jobs.js STATE_BARAKAH_MULTIPLIERS
_STATE_BARAKAH_MULT: dict[str, float] = {
    "struggling":         1.0,
    "ghafil":             1.0,
    "recovering":         1.0,
    "seeking":            1.5,
    "healing":            1.5,
    "rising":             2.0,
    "serving":            2.0,
    "consistent":         2.5,
    "present_with_allah": 3.0,
}
_BARAKAH_RAW_MAX: float = 13.0  # max: focus=5 + (6−1) + state_mult=3


def calculate_barakah_score(
    niyyah_confirmed: bool,
    focus_level: int,       # 1-5
    distraction_level: int, # 1-5 (lower = less distracted = better)
    spiritual_state: str,
) -> float:
    """
    PDF formula: niyyah_multiplier × (focus_rating + (6 − distraction_rating) + state_mult)
    niyyah_multiplier = 1 or 0 — no niyyah means score is 0 (actions are by intention).
    Result scaled 0-100 (raw max = 13).
    """
    if not niyyah_confirmed:
        return 0.0
    f   = max(1, min(5, focus_level))
    d   = max(1, min(5, distraction_level))
    sm  = float(_STATE_BARAKAH_MULT.get(spiritual_state, 1))
    raw = f + (6 - d) + sm          # range 3-13
    return round((raw / _BARAKAH_RAW_MAX) * 100.0, 1)


def barakah_to_points(barakah_score: float) -> int:
    """Maps barakah_score to dimension points awarded.
    Score 0 (no niyyah) → 0 points. 'Innama al-amal bi-n-niyyaat.'
    """
    if barakah_score <= 0:
        return 0
    if barakah_score >= 70:
        return 8
    if barakah_score >= 40:
        return 6
    return 4


# ── Spiritual Resilience ──────────────────────────────────────────────────────

def calculate_resilience_score(relapses: list[dict]) -> dict:
    """
    relapses: list of dicts with keys: relapse_at (datetime), recovered_at (datetime | None)

    Returns:
        {
          score: float (0-100),
          label: str,
          total_relapses: int,
          recovered_count: int,
          avg_recovery_days: float,
        }
    """
    if not relapses:
        return {"score": 100.0, "label": "No relapses — consistent", "total_relapses": 0,
                "recovered_count": 0, "avg_recovery_days": 0.0}

    total     = len(relapses)
    recovered = [r for r in relapses if r.get("recovered_at")]
    rec_count = len(recovered)

    if not recovered:
        label = "Rebuilding — keep going"
        score = max(0.0, 30.0 - (total * 5))
        return {"score": round(score, 1), "label": label, "total_relapses": total,
                "recovered_count": 0, "avg_recovery_days": 0.0}

    recovery_days = []
    for r in recovered:
        delta = (r["recovered_at"] - r["relapse_at"]).total_seconds() / 86400
        recovery_days.append(delta)

    avg_days = sum(recovery_days) / len(recovery_days)

    # Base: recovery ratio (what % of relapses led to recovery)
    recovery_ratio = rec_count / total  # 0.0-1.0

    # Speed bonus: < 1 day = excellent, 1-3 = good, 3-7 = ok, > 7 = slow
    if avg_days < 1:
        speed_score = 40.0
    elif avg_days < 3:
        speed_score = 30.0
    elif avg_days < 7:
        speed_score = 20.0
    else:
        speed_score = 10.0

    score = round(min(100.0, (recovery_ratio * 60) + speed_score), 1)

    if score >= 80:
        label = "Growing — Awwaboon strength"
    elif score >= 60:
        label = "Recovering — on the right path"
    elif score >= 40:
        label = "Rebuilding — keep returning"
    else:
        label = "Struggling — tawbah is always open"

    return {
        "score":             score,
        "label":             label,
        "total_relapses":    total,
        "recovered_count":   rec_count,
        "avg_recovery_days": round(avg_days, 1),
    }


# ── Sunnah DNA ────────────────────────────────────────────────────────────────

_SUNNAH_DNA_MAP: dict[str, dict[str, str]] = {
    "HP_01": {
        "A": "consistent",   # Prays Fajr + Quran/zikr
        "B": "basic",        # Prays then sleeps
        "C": "irregular",    # Sometimes prays
        "D": "developing",   # Usually misses Fajr
    },
    "HP_09": {
        "A": "sunnah",       # Follows Sunnah health habits
        "B": "aware",        # Some awareness, wants to improve
        "C": "neglected",    # Busy life, little attention
        "D": "poor",         # Very little attention
    },
    "HP_12": {
        "A": "sunnah",       # Isha + Surah Mulk/zikr then sleep
        "B": "screen",       # Phone scroll then sleep
        "C": "no_routine",   # No night routine
        "D": "late_night",   # Stays up late, Fajr suffers
    },
    "HP_07": {
        "A": "strong",       # Daily family time
        "B": "moderate",     # Weekly contact
        "C": "weak",         # Tries but falls short
        "D": "disconnected", # Relationships have cooled
    },
}


# Label → 0-100 numeric scores for sunnah_dna_* DB columns
_SUNNAH_DNA_LABEL_SCORE: dict[str, int] = {
    # ibadah (HP_01)
    "consistent":  100,
    "basic":        75,
    "irregular":    40,
    "developing":   20,
    # eating (HP_09)
    "sunnah":      100,
    "aware":        60,
    "neglected":    30,
    "poor":         10,
    # sleeping (HP_12) — shares "sunnah" + own labels
    "screen":       50,
    "no_routine":   30,
    "late_night":   15,
    # social (HP_07)
    "strong":      100,
    "moderate":     70,
    "weak":         40,
    "disconnected": 15,
    # fallback
    "unknown":       0,
}


def extract_sunnah_dna(answers: list[dict]) -> dict:
    """
    Extract Sunnah DNA labels from specific profiler answers.
    HP_01 → ibadah, HP_07 → social, HP_09 → eating, HP_12 → sleeping
    Returns string labels (consistent / sunnah / strong / …).
    """
    selected: dict[str, str] = {}
    for ans in answers:
        qid   = ans.get("question_id", "")
        label = (ans.get("selected_label") or "").upper()
        if qid in _SUNNAH_DNA_MAP:
            selected[qid] = _SUNNAH_DNA_MAP[qid].get(label, "unknown")

    return {
        "ibadah":   selected.get("HP_01", "unknown"),
        "eating":   selected.get("HP_09", "unknown"),
        "sleeping": selected.get("HP_12", "unknown"),
        "social":   selected.get("HP_07", "unknown"),
    }


def sunnah_dna_to_scores(dna: dict) -> dict:
    """
    Convert string Sunnah DNA labels to 0-100 numeric scores
    suitable for the sunnah_dna_* REAL columns in fitrah_users.
    """
    return {
        "ibadah":   _SUNNAH_DNA_LABEL_SCORE.get(dna.get("ibadah",   "unknown"), 0),
        "eating":   _SUNNAH_DNA_LABEL_SCORE.get(dna.get("eating",   "unknown"), 0),
        "sleeping": _SUNNAH_DNA_LABEL_SCORE.get(dna.get("sleeping", "unknown"), 0),
        "social":   _SUNNAH_DNA_LABEL_SCORE.get(dna.get("social",   "unknown"), 0),
    }


def get_cap_period_days(action: dict) -> int:
    """
    For actions with max_per_day=0, return the cap window in days.
    Checks cap_period field first (v1.0 format), falls back to notes parsing (legacy).
    Returns 365 (yearly/once), 90 (quarterly), 30 (monthly), or 7 (weekly default).
    """
    cap_p = (action.get("cap_period") or "").lower()
    if cap_p in ("year", "yearly", "annual", "once"):
        return 365
    if cap_p in ("quarter", "quarterly"):
        return 90
    if cap_p in ("month", "monthly"):
        return 30
    if cap_p in ("week", "weekly", "day"):
        return 7

    # Legacy fallback: infer from notes / action_key text
    notes = (action.get("notes") or action.get("description") or "").lower()
    key   = (action.get("action_key") or "").lower()
    if "quarterly" in notes or "3month" in key:
        return 90
    if "monthly" in notes:
        return 30
    return 7


# ── Profiler calculation ──────────────────────────────────────────────────────

_profiler_raw = _load("profiler_questions.json")

HABIT_QUESTIONS: dict[str, dict] = {
    (q.get("question_id") or q["id"]): q
    for q in _profiler_raw["habit_profiler"]["questions"]
}
NATURE_QUESTIONS: dict[str, dict] = {
    (q.get("question_id") or q["id"]): q
    for q in _profiler_raw["nature_profiler"]["questions"]
}
_UMMAH_ROLES: list[dict]  = _profiler_raw.get("ummah_role_mapping", {}).get("roles", [])
_MIZAJ_MAP:   dict        = _profiler_raw.get("mizaj_mapping", {})

# Max possible raw scores per dimension (from profiler JSON)
_MAX_RAW: dict[str, float] = {
    "taqwa":   100.0,
    "ilm":      40.0,
    "tazkiya":  65.0,
    "ihsan":    47.0,
    "nafs":     47.0,
    "maal":     45.0,
}
_MIN_INITIAL_SCORE: float = float(
    _profiler_raw.get("initial_score_calculation", {}).get("minimum_initial_score", 5)
)


def _determine_ummah_role(np_outputs: dict[str, str]) -> str:
    """Pick the ummah_role with the most matching trigger_outputs."""
    outputs = list(np_outputs.values())
    scores: dict[str, int] = {}
    for role in _UMMAH_ROLES:
        count = sum(1 for o in outputs if o in role["trigger_outputs"])
        if count:
            scores[role["role_key"]] = count
    return max(scores, key=scores.get) if scores else "wasatiyya"


def _determine_jalali_jamali(np_outputs: dict[str, str]) -> str:
    """Derived from NP_01, NP_04, NP_06 answers."""
    jalali_triggers = set(_MIZAJ_MAP["jalali_jamali"]["jalali"]["trigger_outputs"])
    jamali_triggers = set(_MIZAJ_MAP["jalali_jamali"]["jamali"]["trigger_outputs"])
    min_votes       = int(_MIZAJ_MAP["jalali_jamali"]["jalali"]["minimum_votes"])
    relevant        = ["NP_01", "NP_04", "NP_06"]

    jalali_count = sum(1 for q in relevant if np_outputs.get(q) in jalali_triggers)
    jamali_count = sum(1 for q in relevant if np_outputs.get(q) in jamali_triggers)

    if jalali_count >= min_votes:
        return "jalali"
    if jamali_count >= min_votes:
        return "jamali"
    return "mixed"


def _determine_introvert_extrovert(np_outputs: dict[str, str]) -> str:
    """Derived from NP_02, NP_06 answers."""
    ext_triggers  = set(_MIZAJ_MAP["introvert_extrovert"]["extrovert"]["trigger_outputs"])
    int_triggers  = set(_MIZAJ_MAP["introvert_extrovert"]["introvert"]["trigger_outputs"])
    min_votes     = int(_MIZAJ_MAP["introvert_extrovert"]["extrovert"]["minimum_votes"])
    relevant      = ["NP_02", "NP_06"]

    ext_count = sum(1 for q in relevant if np_outputs.get(q) in ext_triggers)
    int_count = sum(1 for q in relevant if np_outputs.get(q) in int_triggers)

    if ext_count >= min_votes:
        return "extrovert"
    if int_count >= min_votes:
        return "introvert"
    return "ambivert"


def calculate_profiler_scores(answers: list[dict]) -> dict:
    """
    Process profiler answers and return initial scores + mizaj.

    answers format:
        [{"question_id": "HP_01", "selected_label": "A"}, ...]

    Returns:
        {
          "dimension_scores": {"taqwa": 72.0, ...},
          "ummah_role": "ahl_ilm",
          "jalali_jamali": "mixed",
          "introvert_extrovert": "ambivert"
        }
    """
    raw:        dict[str, float] = {d: 0.0 for d in VALID_DIMENSIONS}
    np_outputs: dict[str, str]   = {}

    for ans in answers:
        qid   = ans.get("question_id", "")
        label = (ans.get("selected_label") or "").upper()

        if qid.startswith("HP_"):
            q = HABIT_QUESTIONS.get(qid)
            if not q:
                continue
            opt = next((o for o in q["options"] if o["label"] == label), None)
            if not opt:
                continue
            dp = q.get("dimension_primary")
            ds = q.get("dimension_secondary")
            if dp and dp in raw:
                raw[dp] += float(opt.get("score_primary") or 0)
            if ds and ds in raw:
                raw[ds] += float(opt.get("score_secondary") or 0)

        elif qid.startswith("NP_"):
            q = NATURE_QUESTIONS.get(qid)
            if not q:
                continue
            opt = next((o for o in q["options"] if o["label"] == label), None)
            if opt:
                np_outputs[qid] = opt.get("output", "")

    # Normalise to 0-100, floor at minimum_initial_score
    final: dict[str, float] = {}
    for dim, raw_val in raw.items():
        max_raw    = _MAX_RAW.get(dim, 100.0)
        normalised = (raw_val / max_raw * 100.0) if max_raw > 0 else 0.0
        final[dim] = max(_MIN_INITIAL_SCORE, round(normalised, 1))

    return {
        "dimension_scores":     final,
        "ummah_role":           _determine_ummah_role(np_outputs),
        "jalali_jamali":        _determine_jalali_jamali(np_outputs),
        "introvert_extrovert":  _determine_introvert_extrovert(np_outputs),
        "sunnah_dna":           extract_sunnah_dna(answers),
    }
