"""
Engine 5 — Spiritual Resurrection (Tahajjud Deep Tawbah Mode).

NOTE: Dua Therapy DB component is on hold (dua_therapy_db.json pending).
This engine ships with: 5-step guided Tahajjud session + Sayyid-ul-Istighfar
+ sacred-lines rotation hook + Private Ibadah Chamber handoff.
"""
from database import get_db_connection, release_db_connection
from tawbah_os.encryption import encrypt
from tawbah_os.data_loader import SACRED_LINES, DUA_THERAPY_DB

TAHAJJUD_STEPS = (
    ("step_1", "2 rakat nafil"),
    ("step_2", "Sajdah — lambi dua"),
    ("step_3", "Muhasaba (brief self-accounting)"),
    ("step_4", "Sayyid-ul-Istighfar recitation"),
    ("step_5", "Personal dua in own words"),
)

SAYYID_UL_ISTIGHFAR = {
    "arabic": (
        "اللَّهُمَّ أَنْتَ رَبِّي لاَ إِلَهَ إِلاَّ أَنْتَ، خَلَقْتَنِي وَأَنَا عَبْدُكَ، "
        "وَأَنَا عَلَى عَهْدِكَ وَوَعْدِكَ مَا اسْتَطَعْتُ، أَعُوذُ بِكَ مِنْ شَرِّ مَا صَنَعْتُ، "
        "أَبُوءُ لَكَ بِنِعْمَتِكَ عَلَيَّ، وَأَبُوءُ لَكَ بِذَنْبِي، فَاغْفِرْ لِي "
        "فَإِنَّهُ لاَ يَغْفِرُ الذُّنُوبَ إِلاَّ أَنْتَ"
    ),
    "source": "Sahih Bukhari 6306",
    "promise": (
        "Jo subah yaqeen se padhe aur shaam ko mar jaye — Jannati. "
        "Aur jo shaam padhe aur subah mar jaye — Jannati."
    ),
}


def start_tahajjud_session(user_id: str, session_id: int) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_tahajjud_sessions (
                    session_id, user_id, current_step, started_at
                ) VALUES (%s, %s, 'step_1', now())
                RETURNING id
            """, (session_id, user_id))
            tid = c.fetchone()[0]
            conn.commit()
            return tid
    finally:
        release_db_connection(conn)


def complete_tahajjud_step(tahajjud_id: int, step: str, reflection: str,
                           user_id: str) -> dict:
    valid = {s[0] for s in TAHAJJUD_STEPS}
    if step not in valid:
        raise ValueError(f"unknown step: {step}")
    order = [s[0] for s in TAHAJJUD_STEPS]
    idx = order.index(step)
    next_step = order[idx + 1] if idx + 1 < len(order) else None
    is_last = next_step is None
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_tahajjud_step_logs (
                    tahajjud_id, user_id, step, reflection_enc, logged_at
                ) VALUES (%s, %s, %s, %s, now())
            """, (tahajjud_id, user_id, step, encrypt(reflection, user_id)))
            if is_last:
                c.execute("""
                    UPDATE tawbah_tahajjud_sessions
                    SET current_step = NULL, completed_at = now()
                    WHERE id = %s
                """, (tahajjud_id,))
            else:
                c.execute("""
                    UPDATE tawbah_tahajjud_sessions
                    SET current_step = %s WHERE id = %s
                """, (next_step, tahajjud_id))
            conn.commit()
            return {
                "tahajjud_id": tahajjud_id,
                "completed": is_last,
                "next_step": next_step,
            }
    finally:
        release_db_connection(conn)


def get_sayyid_ul_istighfar() -> dict:
    return SAYYID_UL_ISTIGHFAR


def get_sacred_line(context: str) -> str | None:
    cfg = SACRED_LINES if isinstance(SACRED_LINES, dict) else {}
    mappings = cfg.get("context_to_line_mapping", {}).get("mappings", [])
    lines = {l["line_id"]: l for l in cfg.get("sacred_lines", [])}
    for m in mappings:
        if m.get("context") == context:
            primary = lines.get(m.get("primary_line_id"))
            if primary:
                return primary.get("text_ur_en_mix")
    return None


def dua_therapy_available() -> bool:
    return DUA_THERAPY_DB is not None


def dua_therapy_placeholder() -> dict:
    """Graceful fallback while dua_therapy_db.json is on hold."""
    return {
        "available": False,
        "message": "Dua Therapy library aa raha hai jald. Abhi ke liye Sayyid-ul-Istighfar padhein.",
        "fallback": SAYYID_UL_ISTIGHFAR,
    }
