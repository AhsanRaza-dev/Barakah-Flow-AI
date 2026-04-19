"""
Engine 4 — Istiqamah Tracker + Streak Milestones + Ruhani Fatigue Detector.
"""
from datetime import datetime, timedelta, timezone
from database import get_db_connection, release_db_connection
from tawbah_os.data_loader import STREAK_MILESTONES, RUHANI_FATIGUE


def get_chapter_streak(user_id: str, chapter_id: str) -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT streak_start_date, current_day_count, last_active_date,
                       max_streak_achieved
                FROM tawbah_istiqamah_chapters
                WHERE user_id = %s AND chapter_id = %s
            """, (user_id, chapter_id))
            r = c.fetchone()
            if not r:
                return {"current_day_count": 0, "max_streak_achieved": 0}
            return {
                "streak_start_date": r[0],
                "current_day_count": r[1],
                "last_active_date": r[2],
                "max_streak_achieved": r[3],
            }
    finally:
        release_db_connection(conn)


def tick_chapter_day(user_id: str, chapter_id: str) -> dict:
    """Mark today active for this chapter — advance streak by 1 day if eligible."""
    today = datetime.now(timezone.utc).date()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT streak_start_date, current_day_count, last_active_date,
                       max_streak_achieved
                FROM tawbah_istiqamah_chapters
                WHERE user_id = %s AND chapter_id = %s
                FOR UPDATE
            """, (user_id, chapter_id))
            r = c.fetchone()
            if not r:
                c.execute("""
                    INSERT INTO tawbah_istiqamah_chapters (
                        user_id, chapter_id, streak_start_date,
                        current_day_count, last_active_date, max_streak_achieved
                    ) VALUES (%s, %s, %s, 1, %s, 1)
                """, (user_id, chapter_id, today, today))
                conn.commit()
                return {"current_day_count": 1, "milestone_hit": None}
            start, cur, last, mx = r
            if last == today:
                conn.commit()
                return {"current_day_count": cur, "milestone_hit": None}
            if last == today - timedelta(days=1):
                cur += 1
            else:
                # gap: reset streak
                cur = 1
                start = today
            mx = max(mx or 0, cur)
            c.execute("""
                UPDATE tawbah_istiqamah_chapters
                SET streak_start_date = %s,
                    current_day_count = %s,
                    last_active_date = %s,
                    max_streak_achieved = %s
                WHERE user_id = %s AND chapter_id = %s
            """, (start, cur, today, mx, user_id, chapter_id))
            conn.commit()
            return {
                "current_day_count": cur,
                "milestone_hit": check_milestone(cur),
            }
    finally:
        release_db_connection(conn)


def check_milestone(day_count: int) -> dict | None:
    milestones = STREAK_MILESTONES.get("milestones", []) if isinstance(STREAK_MILESTONES, dict) else []
    for m in milestones:
        if m.get("day_threshold") == day_count:
            return m
    return None


def record_relapse_reset(user_id: str, chapter_id: str) -> dict:
    """User relapsed — reset streak but preserve journey moments."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                UPDATE tawbah_istiqamah_chapters
                SET current_day_count = 0,
                    streak_start_date = NULL
                WHERE user_id = %s AND chapter_id = %s
                RETURNING max_streak_achieved
            """, (user_id, chapter_id))
            r = c.fetchone()
            conn.commit()
            return {"max_streak_preserved": r[0] if r else 0}
    finally:
        release_db_connection(conn)


_FATIGUE_MIN_SIGNALS = 3
_FATIGUE_MIN_WEIGHT = 0.65


def evaluate_ruhani_fatigue(active_signal_ids: list[str]) -> dict:
    """Weighted-sum detection. active_signal_ids is a pre-evaluated list."""
    cfg = RUHANI_FATIGUE if isinstance(RUHANI_FATIGUE, dict) else {}
    signals = cfg.get("detection_signals", {}).get("signal_categories", [])
    weight_map = {s["signal_id"]: s.get("weight", 0) for s in signals}
    total = sum(weight_map.get(sid, 0) for sid in active_signal_ids)
    triggered = (len(active_signal_ids) >= _FATIGUE_MIN_SIGNALS
                 and total >= _FATIGUE_MIN_WEIGHT)
    return {
        "fatigue_detected": triggered,
        "composite_weight": round(total, 3),
        "active_signals": active_signal_ids,
        "prescription": cfg.get("intervention_prescription", {}) if triggered else None,
    }


def log_fatigue_detection(user_id: str, active_signals: list[str],
                          composite_weight: float) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_ruhani_fatigue_detections (
                    user_id, signals_active, composite_weight,
                    detected_at
                ) VALUES (%s, %s, %s, now())
                RETURNING id
            """, (user_id, active_signals, composite_weight))
            fid = c.fetchone()[0]
            conn.commit()
            return fid
    finally:
        release_db_connection(conn)
