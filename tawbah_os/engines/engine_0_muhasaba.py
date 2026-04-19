"""
Engine 0 — Muhasaba-e-Nafs.

Components:
  A. Daily 4-question muhasaba
  B. Weekly deep dive (4 categories: zuban, nafs, qalb, amal)
  C. Sin pattern detection awareness (light, reflective)
  D. Heart disease handoff to Tibb-e-Nabawi (awareness only, never treatment)
  E. Sahaba rotating inspiration snippets
  F. Niyyah clarity check
"""
from database import get_db_connection, release_db_connection
from tawbah_os.encryption import encrypt
from tawbah_os.data_loader import MUHASABA_CONFIG, WEEKLY_MUHASABA_QS


def daily_muhasaba(user_id: str, q1: str, q2: str, q3: str, q4: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_daily_muhasaba_log (
                    user_id, q1_answer_enc, q2_answer_enc,
                    q3_answer_enc, q4_answer_enc,
                    logged_date, logged_at
                ) VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, now())
                RETURNING id
            """, (
                user_id,
                encrypt(q1, user_id),
                encrypt(q2, user_id),
                encrypt(q3, user_id),
                encrypt(q4, user_id),
            ))
            mid = c.fetchone()[0]
            conn.commit()
            return mid
    finally:
        release_db_connection(conn)


def weekly_deep_dive(user_id: str, zuban: str, nafs: str,
                     qalb: str, amal: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_weekly_muhasaba_deep_log (
                    user_id, zuban_reflection_enc, nafs_reflection_enc,
                    qalb_reflection_enc, amal_reflection_enc,
                    week_ending, logged_at
                ) VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, now())
                RETURNING id
            """, (
                user_id,
                encrypt(zuban, user_id),
                encrypt(nafs, user_id),
                encrypt(qalb, user_id),
                encrypt(amal, user_id),
            ))
            wid = c.fetchone()[0]
            conn.commit()
            return wid
    finally:
        release_db_connection(conn)


def log_sin_pattern_observation(user_id: str, pattern_type: str,
                                signal_count: int, description: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_sin_pattern_observations (
                    user_id, pattern_type, signal_count,
                    pattern_description, first_detected, last_updated
                ) VALUES (%s, %s, %s, %s, now(), now())
                RETURNING id
            """, (user_id, pattern_type, signal_count, description))
            pid = c.fetchone()[0]
            conn.commit()
            return pid
    finally:
        release_db_connection(conn)


def log_heart_disease_handoff(user_id: str, disease: str, signals_count: int,
                              user_response: str = None) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_heart_disease_handoffs (
                    user_id, disease_detected, signals_count,
                    handoff_offered_at, user_response
                ) VALUES (%s, %s, %s, now(), %s)
                RETURNING id
            """, (user_id, disease, signals_count, user_response))
            hid = c.fetchone()[0]
            conn.commit()
            return hid
    finally:
        release_db_connection(conn)


def get_daily_questions() -> list[dict]:
    cfg = MUHASABA_CONFIG.get("components", {}).get("A_daily_muhasaba_tool", {})
    return cfg.get("flow", {}).get("questions", [])


def get_weekly_categories() -> list[dict]:
    cfg = MUHASABA_CONFIG.get("components", {}).get("B_weekly_muhasaba_deep_dive", {})
    return cfg.get("categories", [])


def get_weekly_questions_raw() -> dict:
    return WEEKLY_MUHASABA_QS if isinstance(WEEKLY_MUHASABA_QS, dict) else {}


def get_sahaba_snippet(rotation_index: int = 0) -> dict | None:
    cfg = MUHASABA_CONFIG.get("components", {}).get("E_sahaba_muhasaba_examples", {})
    snippets = cfg.get("snippets", [])
    if not snippets:
        return None
    return snippets[rotation_index % len(snippets)]


def get_heart_disease_signals() -> dict:
    cfg = MUHASABA_CONFIG.get("components", {}).get("D_heart_disease_handoff_to_tibb", {})
    return cfg.get("awareness_triggers", {})
