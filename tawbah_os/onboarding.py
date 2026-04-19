"""
onboarding.py — Tawbah OS 5-screen onboarding flow.

Screens:
  1. Welcome + niyyah framing
  2. Fiqh school selection
  3. Tone preference
  4. Country (for helplines) + tier preference (optional)
  5. Privacy & consent (AES-256 on-device encryption explained)
"""
import json
from database import get_db_connection, release_db_connection
from tawbah_os.data_loader import (
    ONBOARDING,
    VALID_FIQH,
    VALID_TIERS,
    VALID_TONES,
)


def get_screen(screen_no: int) -> dict:
    cfg = ONBOARDING if isinstance(ONBOARDING, dict) else {}
    screens = cfg.get("screens") or cfg.get("onboarding_screens") or []
    if not screens:
        return {"screen": screen_no, "missing": True}
    for s in screens:
        if s.get("screen_no") == screen_no or s.get("order") == screen_no:
            return s
    idx = max(1, min(screen_no, len(screens))) - 1
    return screens[idx]


def get_all_screens() -> list[dict]:
    cfg = ONBOARDING if isinstance(ONBOARDING, dict) else {}
    return cfg.get("screens") or cfg.get("onboarding_screens") or []


def start_onboarding(user_id: str) -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_onboarding_state (
                    user_id, current_screen, started_at
                ) VALUES (%s, 1, now())
                ON CONFLICT (user_id) DO UPDATE
                SET current_screen = 1, started_at = now(),
                    completed_at = NULL, profile_snapshot = NULL
            """, (user_id,))
            conn.commit()
        return {"user_id": user_id, "current_screen": 1,
                "screen": get_screen(1)}
    finally:
        release_db_connection(conn)


def advance_screen(user_id: str, next_screen: int) -> dict:
    if next_screen < 1 or next_screen > 5:
        raise ValueError("next_screen must be 1..5")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                UPDATE tawbah_onboarding_state
                SET current_screen = %s
                WHERE user_id = %s
            """, (next_screen, user_id))
            conn.commit()
        return {"user_id": user_id, "current_screen": next_screen,
                "screen": get_screen(next_screen)}
    finally:
        release_db_connection(conn)


def save_profile(user_id: str, fiqh_school: str = "hanafi",
                 tone_preference: str = "urdu_english_mix",
                 country_code: str = None,
                 tier_preference: str = None) -> dict:
    if fiqh_school not in VALID_FIQH:
        raise ValueError(f"fiqh_school must be one of {VALID_FIQH}")
    if tone_preference not in VALID_TONES:
        raise ValueError(f"tone_preference must be one of {VALID_TONES}")
    if tier_preference is not None and tier_preference not in VALID_TIERS:
        raise ValueError(f"tier_preference must be one of {VALID_TIERS}")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_user_profile (
                    user_id, fiqh_school, tone_preference,
                    country_code, tier_preference, onboarded_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (user_id) DO UPDATE SET
                    fiqh_school = EXCLUDED.fiqh_school,
                    tone_preference = EXCLUDED.tone_preference,
                    country_code = EXCLUDED.country_code,
                    tier_preference = EXCLUDED.tier_preference,
                    updated_at = now()
            """, (user_id, fiqh_school, tone_preference,
                  country_code, tier_preference))
            conn.commit()
        return get_profile(user_id)
    finally:
        release_db_connection(conn)


def get_profile(user_id: str) -> dict | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT fiqh_school, tone_preference, country_code,
                       tier_preference, onboarded_at
                FROM tawbah_user_profile WHERE user_id = %s
            """, (user_id,))
            r = c.fetchone()
            if not r:
                return None
            return {
                "user_id": user_id,
                "fiqh_school": r[0],
                "tone_preference": r[1],
                "country_code": r[2],
                "tier_preference": r[3],
                "onboarded_at": r[4],
            }
    finally:
        release_db_connection(conn)


def complete_onboarding(user_id: str, profile: dict) -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                UPDATE tawbah_onboarding_state
                SET current_screen = 5,
                    completed_at = now(),
                    profile_snapshot = %s
                WHERE user_id = %s
            """, (json.dumps(profile, ensure_ascii=False), user_id))
            conn.commit()
        return {"user_id": user_id, "onboarded": True, "profile": profile}
    finally:
        release_db_connection(conn)


def is_onboarded(user_id: str) -> bool:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT completed_at FROM tawbah_onboarding_state
                WHERE user_id = %s
            """, (user_id,))
            r = c.fetchone()
            return bool(r and r[0])
    finally:
        release_db_connection(conn)
