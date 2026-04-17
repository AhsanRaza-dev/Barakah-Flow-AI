"""
scheduler.py — Fitrah OS Cron Jobs (v3-aligned)

14 background jobs aligned with JS v3 fitrah_cron_jobs.js.
Jobs are staggered to avoid DB contention.

Schedule (UTC — matches Islamic-time anchoring from v3):
  00:05  nightly_decay             — dimension score decay for inactive users
  00:20  qalb_state_pattern        — detect 3+ day qalb logging gaps
  00:30  spiritual_state_suggestor — compute candidate spiritual state for user confirmation
  01:00  nafs_promotion_check      — flag users eligible for nafs level promotion
  02:00  barakah_daily_calc        — compute daily barakah avg from sessions
  03:00  qadr_moment_sweep         — re-classify high-score actions as Qadr moments
  03:30  streak_expiry             — expire stale streaks (no activity in 2+ days)
  04:00  weekly_ihtisab            — Sunday only: generate weekly Ihtisab summaries
  05:00  sunnah_dna_rederivation   — Monday only: phase-based Sunnah DNA re-run
  05:30  purpose_drift             — Tuesday only: weekly purpose drift detection
  06:00  ruhani_fatigue            — detect 5+ day fatigue episodes
  06:00  monthly_report_flag       — 1st of month: flag users for monthly report generation
  Fri 00:05  dua_thread_reminder   — Jumuah: count pending duas 7+ days old
  Every 3 days 00:45  relationship_pulse — IHSAN action absence detection

All jobs use BATCH_SIZE = 500 (matching JS v3 processAllUsers utility).
"""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database import get_db_connection, release_db_connection
from fitrah_engine.scoring_logic import (
    apply_decay,
    calculate_crystal_score,
    get_nafs_level,
    determine_spiritual_state,
    NAFS_LEVELS,
    DIM_COLUMNS,
    VALID_DIMENSIONS,
)

log = logging.getLogger("fitrah.scheduler")

BATCH_SIZE = 500

_scheduler: BackgroundScheduler | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Helper: fetch all active users in batches
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_users(cur, extra_cols: str = "") -> list:
    """Fetch user_id + last_active_at + optional extra columns for all users."""
    cols = f"u.user_id, u.last_active_at{', ' + extra_cols if extra_cols else ''}"
    cur.execute(f"SELECT {cols} FROM fitrah_users u WHERE u.last_active_at IS NOT NULL")
    return cur.fetchall()


# ─────────────────────────────────────────────────────────────────────────────
# Job 1: Nightly Dimension Decay  (00:05 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_decay_job() -> None:
    """Apply per-dimension decay to users inactive long enough to start decaying."""
    log.info("[Decay] Starting…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Decay] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.user_id, u.last_active_at,
                   d.taqwa_score, d.ilm_score, d.tazkiya_score,
                   d.ihsan_score, d.nafs_score, d.maal_score
            FROM fitrah_users u
            JOIN fitrah_user_dimensions d ON d.user_id = u.user_id
            WHERE u.last_active_at IS NOT NULL
        """)
        rows = cur.fetchall()
        updated = 0
        for row in rows:
            user_id, last_active_at = row[0], row[1]
            dim_scores = {
                "taqwa": float(row[2] or 0), "ilm": float(row[3] or 0),
                "tazkiya": float(row[4] or 0), "ihsan": float(row[5] or 0),
                "nafs": float(row[6] or 0), "maal": float(row[7] or 0),
            }
            if last_active_at is None:
                continue
            if last_active_at.tzinfo is None:
                last_active_at = last_active_at.replace(tzinfo=timezone.utc)

            new_scores = apply_decay(dim_scores, last_active_at)
            if all(abs(new_scores.get(k, 0) - dim_scores.get(k, 0)) < 0.01 for k in dim_scores):
                continue

            new_crystal = calculate_crystal_score(new_scores)
            new_level   = get_nafs_level(new_crystal, new_scores["taqwa"])
            set_parts   = ", ".join(f"{DIM_COLUMNS[k]} = %s" for k in DIM_COLUMNS)
            values      = [round(new_scores[k], 2) for k in DIM_COLUMNS] + [user_id]
            cur.execute(f"UPDATE fitrah_user_dimensions SET {set_parts}, updated_at=now() WHERE user_id=%s", values)
            cur.execute(
                "UPDATE fitrah_users SET crystal_score=%s, current_nafs_level=%s WHERE user_id=%s",
                (new_crystal, new_level["level_key"], user_id),
            )
            updated += 1

        conn.commit()
        log.info(f"[Decay] Done — {updated}/{len(rows)} users updated.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Decay] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 2: Nafs Promotion Eligibility Check  (01:00 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

_NAFS_TIME_GATES_SCHED = {
    "nafs_e_lawwamah":   {"min_days": 30,  "taqwa_min": 0,  "no_dim_below": 0,  "tawbah_min": 0},
    "nafs_e_mulhama":    {"min_days": 60,  "taqwa_min": 30, "no_dim_below": 0,  "tawbah_min": 0},
    "nafs_e_mutmainnah": {"min_days": 90,  "taqwa_min": 60, "no_dim_below": 50, "tawbah_min": 0},
    "nafs_e_radhiya":    {"min_days": 180, "taqwa_min": 75, "no_dim_below": 65, "tawbah_min": 90},
    "nafs_e_mardhiyyah": {"min_days": 365, "taqwa_min": 85, "no_dim_below": 80, "tawbah_min": 0},
}


def _run_nafs_promotion_check() -> None:
    """
    Scan all users. For any user whose crystal has crossed into a higher level
    AND all time gates are met, set pending_nafs_level (if not already set).
    Frontend will then prompt the user to confirm.
    """
    log.info("[Nafs] Starting promotion eligibility check…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Nafs] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.user_id, u.current_nafs_level, u.pending_nafs_level,
                   u.crystal_score, u.nafs_level_since, u.tawbah_streak_current,
                   d.taqwa_score, d.ilm_score, d.tazkiya_score,
                   d.ihsan_score, d.nafs_score, d.maal_score
            FROM fitrah_users u
            JOIN fitrah_user_dimensions d ON d.user_id = u.user_id
        """)
        rows = cur.fetchall()
        flagged = 0
        today = datetime.now(timezone.utc).date()

        for row in rows:
            (user_id, cur_level, pending_level, crystal,
             level_since, tawbah_streak,
             taqwa, ilm, tazkiya, ihsan, nafs, maal) = row

            if pending_level:
                continue  # already has a pending promotion

            dim_scores = {
                "taqwa": float(taqwa or 0), "ilm": float(ilm or 0),
                "tazkiya": float(tazkiya or 0), "ihsan": float(ihsan or 0),
                "nafs": float(nafs or 0), "maal": float(maal or 0),
            }
            crystal_f = float(crystal or 0)
            cand_level = get_nafs_level(crystal_f, dim_scores["taqwa"])
            cur_level_obj = next((l for l in NAFS_LEVELS if l["level_key"] == cur_level), NAFS_LEVELS[0])

            if cand_level["level_order"] <= cur_level_obj["level_order"]:
                continue  # no promotion candidate

            # Check time gate
            days_at = (today - level_since).days if level_since else 0
            gate    = _NAFS_TIME_GATES_SCHED.get(cand_level["level_key"])
            if not gate:
                continue
            if days_at < gate["min_days"]:
                continue
            if dim_scores["taqwa"] < gate["taqwa_min"]:
                continue
            if gate["no_dim_below"] > 0:
                weak = [d for d, v in dim_scores.items() if v < gate["no_dim_below"]]
                if weak:
                    continue
            if gate["tawbah_min"] > 0 and int(tawbah_streak or 0) < gate["tawbah_min"]:
                continue

            # Gate passed — set pending promotion
            cur.execute(
                """UPDATE fitrah_users SET pending_nafs_level = %s WHERE user_id = %s
                   AND pending_nafs_level IS NULL""",
                (cand_level["level_key"], user_id),
            )
            flagged += 1

        conn.commit()
        log.info(f"[Nafs] Done — {flagged} users flagged for promotion.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Nafs] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 3: Spiritual State Suggestor  (00:30 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_spiritual_state_suggestor() -> None:
    """
    PDF §06 — runs nightly after decay & qalb pattern jobs.
    Computes a candidate spiritual state for each active user using
    determine_spiritual_state(), then writes it to spiritual_state_suggested.

    Critical: does NOT auto-assign the state — it only writes the suggestion.
    The user must confirm via POST /spiritual_state/confirm on next open.
    Only updates the suggestion if it differs from the current confirmed state
    (avoids unnecessary UI prompts for stable users).
    """
    log.info("[StatesSuggestor] Starting spiritual state suggestion…")
    conn = get_db_connection()
    if conn is None:
        log.error("[StateSuggestor] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.user_id, u.crystal_score, u.crystal_prev,
                   u.streak_current, u.tawbah_streak_current,
                   u.consecutive_ghafil_days, u.spiritual_state,
                   u.spiritual_state_suggested,
                   d.taqwa_score, d.ilm_score, d.tazkiya_score,
                   d.ihsan_score, d.nafs_score, d.maal_score
            FROM fitrah_users u
            JOIN fitrah_user_dimensions d ON d.user_id = u.user_id
            WHERE u.last_active_at >= now() - INTERVAL '7 days'
        """)
        rows = cur.fetchall()
        updated = 0

        for row in rows:
            (user_id, crystal, crystal_prev,
             streak, tawbah_streak,
             ghafil_days, current_state, current_suggestion,
             taqwa, ilm, tazkiya, ihsan, nafs, maal) = row

            dim_scores = {
                "taqwa":   float(taqwa   or 0),
                "ilm":     float(ilm     or 0),
                "tazkiya": float(tazkiya or 0),
                "ihsan":   float(ihsan   or 0),
                "nafs":    float(nafs    or 0),
                "maal":    float(maal    or 0),
            }

            # Check for recent penalty (any penalty action in last 7 days)
            cur.execute("""
                SELECT 1 FROM fitrah_user_action_logs
                WHERE user_id = %s
                  AND points_primary < 0
                  AND logged_at >= now() - INTERVAL '7 days'
                LIMIT 1
            """, (user_id,))
            recent_penalty = cur.fetchone() is not None

            candidate = determine_spiritual_state(
                crystal_score=float(crystal or 0),
                dim_scores=dim_scores,
                streak_current=int(streak or 0),
                tawbah_streak_current=int(tawbah_streak or 0),
                recent_penalty=recent_penalty,
                crystal_prev=float(crystal_prev or 0),
                consecutive_ghafil_days=int(ghafil_days or 0),
            )

            # Only write if the suggestion is new (different from last suggestion)
            if candidate != current_suggestion:
                cur.execute(
                    "UPDATE fitrah_users SET spiritual_state_suggested = %s WHERE user_id = %s",
                    (candidate, user_id),
                )
                updated += 1

        conn.commit()
        log.info(f"[StateSuggestor] Done — {updated}/{len(rows)} new suggestions written.")
    except Exception as e:
        conn.rollback()
        log.error(f"[StateSuggestor] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 4: Daily Barakah Score Calculation  (02:00 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_barakah_daily_calc() -> None:
    """
    For each user, compute:
      - barakah_score_today: average of today's completed sessions
      - barakah_score_weekly_avg: rolling 7-day average
    Writes back to fitrah_users.
    """
    log.info("[Barakah] Starting daily calc…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Barakah] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        # Today's averages
        cur.execute("""
            SELECT user_id,
                   AVG(barakah_score)::REAL AS today_avg,
                   AVG(barakah_score) FILTER (WHERE completed_at >= now() - INTERVAL '7 days')::REAL AS week_avg
            FROM fitrah_barakah_sessions
            WHERE completed_at IS NOT NULL
              AND completed_at >= now() - INTERVAL '7 days'
            GROUP BY user_id
        """)
        rows = cur.fetchall()
        for user_id, today_avg, week_avg in rows:
            cur.execute(
                """UPDATE fitrah_users
                   SET barakah_score_today      = %s,
                       barakah_score_weekly_avg = %s
                   WHERE user_id = %s""",
                (round(today_avg or 0, 1), round(week_avg or 0, 1), user_id),
            )
        conn.commit()
        log.info(f"[Barakah] Done — {len(rows)} users updated.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Barakah] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 4: Streak Expiry  (03:30 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_streak_expiry() -> None:
    """
    Reset streak_current to 0 for users with no activity in the last 2 days.
    tawbah_streak_current is preserved (it resets on penalty, not inactivity).
    """
    log.info("[Streak] Starting expiry check…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Streak] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE fitrah_users
            SET streak_current = 0
            WHERE last_active_at < now() - INTERVAL '2 days'
              AND streak_current > 0
        """)
        expired = cur.rowcount
        conn.commit()
        log.info(f"[Streak] Done — {expired} streaks expired.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Streak] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 5: Ruhani Fatigue Detection  (06:00 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_ruhani_fatigue_detection() -> None:
    """
    Detect Ruhani Fatigue: TAQWA < 40 AND TAZKIYA < 40 for 5+ consecutive days.
    Sets ruhani_fatigue_active=TRUE and logs to fitrah_ruhani_fatigue_log.
    Resolves fatigue when TAQWA >= 40 OR TAZKIYA >= 40.
    """
    log.info("[Fatigue] Starting detection…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Fatigue] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()

        # Find users with both TAQWA and TAZKIYA < 40
        cur.execute("""
            SELECT u.user_id, u.ruhani_fatigue_active, d.taqwa_score, d.tazkiya_score
            FROM fitrah_users u
            JOIN fitrah_user_dimensions d ON d.user_id = u.user_id
        """)
        rows = cur.fetchall()
        flagged = resolved = 0

        for user_id, fatigue_active, taqwa, tazkiya in rows:
            taqwa_f   = float(taqwa or 0)
            tazkiya_f = float(tazkiya or 0)
            both_low  = taqwa_f < 40 and tazkiya_f < 40

            if both_low and not fatigue_active:
                # Check how many consecutive days TAQWA was < 40 in action logs
                cur.execute("""
                    SELECT COUNT(DISTINCT logged_at::DATE)
                    FROM fitrah_user_action_logs
                    WHERE user_id = %s
                      AND logged_at >= now() - INTERVAL '7 days'
                """, (user_id,))
                days_active = cur.fetchone()[0] or 0
                # If they've been active for 5+ days but still both low → fatigue
                if days_active >= 5:
                    cur.execute(
                        "UPDATE fitrah_users SET ruhani_fatigue_active = TRUE WHERE user_id = %s",
                        (user_id,),
                    )
                    cur.execute(
                        """INSERT INTO fitrah_ruhani_fatigue_log
                           (user_id, taqwa_avg, tazkiya_avg)
                           VALUES (%s, %s, %s)
                           ON CONFLICT DO NOTHING""",
                        (user_id, round(taqwa_f, 1), round(tazkiya_f, 1)),
                    )
                    flagged += 1

            elif not both_low and fatigue_active:
                # Recover: at least one dimension back above 40
                cur.execute(
                    "UPDATE fitrah_users SET ruhani_fatigue_active = FALSE WHERE user_id = %s",
                    (user_id,),
                )
                cur.execute(
                    """UPDATE fitrah_ruhani_fatigue_log
                       SET ended_at = now(), resolved = TRUE
                       WHERE user_id = %s AND resolved = FALSE""",
                    (user_id,),
                )
                resolved += 1

        conn.commit()
        log.info(f"[Fatigue] Done — {flagged} new, {resolved} resolved.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Fatigue] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 6: Weekly Ihtisab Generation  (Sunday 04:00 UTC)
# ─────────────────────────────────────────────────────────────────────────────

def _run_weekly_ihtisab() -> None:
    """
    Generate weekly Ihtisab records for all active users.
    Computes action counts per category, crystal delta, qalb mode, barakah avg.
    Inserts into fitrah_weekly_ihtisab (UNIQUE on user_id + week_ending_date).
    AI narrative generation is deferred — frontend calls /ihtisab/weekly which
    generates the narrative on-demand using Claude.
    """
    log.info("[Ihtisab] Starting weekly generation…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Ihtisab] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        today      = datetime.now(timezone.utc).date()
        week_end   = today
        week_start = today - timedelta(days=7)
        week_num   = today.isocalendar()[1]

        # Users active in the last 7 days
        cur.execute(
            "SELECT user_id, crystal_score FROM fitrah_users WHERE last_active_at >= %s",
            (week_start,),
        )
        active_users = cur.fetchall()
        generated = 0

        for user_id, crystal_end in active_users:
            crystal_end = float(crystal_end or 0)

            # Action counts per category this week
            cur.execute("""
                SELECT a.data->>'category' AS category, COUNT(*)
                FROM fitrah_user_action_logs l
                JOIN fitrah_master_actions a ON a.action_key = l.action_key
                WHERE l.user_id = %s
                  AND l.logged_at::DATE BETWEEN %s AND %s
                GROUP BY category
            """, (user_id, week_start, week_end))
            cat_counts: dict[str, int] = {}
            total = 0
            for cat, cnt in cur.fetchall():
                cat_counts[cat or "other"] = int(cnt)
                total += int(cnt)

            ibadat  = sum(cat_counts.get(c, 0) for c in ["ibadat"])
            ilm_cnt = cat_counts.get("ilm", 0)
            akhlaq  = cat_counts.get("akhlaq_tazkiya", 0)
            khidmat = cat_counts.get("khidmat_ihsan", 0)
            nafs_cnt = sum(cat_counts.get(c, 0) for c in ["nafs_body", "maal", "state_reflection", "dua_thread", "penalty"])

            # Crystal start (7 days ago snapshot — approximate from action log)
            crystal_start = crystal_end  # default if no week-ago snapshot

            # Qalb mode this week
            cur.execute("""
                SELECT qalb_state, COUNT(*) FROM fitrah_qalb_state_history
                WHERE user_id = %s AND logged_date BETWEEN %s AND %s
                GROUP BY qalb_state ORDER BY COUNT(*) DESC LIMIT 1
            """, (user_id, week_start, week_end))
            qalb_row = cur.fetchone()
            qalb_mode = qalb_row[0] if qalb_row else None

            # Barakah avg this week
            cur.execute("""
                SELECT AVG(barakah_score)::REAL, MAX(completed_at::DATE)
                FROM fitrah_barakah_sessions
                WHERE user_id = %s AND completed_at::DATE BETWEEN %s AND %s
                  AND completed_at IS NOT NULL
            """, (user_id, week_start, week_end))
            bar_row = cur.fetchone()
            avg_barakah = round(float(bar_row[0] or 0), 1) if bar_row else 0.0

            cur.execute(
                """INSERT INTO fitrah_weekly_ihtisab
                   (user_id, week_ending_date, week_number,
                    total_actions_count, ibadat_actions_count, ilm_actions_count,
                    akhlaq_actions_count, khidmat_actions_count, nafs_actions_count,
                    crystal_start, crystal_end, crystal_change,
                    avg_barakah_score, qalb_state_mode, user_reviewed)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                   ON CONFLICT (user_id, week_ending_date) DO UPDATE
                   SET total_actions_count   = EXCLUDED.total_actions_count,
                       ibadat_actions_count  = EXCLUDED.ibadat_actions_count,
                       ilm_actions_count     = EXCLUDED.ilm_actions_count,
                       akhlaq_actions_count  = EXCLUDED.akhlaq_actions_count,
                       khidmat_actions_count = EXCLUDED.khidmat_actions_count,
                       nafs_actions_count    = EXCLUDED.nafs_actions_count,
                       crystal_end          = EXCLUDED.crystal_end,
                       crystal_change       = EXCLUDED.crystal_change,
                       avg_barakah_score    = EXCLUDED.avg_barakah_score,
                       qalb_state_mode      = EXCLUDED.qalb_state_mode,
                       generated_at         = now()""",
                (user_id, week_end, week_num,
                 total, ibadat, ilm_cnt, akhlaq, khidmat, nafs_cnt,
                 crystal_start, crystal_end, round(crystal_end - crystal_start, 2),
                 avg_barakah, qalb_mode),
            )
            generated += 1

        conn.commit()
        log.info(f"[Ihtisab] Done — {generated} records generated for week {week_num}.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Ihtisab] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 7: Sunnah DNA Re-derivation  (Monday 05:00 UTC)
# ─────────────────────────────────────────────────────────────────────────────

def _run_sunnah_dna_rederivation() -> None:
    """
    Phase-based Sunnah DNA re-derivation (JS v3 deriveSunnahDNA phases):
      Phase 1 (default): profiler-only scores — set at onboarding, no change here
      Phase 2 (day 14+): blended — profiler 50% + action log 50%
      Phase 3 (day 60+): mature — action log 80% + profiler 20%
    Updates fitrah_users.sunnah_dna_phase when milestone reached.
    """
    log.info("[SunnahDNA] Starting re-derivation…")
    conn = get_db_connection()
    if conn is None:
        log.error("[SunnahDNA] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, profiler_completed_at, sunnah_dna_phase FROM fitrah_users WHERE profiler_completed_at IS NOT NULL"
        )
        rows = cur.fetchall()
        updated = 0
        today = datetime.now(timezone.utc).date()

        for user_id, profiler_at, current_phase in rows:
            if profiler_at is None:
                continue
            if profiler_at.tzinfo is None:
                profiler_at = profiler_at.replace(tzinfo=timezone.utc)

            days_since = (today - profiler_at.date()).days
            new_phase  = 1
            if days_since >= 60:
                new_phase = 3
            elif days_since >= 14:
                new_phase = 2

            if new_phase <= int(current_phase or 1):
                continue  # no phase change needed

            # Log phase transition
            cur.execute(
                """INSERT INTO fitrah_sunnah_dna_history
                   (user_id, phase)
                   VALUES (%s, %s)""",
                (user_id, new_phase),
            )
            cur.execute(
                "UPDATE fitrah_users SET sunnah_dna_phase = %s WHERE user_id = %s",
                (new_phase, user_id),
            )
            updated += 1

        conn.commit()
        log.info(f"[SunnahDNA] Done — {updated} users advanced to new phase.")
    except Exception as e:
        conn.rollback()
        log.error(f"[SunnahDNA] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 8: Purpose Drift Detection  (Tuesday 05:30 UTC)
# ─────────────────────────────────────────────────────────────────────────────

# Expected dimension point distribution per ummah_role (% of total points)
_ROLE_EXPECTED: dict[str, dict] = {
    "ahl_ilm":        {"taqwa": 20, "ilm": 35, "tazkiya": 15, "ihsan": 10, "nafs": 10, "maal": 10},
    "ahl_khidmat":    {"taqwa": 20, "ilm": 10, "tazkiya": 15, "ihsan": 30, "nafs": 15, "maal": 10},
    "ahl_maal":       {"taqwa": 20, "ilm": 10, "tazkiya": 15, "ihsan": 15, "nafs": 10, "maal": 30},
    "ahl_dawah":      {"taqwa": 25, "ilm": 20, "tazkiya": 20, "ihsan": 15, "nafs": 10, "maal": 10},
    "ahl_tarbiyah":   {"taqwa": 25, "ilm": 25, "tazkiya": 20, "ihsan": 15, "nafs": 10, "maal": 5},
    "wasatiyya":      {"taqwa": 20, "ilm": 15, "tazkiya": 20, "ihsan": 20, "nafs": 15, "maal": 10},
}
_DRIFT_THRESHOLD = 30  # delta > 30% triggers drift flag


def _run_purpose_drift_detection() -> None:
    """
    For each user, compare last 2 weeks of dimension point distribution
    against expected for their ummah_role.
    If any dimension delta > 30% for 2+ consecutive weeks, sets purpose_drift_weeks += 1.
    """
    log.info("[Drift] Starting purpose drift detection…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Drift] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        # PDF §10 — respect user's conscious_choice drift pause (drift_pause_until)
        cur.execute(
            """SELECT user_id, ummah_role FROM fitrah_users
               WHERE ummah_role IS NOT NULL
                 AND (drift_pause_until IS NULL OR drift_pause_until < CURRENT_DATE)"""
        )
        users = cur.fetchall()
        today      = datetime.now(timezone.utc).date()
        week_start = today - timedelta(days=14)
        detected = 0

        for user_id, ummah_role in users:
            expected = _ROLE_EXPECTED.get(ummah_role, _ROLE_EXPECTED["wasatiyya"])

            # Actual distribution over last 2 weeks
            cur.execute("""
                SELECT dimension_primary, SUM(points_primary) as pts
                FROM fitrah_user_action_logs
                WHERE user_id = %s
                  AND logged_at::DATE >= %s
                  AND points_primary > 0
                GROUP BY dimension_primary
            """, (user_id, week_start))
            dim_pts = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
            total_pts = sum(dim_pts.values()) or 1

            actual_pct: dict[str, float] = {
                d: round((dim_pts.get(d, 0) / total_pts) * 100, 1)
                for d in VALID_DIMENSIONS
            }

            drift_dims = [
                d for d in VALID_DIMENSIONS
                if abs(actual_pct.get(d, 0) - expected.get(d, 0)) > _DRIFT_THRESHOLD
            ]
            drift_detected = len(drift_dims) > 0

            cur.execute(
                """INSERT INTO fitrah_purpose_drift_log
                   (user_id, week_ending_date, ummah_role,
                    actual_distribution, expected_distribution,
                    drift_detected, drift_dimensions)
                   VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                   ON CONFLICT (user_id, week_ending_date) DO UPDATE
                   SET drift_detected    = EXCLUDED.drift_detected,
                       drift_dimensions  = EXCLUDED.drift_dimensions,
                       actual_distribution = EXCLUDED.actual_distribution""",
                (user_id, today, ummah_role,
                 str(actual_pct).replace("'", '"'),
                 str(expected).replace("'", '"'),
                 drift_detected, drift_dims),
            )

            if drift_detected:
                cur.execute(
                    "UPDATE fitrah_users SET purpose_drift_days = purpose_drift_days + 7, last_drift_check = %s WHERE user_id = %s",
                    (today, user_id),
                )
                detected += 1
            else:
                cur.execute(
                    "UPDATE fitrah_users SET purpose_drift_days = 0, last_drift_check = %s WHERE user_id = %s",
                    (today, user_id),
                )

        conn.commit()
        log.info(f"[Drift] Done — {detected}/{len(users)} users showing drift.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Drift] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 9: Qadr Moment Sweep  (Wednesday 03:00 UTC)
# ─────────────────────────────────────────────────────────────────────────────

def _run_qadr_moment_sweep() -> None:
    """
    Re-classify high-score actions logged in the last 7 days as potential Qadr moments.
    Flags actions where points_primary >= 10 as `qadr_candidate = TRUE` in a system config.
    Actual AI classification happens on-demand via /maqsad/qadr endpoint.
    This job just creates a weekly report entry summarising candidates.
    """
    log.info("[Qadr] Starting moment sweep…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Qadr] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, COUNT(*) as high_score_count
            FROM fitrah_user_action_logs
            WHERE points_primary >= 10
              AND logged_at >= now() - INTERVAL '7 days'
            GROUP BY user_id
            HAVING COUNT(*) >= 3
        """)
        rows = cur.fetchall()
        # Update a cached field on the user for the frontend to surface Qadr prompts
        for user_id, count in rows:
            cur.execute(
                """UPDATE fitrah_users
                   SET spiritual_state_suggested = 'present_with_allah'
                   WHERE user_id = %s
                     AND crystal_score >= 70
                     AND (spiritual_state_suggested IS NULL OR spiritual_state_suggested != 'present_with_allah')""",
                (user_id,),
            )
        conn.commit()
        log.info(f"[Qadr] Done — {len(rows)} users with high-score weeks.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Qadr] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 10: Monthly Report Flag  (1st of each month, 06:00 UTC)
# ─────────────────────────────────────────────────────────────────────────────

def _run_monthly_report_flag() -> None:
    """
    On the 1st of each month, mark all active users as needing a monthly report.
    The actual report is generated on-demand via POST /maqsad/report when the
    user opens the monthly summary screen.
    This job resets the `user_reviewed` flag on last month's ihtisab entries.
    """
    log.info("[Monthly] Starting monthly report flag…")
    conn = get_db_connection()
    if conn is None:
        log.error("[Monthly] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        # Mark prior month's ihtisab as needing re-review
        last_month_start = (datetime.now(timezone.utc).date().replace(day=1) - timedelta(days=1)).replace(day=1)
        cur.execute(
            """UPDATE fitrah_weekly_ihtisab
               SET user_reviewed = FALSE
               WHERE week_ending_date >= %s
                 AND week_ending_date < CURRENT_DATE""",
            (last_month_start,),
        )
        updated = cur.rowcount
        conn.commit()
        log.info(f"[Monthly] Done — {updated} ihtisab records flagged for review.")
    except Exception as e:
        conn.rollback()
        log.error(f"[Monthly] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 11: Qalb State Pattern Detection  (00:20 UTC daily)
# ─────────────────────────────────────────────────────────────────────────────

def _run_qalb_state_pattern() -> None:
    """
    Detect users who haven't logged a Qalb state in 3+ days.

    When a 3-day gap is found:
    - Sets qalb_gap_flagged = TRUE so the AI context layer knows to gently
      prompt the user about their heart state on next interaction.
    - Increments consecutive_ghafil_days (no self-reflection for 3+ days
      is itself a ghafil signal per §07).

    Clears qalb_gap_flagged for users who logged today.
    """
    log.info("[QalbPattern] Starting Qalb State Pattern detection…")
    conn = get_db_connection()
    if conn is None:
        log.error("[QalbPattern] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()

        # Clear flag for users who logged today (reset)
        cur.execute(
            """UPDATE fitrah_users
               SET qalb_gap_flagged = FALSE
               WHERE last_qalb_state_logged = CURRENT_DATE
                 AND qalb_gap_flagged = TRUE"""
        )
        cleared = cur.rowcount

        # Flag users with 3+ day gap (NULL counts as never logged = gap)
        cur.execute(
            """UPDATE fitrah_users
               SET qalb_gap_flagged       = TRUE,
                   consecutive_ghafil_days = LEAST(consecutive_ghafil_days + 1, 30)
               WHERE (
                   last_qalb_state_logged IS NULL
                   OR last_qalb_state_logged <= CURRENT_DATE - INTERVAL '3 days'
               )
               AND last_active_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'"""
        )
        flagged = cur.rowcount

        conn.commit()
        log.info(
            f"[QalbPattern] Done — {flagged} users flagged (3+ day gap), "
            f"{cleared} flags cleared (logged today)."
        )
    except Exception as e:
        conn.rollback()
        log.error(f"[QalbPattern] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 12: Dua Thread Reminder  (Friday 00:05 UTC — Jumuah)
# ─────────────────────────────────────────────────────────────────────────────

def _run_dua_thread_reminder() -> None:
    """
    Every Jumuah (Friday), count each user's pending duas older than 7 days
    and update dua_reminder_count on fitrah_users.

    The app layer reads dua_reminder_count > 0 to show a gentle reminder
    on next open — "Aapki X dua(ein) pending hain — kya koi update hai?"
    Never aggressive — one reminder per week maximum.

    Only targets active users (last_active_at within 30 days).
    """
    log.info("[DuaReminder] Starting Dua Thread Reminder sweep…")
    conn = get_db_connection()
    if conn is None:
        log.error("[DuaReminder] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()

        # Reset all counts first
        cur.execute("UPDATE fitrah_users SET dua_reminder_count = 0")

        # Update with fresh counts of pending duas >= 7 days old
        cur.execute(
            """UPDATE fitrah_users u
               SET dua_reminder_count = sub.pending_count
               FROM (
                   SELECT user_id, COUNT(*) AS pending_count
                   FROM fitrah_dua_thread
                   WHERE status = 'pending'
                     AND created_at <= CURRENT_TIMESTAMP - INTERVAL '7 days'
                     AND deleted_at IS NULL
                   GROUP BY user_id
               ) sub
               WHERE u.user_id = sub.user_id
                 AND u.last_active_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'"""
        )
        updated = cur.rowcount

        conn.commit()
        log.info(f"[DuaReminder] Done — {updated} users have pending dua reminders set.")
    except Exception as e:
        conn.rollback()
        log.error(f"[DuaReminder] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Job 14: Relationship Pulse Update  (every 3 days, 00:45 UTC)
# ─────────────────────────────────────────────────────────────────────────────

def _run_relationship_pulse_update() -> None:
    """
    PDF §20 — runs every 3 days.
    Detects IHSAN neglect: users who haven't logged any IHSAN-related action
    in the last 3 days. Increments relationship_neglect_days for each missed
    3-day window; resets to 0 when an IHSAN action is found.

    The AI layer reads relationship_neglect_days > 0 to softly surface
    a haquq (rights) reminder on the next Akhlaq AI interaction.
    Only targets active users (last_active_at within 14 days).
    """
    log.info("[RelPulse] Starting Relationship Pulse Update…")
    conn = get_db_connection()
    if conn is None:
        log.error("[RelPulse] No DB connection — skipping.")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id
            FROM fitrah_users
            WHERE last_active_at >= now() - INTERVAL '14 days'
        """)
        users = [r[0] for r in cur.fetchall()]
        neglected = reset_count = 0

        for user_id in users:
            # Check for any IHSAN action in last 3 days
            cur.execute("""
                SELECT 1 FROM fitrah_user_action_logs
                WHERE user_id = %s
                  AND dimension_primary = 'ihsan'
                  AND logged_at >= now() - INTERVAL '3 days'
                LIMIT 1
            """, (user_id,))
            has_ihsan = cur.fetchone() is not None

            if has_ihsan:
                cur.execute(
                    "UPDATE fitrah_users SET relationship_neglect_days = 0 WHERE user_id = %s AND relationship_neglect_days > 0",
                    (user_id,),
                )
                if cur.rowcount:
                    reset_count += 1
            else:
                cur.execute(
                    """UPDATE fitrah_users
                       SET relationship_neglect_days = LEAST(relationship_neglect_days + 3, 90)
                       WHERE user_id = %s""",
                    (user_id,),
                )
                neglected += 1

        conn.commit()
        log.info(f"[RelPulse] Done — {neglected} users flagged, {reset_count} reset.")
    except Exception as e:
        conn.rollback()
        log.error(f"[RelPulse] Error: {e}")
    finally:
        release_db_connection(conn)


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    """Initialise and start the APScheduler with all 12 Fitrah OS cron jobs."""
    global _scheduler
    if _scheduler and _scheduler.running:
        return  # Already started (e.g. uvicorn reload)

    _scheduler = BackgroundScheduler(timezone="UTC")

    jobs = [
        # (func,                             cron kwargs,                                      id,                            name)
        (_run_decay_job,                     {"hour": 0,  "minute": 5},                        "fitrah_nightly_decay",         "Nightly Dimension Decay"),
        (_run_qalb_state_pattern,            {"hour": 0,  "minute": 20},                       "fitrah_qalb_pattern",          "Qalb State Pattern Detection"),
        (_run_spiritual_state_suggestor,     {"hour": 0,  "minute": 30},                       "fitrah_state_suggestor",       "Spiritual State Suggestor"),
        (_run_nafs_promotion_check,          {"hour": 1,  "minute": 0},                        "fitrah_nafs_promo_check",      "Nafs Promotion Eligibility Check"),
        (_run_barakah_daily_calc,            {"hour": 2,  "minute": 0},                        "fitrah_barakah_daily",         "Daily Barakah Score Calculation"),
        (_run_streak_expiry,                 {"hour": 3,  "minute": 30},                       "fitrah_streak_expiry",         "Daily Streak Expiry"),
        (_run_ruhani_fatigue_detection,      {"hour": 6,  "minute": 0},                        "fitrah_ruhani_fatigue",        "Ruhani Fatigue Detection"),
        (_run_weekly_ihtisab,                {"day_of_week": "sun", "hour": 4},                "fitrah_weekly_ihtisab",        "Weekly Ihtisab Generation"),
        (_run_sunnah_dna_rederivation,       {"day_of_week": "mon", "hour": 5},                "fitrah_sunnah_dna",            "Sunnah DNA Re-derivation"),
        (_run_purpose_drift_detection,       {"day_of_week": "tue", "hour": 5, "minute": 30},  "fitrah_purpose_drift",         "Purpose Drift Detection"),
        (_run_qadr_moment_sweep,             {"day_of_week": "wed", "hour": 3},                "fitrah_qadr_sweep",            "Qadr Moment Sweep"),
        (_run_monthly_report_flag,           {"day": 1,   "hour": 6},                          "fitrah_monthly_report",        "Monthly Report Flag"),
        (_run_dua_thread_reminder,           {"day_of_week": "fri", "hour": 0, "minute": 5},   "fitrah_dua_reminder",          "Dua Thread Reminder (Jumuah)"),
        (_run_relationship_pulse_update,     {"day": "*/3", "hour": 0, "minute": 45},          "fitrah_relationship_pulse",    "Relationship Pulse Update"),
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
    log.info(f"[Fitrah Scheduler] Started — {len(jobs)} cron jobs registered.")  # 14 total


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("[Fitrah Scheduler] Stopped.")
