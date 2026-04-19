"""
data_loader.py — Loads all Tawbah OS JSON configs once at startup.

Also re-exports 3 shared configs from fitrah_engine/data to avoid duplication:
  crisis_safe_ayaat, qalb_state_opening_lines, fiqh_rulings_kafarat.
"""
import json
import os

_TAWBAH_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_FITRAH_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fitrah_engine", "data"
)

_SHARED_FITRAH_FILES = {
    "crisis_safe_ayaat.json",
    "qalb_state_opening_lines.json",
    "fiqh_rulings_kafarat.json",
}


def _load(filename: str) -> dict | list:
    if filename in _SHARED_FITRAH_FILES:
        path = os.path.join(_FITRAH_DATA_DIR, filename)
        if not os.path.exists(path):
            path = os.path.join(_TAWBAH_DATA_DIR, filename)
    else:
        path = os.path.join(_TAWBAH_DATA_DIR, filename)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


ONBOARDING              = _load("onboarding_screens.json")
TIER_DETECTION          = _load("tier_detection_rules.json")
ENGINE_TIER_ALLOWANCE   = _load("engine_tier_allowance.json")
TAWBAH_ACTIONS          = _load("tawbah_actions_config.json")
MUHASABA_CONFIG         = _load("muhasaba_engine_config.json")
WEEKLY_MUHASABA_QS      = _load("weekly_muhasaba_questions.json")
KAFFARAH_CONFIG         = _load("kaffarah_engine_config.json")
AQAL_NAFS_NEGOTIATION   = _load("aqal_nafs_negotiation_config.json")
BAD_HABITS_SUBTYPES     = _load("bad_habits_subtypes.json")
INTERNAL_DIALOGUE       = _load("internal_dialogue_corrections.json")
ISLAMIC_REPLACEMENTS    = _load("islamic_replacements.json")
RELAPSE_PREDICTION      = _load("relapse_prediction_config.json")
RUHANI_FATIGUE          = _load("ruhani_fatigue_signals.json")
STREAK_MILESTONES       = _load("streak_milestones.json")
TAWBAH_NISHANIYAAN      = _load("tawbah_nishaniyaan.json")
SACRED_LINES            = _load("sacred_lines_rotation.json")
QABOOLIYAT_TIMES        = _load("qabooliyat_times_config.json")
TIER3_MUFTI_CASES       = _load("tier3_mufti_referral_cases.json")
HELPLINES_BY_COUNTRY    = _load("helplines_by_country.json")
CRISIS_DETECTION_PATTERNS = _load("crisis_detection_patterns.json")
MENTAL_HEALTH_BRIDGE    = _load("mental_health_bridge_config.json")
EXIT_PATHWAYS           = _load("exit_pathways_config.json")
EXTERNAL_FEATURE_LINKS  = _load("external_feature_links.json")

CRISIS_SAFE_AYAAT       = _load("crisis_safe_ayaat.json")
QALB_STATE_LINES        = _load("qalb_state_opening_lines.json")
FIQH_RULINGS_KAFARAT    = _load("fiqh_rulings_kafarat.json")

DUA_THERAPY_DB = None  # On hold — graceful absence

VALID_TIERS    = ("light", "medium", "severe")
VALID_FIQH     = ("hanafi", "shafi", "maliki", "hanbali", "ahle_hadith")
VALID_TONES    = ("urdu_english_mix", "urdu_formal", "english_formal",
                  "hindi_english_mix", "arabic_emphasized")
