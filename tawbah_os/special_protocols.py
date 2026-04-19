"""
special_protocols.py — Tawbah OS safety-critical protocols.

Protocols:
  1. Crisis Protocol       — immediate compassion + helpline + crisis-safe ayah
  2. Mental Health Bridge  — acknowledge + dual-action (Islamic + professional)
  3. Exit Pathways         — completed / abandoned / paused / handoffs
"""
import hashlib
from database import get_db_connection, release_db_connection
from tawbah_os.data_loader import (
    CRISIS_DETECTION_PATTERNS,
    CRISIS_SAFE_AYAAT,
    HELPLINES_BY_COUNTRY,
    MENTAL_HEALTH_BRIDGE,
    EXIT_PATHWAYS,
)
from tawbah_os.middleware import detect_crisis, pick_crisis_safe_ayah


def get_helpline(country_code: str = None, helpline_type: str = "suicide") -> dict | None:
    data = HELPLINES_BY_COUNTRY if isinstance(HELPLINES_BY_COUNTRY, dict) else {}
    by_country = data.get("helplines_by_country") or data.get("countries") or data
    if country_code and isinstance(by_country, dict):
        entry = by_country.get(country_code.upper()) or by_country.get(country_code.lower())
        if entry:
            lines = entry.get("helplines") if isinstance(entry, dict) else None
            if isinstance(lines, list):
                for ln in lines:
                    if ln.get("type", "").lower() == helpline_type.lower():
                        return ln
                return lines[0] if lines else None
            return entry
    fallback = data.get("international_fallback") or data.get("fallback") or {}
    return fallback or None


def log_crisis_detection(user_id: str, trigger_phrase: str,
                         session_id: int = None,
                         response_offered: str = "crisis_payload") -> int:
    h = hashlib.sha256((trigger_phrase or "").encode("utf-8")).hexdigest()[:32]
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_crisis_detections (
                    user_id, session_id, trigger_phrase_hash,
                    response_offered, detected_at
                ) VALUES (%s, %s, %s, %s, now())
                RETURNING id
            """, (user_id, session_id, h, response_offered))
            cid = c.fetchone()[0]
            conn.commit()
            return cid
    finally:
        release_db_connection(conn)


def log_helpline_display(user_id: str, country_code: str, helpline_type: str,
                         trigger_context: str = "crisis") -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_helpline_display_log (
                    user_id, country_code, helpline_type,
                    trigger_context, displayed_at
                ) VALUES (%s, %s, %s, %s, now())
                RETURNING id
            """, (user_id, country_code, helpline_type, trigger_context))
            hid = c.fetchone()[0]
            conn.commit()
            return hid
    finally:
        release_db_connection(conn)


def crisis_protocol_payload(user_id: str, country_code: str = None,
                            session_id: int = None,
                            user_text: str = "") -> dict:
    """
    Build full crisis-response payload:
      - compassionate message
      - one crisis-safe ayah
      - country helpline
      - logs detection + helpline display
    """
    ayah = pick_crisis_safe_ayah(seed=int(hashlib.md5(
        (user_id or "").encode("utf-8")).hexdigest(), 16) % 997)
    helpline = get_helpline(country_code=country_code, helpline_type="suicide")
    log_crisis_detection(user_id, user_text, session_id=session_id)
    if helpline:
        log_helpline_display(user_id, country_code or "INTL", "suicide",
                             trigger_context="crisis_protocol")
    return {
        "crisis_detected": True,
        "message_ur_en": (
            "Aap akele nahi hain. Aap ki zindagi Allah ki amanat hai — "
            "abhi madad milna zaroori hai. Barah-e-karam yeh helpline call karein."
        ),
        "ayah": ayah,
        "helpline": helpline,
        "do_not_proceed_with_engines": True,
    }


def mental_health_bridge_payload(user_id: str) -> dict:
    """
    Mental-Health Bridge — Islamic guidance + strong suggestion to seek
    a qualified professional. Never treats mental illness as a spiritual failure.
    """
    cfg = MENTAL_HEALTH_BRIDGE if isinstance(MENTAL_HEALTH_BRIDGE, dict) else {}
    acknowledgement = (
        cfg.get("acknowledgement")
        or "Jo aap mehsoos kar rahe hain woh sach hai aur sun-ne layaq hai. "
           "Ruhani aur jismi dono ilaj zaroori ho sakte hain."
    )
    islamic_side = cfg.get("islamic_side") or {
        "dua": "Hasbunallahu wa ni'mal wakeel.",
        "practice": "Zikr, 2 rakat nafil, aur apne qareebi se baat karein.",
    }
    professional_side = cfg.get("professional_side") or {
        "recommendation": (
            "Barah-e-karam ek licensed mental-health professional se milein. "
            "Yeh kamzori nahi — yeh hikmat hai."
        ),
    }
    return {
        "acknowledgement": acknowledgement,
        "islamic_side": islamic_side,
        "professional_side": professional_side,
        "reminder": "Dono raste saath chalte hain — ek doosre ke muqabil nahi.",
    }


_VALID_EXIT_TYPES = (
    "completed", "abandoned", "paused",
    "mufti_handoff", "tibb_handoff", "mental_health_bridge",
)


def log_exit_pathway(user_id: str, exit_type: str,
                     session_id: int = None, notes: str = None) -> int:
    if exit_type not in _VALID_EXIT_TYPES:
        raise ValueError(f"exit_type must be one of {_VALID_EXIT_TYPES}")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_exit_pathways (
                    user_id, session_id, exit_type, notes, exited_at
                ) VALUES (%s, %s, %s, %s, now())
                RETURNING id
            """, (user_id, session_id, exit_type, notes))
            eid = c.fetchone()[0]
            conn.commit()
            return eid
    finally:
        release_db_connection(conn)


def get_exit_pathway_config(exit_type: str) -> dict:
    cfg = EXIT_PATHWAYS if isinstance(EXIT_PATHWAYS, dict) else {}
    paths = cfg.get("pathways") or cfg.get("exit_pathways") or {}
    if isinstance(paths, dict):
        return paths.get(exit_type, {}) or {}
    if isinstance(paths, list):
        for p in paths:
            if p.get("type") == exit_type or p.get("key") == exit_type:
                return p
    return {}


def scan_and_route(user_text: str, user_id: str, country_code: str = None,
                   session_id: int = None) -> dict | None:
    """
    Convenience gate — call before every engine invocation.
    Returns crisis payload if crisis detected, else None.
    """
    if detect_crisis(user_text or ""):
        return crisis_protocol_payload(
            user_id=user_id, country_code=country_code,
            session_id=session_id, user_text=user_text,
        )
    return None
