"""
Engine 3 — Habit Breaking Flow.

Components: Shaytan Pattern Recognition, Islamic Replacements (trigger→Sunnah),
Relapse Prediction, Emergency Mode, Muhajir Protocol.
"""
import re
from database import get_db_connection, release_db_connection
from tawbah_os.encryption import encrypt
from tawbah_os.data_loader import (
    ISLAMIC_REPLACEMENTS,
    RELAPSE_PREDICTION,
    BAD_HABITS_SUBTYPES,
    INTERNAL_DIALOGUE,
)


def find_replacement(trigger_text: str) -> list[dict]:
    """Keyword match against replacements; return top matches."""
    if not trigger_text:
        return []
    t = trigger_text.lower()
    matches = []
    reps = ISLAMIC_REPLACEMENTS.get("replacements", []) if isinstance(ISLAMIC_REPLACEMENTS, dict) else []
    for rep in reps:
        score = 0
        for kw in rep.get("trigger_keywords", []):
            if kw.lower() in t:
                score += 1
        if score > 0:
            matches.append((score, rep))
    matches.sort(key=lambda x: -x[0])
    return [m[1] for m in matches[:3]]


def log_shaytan_pattern(user_id: str, trigger_time: str, location: str,
                        emotion: str, gunah_category: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_shaytan_patterns (
                    user_id, trigger_time, location_enc, emotion_enc,
                    gunah_category, logged_at
                ) VALUES (%s, %s, %s, %s, %s, now())
                RETURNING id
            """, (
                user_id, trigger_time,
                encrypt(location, user_id),
                encrypt(emotion, user_id),
                gunah_category,
            ))
            pid = c.fetchone()[0]
            conn.commit()
            return pid
    finally:
        release_db_connection(conn)


def log_relapse(user_id: str, session_id: int, context: str,
                minutes_before_predicted: int = None) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_relapse_log (
                    session_id, user_id, context_enc,
                    minutes_before_predicted, logged_at
                ) VALUES (%s, %s, %s, %s, now())
                RETURNING id
            """, (
                session_id, user_id,
                encrypt(context, user_id),
                minutes_before_predicted,
            ))
            rid = c.fetchone()[0]
            conn.commit()
            return rid
    finally:
        release_db_connection(conn)


def predict_next_risk_window(user_id: str) -> dict | None:
    """Aggregate last 30 days of shaytan patterns to estimate next high-risk window."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT trigger_time, COUNT(*) AS n
                FROM tawbah_shaytan_patterns
                WHERE user_id = %s
                  AND logged_at > now() - INTERVAL '30 days'
                GROUP BY trigger_time
                ORDER BY n DESC
                LIMIT 1
            """, (user_id,))
            row = c.fetchone()
            if not row or row[1] < 3:
                return None
            return {"predicted_window": row[0], "occurrences": row[1]}
    finally:
        release_db_connection(conn)


def get_bad_habit_subtypes() -> dict:
    return BAD_HABITS_SUBTYPES if isinstance(BAD_HABITS_SUBTYPES, dict) else {}


def get_internal_dialogue_corrections() -> dict:
    return INTERNAL_DIALOGUE if isinstance(INTERNAL_DIALOGUE, dict) else {}


def emergency_mode_payload() -> dict:
    """Minimal urgent-intervention content — step-by-step crisis interrupt."""
    reps = find_replacement("urge aa rahi shahwat") or []
    primary = reps[0] if reps else None
    return {
        "title": "Emergency Mode",
        "immediate_steps": primary.get("islamic_replacement") if primary else {},
        "follow_up": "Wudu + change position + 2 rakat — break the loop.",
    }
