"""
Engine 6 — Kaffarat-ul-Dhunub Engine.

10 authentic ways gunah erased (tracked): istighfar, sadaqah, hasanat,
musibat sabr, dua for others, hajj/umrah intentions.
Qualitative visualization only — no numeric "gunah erased" progress.
"""
from database import get_db_connection, release_db_connection
from tawbah_os.encryption import encrypt
from tawbah_os.data_loader import KAFFARAH_CONFIG


def activate(user_id: str, session_id: int, duration_days: int,
             target_gunah: str = None) -> int:
    if duration_days not in (30, 60, 90):
        raise ValueError("duration_days must be 30, 60, or 90")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_kaffarah_activation (
                    session_id, user_id, activated_at, duration_days,
                    target_gunah_optional_enc, status
                ) VALUES (%s, %s, now(), %s, %s, 'active')
                RETURNING id
            """, (
                session_id, user_id, duration_days,
                encrypt(target_gunah, user_id) if target_gunah else None,
            ))
            kid = c.fetchone()[0]
            conn.commit()
            return kid
    finally:
        release_db_connection(conn)


def log_istighfar(user_id: str, count: int, type_: str = "basic") -> int:
    if type_ not in ("basic", "sayyid_morning", "sayyid_evening"):
        raise ValueError("invalid istighfar type")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_istighfar_log (
                    user_id, count, type, logged_date, logged_at
                ) VALUES (%s, %s, %s, CURRENT_DATE, now())
                RETURNING id
            """, (user_id, count, type_))
            lid = c.fetchone()[0]
            conn.commit()
            return lid
    finally:
        release_db_connection(conn)


def log_sadaqah(user_id: str, amount: str, currency: str, recipient_type: str,
                niyyah: str, linked_gunah: str = None,
                is_jariyah: bool = False) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_sadaqah_kaffarah_log (
                    user_id, amount_enc, currency, recipient_type,
                    niyyah_enc, linked_gunah_enc, is_jariyah, logged_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                RETURNING id
            """, (
                user_id,
                encrypt(amount, user_id),
                currency,
                recipient_type,
                encrypt(niyyah, user_id),
                encrypt(linked_gunah, user_id) if linked_gunah else None,
                is_jariyah,
            ))
            sid = c.fetchone()[0]
            conn.commit()
            return sid
    finally:
        release_db_connection(conn)


def log_hasanah(user_id: str, category: str, description: str, niyyah: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_hasanat_log (
                    user_id, category, description_enc, niyyah_enc, logged_at
                ) VALUES (%s, %s, %s, %s, now())
                RETURNING id
            """, (
                user_id, category,
                encrypt(description, user_id),
                encrypt(niyyah, user_id),
            ))
            hid = c.fetchone()[0]
            conn.commit()
            return hid
    finally:
        release_db_connection(conn)


def log_musibat_sabr(user_id: str, category: str, sensitivity: str,
                     reflection: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_musibat_sabr_log (
                    user_id, category, sensitivity_level, reflection_enc, logged_at
                ) VALUES (%s, %s, %s, %s, now())
                RETURNING id
            """, (
                user_id, category, sensitivity,
                encrypt(reflection, user_id),
            ))
            mid = c.fetchone()[0]
            conn.commit()
            return mid
    finally:
        release_db_connection(conn)


def log_dua_for_others(user_id: str, mode: str, target: str = None) -> int:
    if mode not in ("specific_person", "general_ummah", "specific_group"):
        raise ValueError("invalid dua_for_others mode")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_dua_for_others_log (
                    user_id, mode, target_encrypted_optional, logged_date, logged_at
                ) VALUES (%s, %s, %s, CURRENT_DATE, now())
                RETURNING id
            """, (
                user_id, mode,
                encrypt(target, user_id) if target else None,
            ))
            did = c.fetchone()[0]
            conn.commit()
            return did
    finally:
        release_db_connection(conn)


def log_hajj_umrah_intention(user_id: str, type_: str, year_target: int,
                             niyyah: str, reflection: str = None) -> int:
    valid = ("hajj_planned", "hajj_completed", "umrah_planned", "umrah_completed")
    if type_ not in valid:
        raise ValueError(f"type must be one of {valid}")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_hajj_umrah_intentions (
                    user_id, type, status, year_target,
                    niyyah_enc, reflection_enc, logged_at
                ) VALUES (%s, %s, 'logged', %s, %s, %s, now())
                RETURNING id
            """, (
                user_id, type_, year_target,
                encrypt(niyyah, user_id),
                encrypt(reflection, user_id) if reflection else None,
            ))
            hid = c.fetchone()[0]
            conn.commit()
            return hid
    finally:
        release_db_connection(conn)


def weekly_summary(user_id: str) -> dict:
    """Qualitative weekly summary — no numeric 'gunah erased' claim."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT
                  (SELECT COUNT(*) FROM tawbah_istighfar_log
                   WHERE user_id=%s AND logged_at > now() - INTERVAL '7 days'),
                  (SELECT COUNT(*) FROM tawbah_sadaqah_kaffarah_log
                   WHERE user_id=%s AND logged_at > now() - INTERVAL '7 days'),
                  (SELECT COUNT(*) FROM tawbah_hasanat_log
                   WHERE user_id=%s AND logged_at > now() - INTERVAL '7 days'),
                  (SELECT COUNT(*) FROM tawbah_dua_for_others_log
                   WHERE user_id=%s AND logged_at > now() - INTERVAL '7 days'),
                  (SELECT COUNT(*) FROM tawbah_musibat_sabr_log
                   WHERE user_id=%s AND logged_at > now() - INTERVAL '7 days')
            """, (user_id,) * 5)
            r = c.fetchone()
            return {
                "istighfar_sessions": r[0],
                "sadaqah_entries": r[1],
                "good_deeds": r[2],
                "duas_for_others": r[3],
                "musibat_acknowledgments": r[4],
                "disclaimer": (
                    "Yeh amaal Allah ke paas jama ho rahe hain — qabool karna "
                    "aur gunah mitana sirf Allah ka kaam hai."
                ),
            }
    finally:
        release_db_connection(conn)


def get_config() -> dict:
    return KAFFARAH_CONFIG if isinstance(KAFFARAH_CONFIG, dict) else {}
