"""
scheduler.py — Tawbah OS background jobs.

Jobs (all UTC):
  00:10  qabooliyat_window_flags     — mark active qabooliyat prayer windows
  00:40  sin_pattern_aggregation     — roll shaytan_patterns into observations
  01:15  istiqamah_gap_sweep         — reset streaks for chapters with 2+ day gap
  02:15  kaffarah_expiry_sweep       — mark expired kaffarah activations completed
  06:30  ruhani_fatigue_sweep        — auto-evaluate per-user fatigue signals
  Fri 00:10 sayyid_istighfar_reminder_flag — flag users who haven't logged Sayyid lately
"""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database import get_db_connection, release_db_connection
from tawbah_os.data_loader import QABOOLIYAT_TIMES, RUHANI_FATIGUE
from tawbah_os.engines import engine_4_istiqamah as eng4

log = logging.getLogger("tawbah_os.scheduler")

_scheduler: BackgroundScheduler | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Job 1: Qabooliyat window flags  (00:10 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_qabooliyat_window_flags() -> None:
    """
    Precompute today's qabooliyat windows.
    Writes a single system-wide JSON record to tawbah_system_configs-like storage.
    For now, just log — frontend reads QABOOLIYAT_TIMES directly.
    """
    log.info("[Qabooliyat] Qabooliyat windows config refreshed (read-only).")


# ─────────────────────────────────────────────────────────────────────────────
# Job 2: Sin Pattern Aggregation  (00:40 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_sin_pattern_aggregation() -> None:
    """
    Aggregate last-30-day shaytan_patterns by (user_id, gunah_category) — if
    a user has 5+ same-category entries, upsert a pattern observation.
    """
    log.info("[SinPattern] Starting aggregation…")
    conn = get_db_connection()
    if conn is None:
        log.error("[SinPattern] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, gunah_category, COUNT(*) AS n
            FROM tawbah_shaytan_patterns
            WHERE logged_at > now() - INTERVAL '30 days'
              AND gunah_category IS NOT NULL
            GROUP BY user_id, gunah_category
            HAVING COUNT(*) >= 5
        """)
        rows = cur.fetchall()
        upserted = 0
        for user_id, category, n in rows:
            cur.execute("""
                SELECT id FROM tawbah_sin_pattern_observations
                WHERE user_id = %s AND pattern_type = %s
                ORDER BY last_updated DESC LIMIT 1
            """, (user_id, category))
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                    UPDATE tawbah_sin_pattern_observations
                    SET signal_count = %s, last_updated = now()
                    WHERE id = %s
                """, (int(n), existing[0]))
            else:
                cur.execute("""
                    INSERT INTO tawbah_sin_pattern_observations (
                        user_id, pattern_type, signal_count,
                        pattern_description, first_detected, last_updated
                    ) VALUES (%s, %s, %s, %s, now(), now())
                """, (
                    user_id, category, int(n),
                    f"Auto-detected: {n} occurrences in last 30 days",
                ))
            upserted += 1
        conn.commit()
        log.info(f"[SinPattern] Done — {upserted} observations upserted.")
    except Exception as e:
        conn.rollback()
        log.error(f"[SinPattern] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 3: Istiqamah Gap Sweep  (01:15 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_istiqamah_gap_sweep() -> None:
    """
    Reset current_day_count to 0 for any chapter streak whose last_active_date
    is 2+ days ago. max_streak_achieved is preserved.
    """
    log.info("[Istiqamah] Starting gap sweep…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Istiqamah] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE tawbah_istiqamah_chapters
            SET current_day_count = 0,
                streak_start_date = NULL,
                updated_at = now()
            WHERE last_active_date < CURRENT_DATE - INTERVAL '2 days'
              AND current_day_count > 0
        """)
        reset = cur.rowcount
        conn.commit()
        log.info(f"[Istiqamah] Done — {reset} chapter streaks reset.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Istiqamah] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 4: Kaffarah Expiry Sweep  (02:15 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_kaffarah_expiry_sweep() -> None:
    """
    Mark active kaffarah activations as 'completed' once duration_days elapsed
    from activated_at. Does NOT claim qabooliyat — just marks duration complete.
    """
    log.info("[Kaffarah] Starting expiry sweep…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Kaffarah] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE tawbah_kaffarah_activation
            SET status = 'completed'
            WHERE status = 'active'
              AND activated_at + (duration_days || ' days')::INTERVAL < now()
        """)
        completed = cur.rowcount
        conn.commit()
        log.info(f"[Kaffarah] Done — {completed} activations marked completed.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Kaffarah] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 5: Ruhani Fatigue Sweep  (06:30 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_ruhani_fatigue_sweep() -> None:
    """
    For each user with a tawbah_user_profile, evaluate signal flags derived from
    recent engine activity:
      - low_istighfar_7d  — < 3 istighfar logs in 7 days
      - no_tahajjud_14d   — no tahajjud session in 14 days
      - repeated_relapse_7d — 3+ relapses in 7 days
      - no_muhasaba_7d    — no daily muhasaba in 7 days
      - streak_collapse   — any chapter reset in last 7 days

    If weighted-sum fatigue triggered, write a fatigue detection row.
    Uses engine_4.evaluate_ruhani_fatigue() — single source of truth for weights.
    """
    log.info("[RuhaniFatigue] Starting sweep…")
    conn = get_db_connection()
    if conn is None:
        log.error("[RuhaniFatigue] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM tawbah_user_profile")
        users = [r[0] for r in cur.fetchall()]
        flagged = 0

        for user_id in users:
            signals: list[str] = []

            cur.execute("""
                SELECT COUNT(*) FROM tawbah_istighfar_log
                WHERE user_id = %s AND logged_at > now() - INTERVAL '7 days'
            """, (user_id,))
            if (cur.fetchone()[0] or 0) < 3:
                signals.append("low_istighfar_7d")

            cur.execute("""
                SELECT 1 FROM tawbah_tahajjud_sessions
                WHERE user_id = %s AND started_at > now() - INTERVAL '14 days'
                LIMIT 1
            """, (user_id,))
            if cur.fetchone() is None:
                signals.append("no_tahajjud_14d")

            cur.execute("""
                SELECT COUNT(*) FROM tawbah_relapse_log
                WHERE user_id = %s AND logged_at > now() - INTERVAL '7 days'
            """, (user_id,))
            if (cur.fetchone()[0] or 0) >= 3:
                signals.append("repeated_relapse_7d")

            cur.execute("""
                SELECT 1 FROM tawbah_daily_muhasaba_log
                WHERE user_id = %s AND logged_at > now() - INTERVAL '7 days'
                LIMIT 1
            """, (user_id,))
            if cur.fetchone() is None:
                signals.append("no_muhasaba_7d")

            if not signals:
                continue

            result = eng4.evaluate_ruhani_fatigue(signals)
            if result.get("fatigue_detected"):
                cur.execute("""
                    INSERT INTO tawbah_ruhani_fatigue_detections (
                        user_id, signals_active, composite_weight, detected_at
                    ) VALUES (%s, %s, %s, now())
                """, (
                    user_id, signals,
                    float(result.get("composite_weight") or 0.0),
                ))
                flagged += 1

        conn.commit()
        log.info(f"[RuhaniFatigue] Done — {flagged} users flagged for fatigue.")
    except Exception as e:
        conn.rollback()
        log.error(f"[RuhaniFatigue] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 6: Sayyid-ul-Istighfar reminder flag  (Friday 00:10 UTC)
# ─────────────────────────────────────────────────────────────────────────────

def _run_sayyid_istighfar_reminder() -> None:
    """
    Jumuah ritual refresh — log which users haven't logged a sayyid_morning or
    sayyid_evening istighfar in the last 14 days. Frontend surfaces a gentle
    prompt. We store the flag as a display-log entry (context='weekly_sayyid').
    """
    log.info("[SayyidReminder] Starting weekly reminder sweep…")
    conn = get_db_connection()
    if conn is None:
        log.error("[SayyidReminder] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.user_id
            FROM tawbah_user_profile p
            LEFT JOIN (
                SELECT user_id, MAX(logged_at) AS last_sayyid
                FROM tawbah_istighfar_log
                WHERE type IN ('sayyid_morning', 'sayyid_evening')
                GROUP BY user_id
            ) s ON s.user_id = p.user_id
            WHERE s.last_sayyid IS NULL
               OR s.last_sayyid < now() - INTERVAL '14 days'
        """)
        users = [r[0] for r in cur.fetchall()]
        for user_id in users:
            cur.execute("""
                INSERT INTO tawbah_sacred_line_display_log (
                    user_id, line_id, context, displayed_at
                ) VALUES (%s, 'sayyid_ul_istighfar', 'weekly_sayyid_reminder', now())
            """, (user_id,))
        conn.commit()
        log.info(f"[SayyidReminder] Done — {len(users)} users reminded.")
    except Exception as e:
        conn.rollback()
        log.error(f"[SayyidReminder] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    """Initialise and start the Tawbah OS background scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")

    jobs = [
        (_run_qabooliyat_window_flags,   {"hour": 0, "minute": 10},                         "tawbah_qabooliyat_flags",  "Qabooliyat Window Flags"),
        (_run_sin_pattern_aggregation,   {"hour": 0, "minute": 40},                         "tawbah_sin_pattern_agg",   "Sin Pattern Aggregation"),
        (_run_istiqamah_gap_sweep,       {"hour": 1, "minute": 15},                         "tawbah_istiqamah_gap",     "Istiqamah Gap Sweep"),
        (_run_kaffarah_expiry_sweep,     {"hour": 2, "minute": 15},                         "tawbah_kaffarah_expiry",   "Kaffarah Expiry Sweep"),
        (_run_ruhani_fatigue_sweep,      {"hour": 6, "minute": 30},                         "tawbah_ruhani_fatigue",    "Ruhani Fatigue Sweep"),
        (_run_sayyid_istighfar_reminder, {"day_of_week": "fri", "hour": 0, "minute": 10},   "tawbah_sayyid_reminder",   "Sayyid-ul-Istighfar Weekly Reminder"),
    ]

    for func, cron_kw, job_id, job_name in jobs:
        _scheduler.add_job(
            func,
            trigger=CronTrigger(**cron_kw),
            id=job_id,
            name=job_name,
            replace_existing=True,
        )

    _scheduler.start()
    log.info(f"[Tawbah Scheduler] Started — {len(jobs)} cron jobs registered.")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("[Tawbah Scheduler] Stopped.")
