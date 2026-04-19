"""
Engine 2 — Tawbah Roadmap.

Core tawbah flow — enforces 3 conditions in order:
  1. Imsak     — stop the sin
  2. Nadim     — sincere remorse
  3. Azm       — resolve never to return

Plus huquq-ul-ibaad branch if rights owed to another person.
Displays Tawbah Nishaniyaan (6 signs) + mandatory qabooliyat disclaimer on completion.
"""
from database import get_db_connection, release_db_connection
from tawbah_os.encryption import encrypt
from tawbah_os.data_loader import TAWBAH_NISHANIYAAN, TIER3_MUFTI_CASES
from tawbah_os.middleware import strip_qabooliyat_claims

IMSAK, NADIM, AZM, HUQUQ = "imsak", "nadim", "azm", "huquq_ul_ibaad"
STEPS_CORE = (IMSAK, NADIM, AZM)


def detect_tier3_case(user_description: str) -> dict | None:
    """Return tier-3 case record if user's query matches a complex-fiqh trigger."""
    if not user_description:
        return None
    t = user_description.lower()
    cases = TIER3_MUFTI_CASES.get("cases", []) if isinstance(TIER3_MUFTI_CASES, dict) else []
    for case in cases:
        for trig in case.get("case_triggers", []):
            if trig.lower() in t:
                return case
    return None


def start_roadmap(user_id: str, session_id: int, gunah_description: str,
                  requires_huquq: bool = False) -> int:
    """Create a roadmap row. Returns roadmap_id."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_roadmap (
                    session_id, user_id, gunah_description_enc,
                    requires_huquq, current_step, status
                ) VALUES (%s, %s, %s, %s, %s, 'in_progress')
                RETURNING id
            """, (
                session_id, user_id,
                encrypt(gunah_description, user_id),
                requires_huquq, IMSAK,
            ))
            rid = c.fetchone()[0]
            conn.commit()
            return rid
    finally:
        release_db_connection(conn)


def complete_step(roadmap_id: int, step: str, reflection: str,
                  user_id: str) -> dict:
    """Mark a step complete; advance to next. Returns updated roadmap state."""
    if step not in STEPS_CORE + (HUQUQ,):
        raise ValueError(f"unknown step: {step}")
    next_step = {IMSAK: NADIM, NADIM: AZM, AZM: None}[step] if step in STEPS_CORE else None
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_roadmap_steps
                    (roadmap_id, user_id, step, reflection_enc)
                VALUES (%s, %s, %s, %s)
            """, (roadmap_id, user_id, step, encrypt(reflection, user_id)))
            if step in STEPS_CORE and next_step is not None:
                c.execute(
                    "UPDATE tawbah_roadmap SET current_step = %s WHERE id = %s",
                    (next_step, roadmap_id),
                )
            if step == AZM:
                c.execute("""
                    UPDATE tawbah_roadmap
                    SET status = 'completed', completed_at = now()
                    WHERE id = %s
                """, (roadmap_id,))
            conn.commit()
            c.execute("""
                SELECT current_step, status, requires_huquq
                FROM tawbah_roadmap WHERE id = %s
            """, (roadmap_id,))
            r = c.fetchone()
            return {
                "roadmap_id": roadmap_id,
                "current_step": r[0],
                "status": r[1],
                "requires_huquq": r[2],
                "next_step": next_step,
            }
    finally:
        release_db_connection(conn)


def get_nishaniyaan_payload(tone: str = "urdu_english_mix") -> dict:
    """Returns disclaimer + 6 nishaniyaan for UI display after Azm complete."""
    cfg = TAWBAH_NISHANIYAAN
    disc = cfg.get("mandatory_disclaimer", {})
    text_key = {
        "urdu_english_mix": "text_ur_en_mix",
        "urdu_formal": "text_urdu_formal",
        "english_formal": "text_english_formal",
        "hindi_english_mix": "text_hindi_english_mix",
        "arabic_emphasized": "text_arabic_emphasized",
    }.get(tone, "text_ur_en_mix")
    return {
        "disclaimer": disc.get(text_key) or disc.get("text_ur_en_mix"),
        "nishaniyaan": cfg.get("nishaniyaan", []),
        "cross_fiqh_note": cfg.get("cross_fiqh_consistency", {}).get("note"),
    }


def sanitize_ai_reply(text: str) -> str:
    """Guarantee no qabooliyat claim slips through."""
    cleaned, _ = strip_qabooliyat_claims(text)
    return cleaned
