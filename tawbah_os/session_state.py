"""
session_state.py — Tawbah OS session state machine.

Locked rule: one session = one dominant goal. Backend enforces.

States:
  NEW_SESSION → TIER_DETECTED → GOAL_SELECTED → ENGINES_ACTIVE → COMPLETED | ABANDONED
"""
from database import get_db_connection, release_db_connection

NEW_SESSION     = "NEW_SESSION"
TIER_DETECTED   = "TIER_DETECTED"
GOAL_SELECTED   = "GOAL_SELECTED"
ENGINES_ACTIVE  = "ENGINES_ACTIVE"
COMPLETED       = "COMPLETED"
ABANDONED       = "ABANDONED"

ALL_STATES = (NEW_SESSION, TIER_DETECTED, GOAL_SELECTED,
              ENGINES_ACTIVE, COMPLETED, ABANDONED)

_TRANSITIONS = {
    NEW_SESSION:    {TIER_DETECTED, ABANDONED},
    TIER_DETECTED:  {GOAL_SELECTED, ABANDONED},
    GOAL_SELECTED:  {ENGINES_ACTIVE, ABANDONED},
    ENGINES_ACTIVE: {COMPLETED, ABANDONED},
    COMPLETED:      set(),
    ABANDONED:      set(),
}


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in _TRANSITIONS.get(from_state, set())


def create_session(user_id: str, entry_type: str = "normal") -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO tawbah_sessions (user_id, state, entry_type)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (user_id, NEW_SESSION, entry_type))
            sid = c.fetchone()[0]
            conn.commit()
            return sid
    finally:
        release_db_connection(conn)


def get_session(session_id: int) -> dict | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, user_id, state, tier, goal_type, entry_type,
                       started_at, closed_at
                FROM tawbah_sessions WHERE id = %s
            """, (session_id,))
            r = c.fetchone()
            if not r:
                return None
            return {
                "id": r[0], "user_id": r[1], "state": r[2],
                "tier": r[3], "goal_type": r[4], "entry_type": r[5],
                "started_at": r[6], "closed_at": r[7],
            }
    finally:
        release_db_connection(conn)


def transition(session_id: int, new_state: str, *, tier: str = None,
               goal_type: str = None) -> dict:
    sess = get_session(session_id)
    if not sess:
        raise ValueError(f"session {session_id} not found")
    if not can_transition(sess["state"], new_state):
        raise ValueError(
            f"illegal transition {sess['state']} → {new_state}"
        )
    fields = ["state = %s"]
    vals = [new_state]
    if tier is not None:
        fields.append("tier = %s")
        vals.append(tier)
    if goal_type is not None:
        fields.append("goal_type = %s")
        vals.append(goal_type)
    if new_state in (COMPLETED, ABANDONED):
        fields.append("closed_at = now()")
    vals.append(session_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute(
                f"UPDATE tawbah_sessions SET {', '.join(fields)} WHERE id = %s",
                tuple(vals),
            )
            conn.commit()
    finally:
        release_db_connection(conn)
    return get_session(session_id)
