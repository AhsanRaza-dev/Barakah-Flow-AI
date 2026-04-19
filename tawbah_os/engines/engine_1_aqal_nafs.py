"""
Engine 1 — Aqal vs Nafs Negotiation System.

User describes internal struggle; engine externalizes both voices
(nafs khwaish + aqal/fitrah hidayat) so user can see and reinforce aqal.
"""
from database import get_db_connection, release_db_connection
from tawbah_os.encryption import encrypt
from tawbah_os.data_loader import AQAL_NAFS_NEGOTIATION


def log_negotiation(user_id: str, session_id: int, urge_text: str,
                    nafs_voice: str, aqal_voice: str, resolution: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_aqal_nafs_logs (
                    session_id, user_id, urge_text_enc,
                    nafs_voice_enc, aqal_voice_enc, resolution, logged_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now())
                RETURNING id
            """, (
                session_id, user_id,
                encrypt(urge_text, user_id),
                encrypt(nafs_voice, user_id),
                encrypt(aqal_voice, user_id),
                resolution,
            ))
            nid = c.fetchone()[0]
            conn.commit()
            return nid
    finally:
        release_db_connection(conn)


def get_config() -> dict:
    return AQAL_NAFS_NEGOTIATION if isinstance(AQAL_NAFS_NEGOTIATION, dict) else {}
