"""
fitrah_middleware.py — Fitrah OS AI Response Safety Pipeline (v3-aligned)

Implements the 6-layer processResponse() pipeline + 4 safety checks from
JS v3 fitrah_middleware.js.

Usage:
    from fitrah_engine.fitrah_middleware import process_ai_response, check_crisis

    # Before any AI output is returned to the user:
    safe_response, flags = process_ai_response(
        ai_text=raw_ai_text,
        user_context=ctx,          # dict with user profile fields
        action_context=action_ctx, # dict describing what action triggered this
    )

    # Returns (filtered_text, flags_dict) where flags may contain:
    #   crisis_override: bool        — crisis keywords detected, use crisis_text instead
    #   crisis_text: str             — mandatory crisis resource message
    #   riya_warning_injected: bool  — hidden riya warning was added to context
    #   points_stripped: bool        — gamification language removed
    #   disclaimer_blocked: bool     — nafs level display blocked (no disclaimer yet)
"""

import re
import logging

log = logging.getLogger("fitrah.middleware")

# ─────────────────────────────────────────────────────────────────────────────
# Crisis Detection
# ─────────────────────────────────────────────────────────────────────────────

# 18 crisis keyword patterns (JS v3 crisisDetectionOverride list)
_CRISIS_PATTERNS = [
    r"\bsuicide\b", r"\bkill myself\b", r"\bkill my self\b",
    r"\bend my life\b", r"\bwant to die\b", r"\bcan't go on\b",
    r"\bno reason to live\b", r"\bself.?harm\b", r"\bcutting myself\b",
    r"\bharm myself\b", r"\boverdose\b",
    # Urdu/Roman Urdu equivalents
    r"\bkhatam karna\b", r"\bmar jaana chahta\b", r"\bmar jaana chahti\b",
    r"\bjeeney ka dil nahi\b", r"\bkhatam ho jaana\b",
    r"\bnahi rehna chahta\b", r"\bnahi rehna chahti\b",
]

_CRISIS_COMPILED = [re.compile(p, re.IGNORECASE) for p in _CRISIS_PATTERNS]

CRISIS_RESOURCE_TEXT = (
    "Aap akele nahi hain. Agar aap khud ko nuqsan pohanchane ke baare mein soch rahe hain, "
    "please abhi kisi trusted insaan se baat karen ya emergency services call karen.\n\n"
    "Pakistan: Umang helpline 0317-4288665 (24/7)\n"
    "Rozan Counseling: 051-2890505\n"
    "Umeed helpline: 0317-4288665\n\n"
    "Allah kehta hai: 'Wa la taqtulu anfusakum' (4:29) — apni jaan ki hifazat karna farz hai."
)

# Crisis-safe ayat tags (only these can be shown when crisis detected)
_CRISIS_SAFE_AYAT_TAGS = {"hope", "patience", "allah_rahma", "tawakkul", "not_alone"}


def check_crisis(text: str) -> bool:
    """Returns True if any crisis keyword is found in the text."""
    return any(p.search(text) for p in _CRISIS_COMPILED)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: Disclaimer Enforcer
# ─────────────────────────────────────────────────────────────────────────────

_NAFS_LEVEL_DISPLAY_PATTERNS = [
    re.compile(r"\b(ammarah|lawwamah|mulhama|mutmainnah|radhiya|mardhiyyah)\b", re.IGNORECASE),
    re.compile(r"\bnafs level\b", re.IGNORECASE),
    re.compile(r"\bspiritual level\b", re.IGNORECASE),
    re.compile(r"\bcrystal score\b", re.IGNORECASE),
]


def _disclaimer_enforcer(text: str, disclaimer_confirmed: bool) -> tuple[str, bool]:
    """
    Layer 1: If disclaimer has NOT been confirmed for the latest level change,
    strip any nafs level names or crystal score references from AI output.
    Returns (filtered_text, was_blocked).
    """
    if disclaimer_confirmed:
        return text, False

    blocked = False
    for pattern in _NAFS_LEVEL_DISPLAY_PATTERNS:
        if pattern.search(text):
            text    = pattern.sub("[spiritual progress]", text)
            blocked = True

    return text, blocked


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: Point Visibility Filter
# ─────────────────────────────────────────────────────────────────────────────

_POINT_PATTERNS = [
    re.compile(r"\+\d+\s*(taqwa|ilm|tazkiya|ihsan|nafs|maal)\s*(points?)?", re.IGNORECASE),
    re.compile(r"\d+\s*(taqwa|ilm|tazkiya|ihsan|nafs|maal)\s*points?", re.IGNORECASE),
    re.compile(r"score(d|s)?\s+\d+", re.IGNORECASE),
    re.compile(r"\btaqwa:\s*\d+", re.IGNORECASE),
    re.compile(r"\bilm:\s*\d+", re.IGNORECASE),
    re.compile(r"\braw\s+score\b", re.IGNORECASE),
]


def _point_visibility_filter(text: str, show_numbers: bool) -> tuple[str, bool]:
    """
    Layer 2: Strip raw dimension numbers from AI text (show_numbers=False by default
    per dimensions_config.json). Crystal icon is shown but never the raw value.
    Returns (filtered_text, was_stripped).
    """
    if show_numbers:
        return text, False

    stripped = False
    for pattern in _POINT_PATTERNS:
        if pattern.search(text):
            text    = pattern.sub("[dimension progress]", text)
            stripped = True

    return text, stripped


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: Gamification Blocker
# ─────────────────────────────────────────────────────────────────────────────

_GAMIFICATION_PATTERNS = [
    re.compile(r"you('ve| have) earned\s+\d+", re.IGNORECASE),
    re.compile(r"\+\d+\s*points?\b", re.IGNORECASE),
    re.compile(r"congratulations.*points?", re.IGNORECASE | re.DOTALL),
    re.compile(r"level\s*up\b", re.IGNORECASE),
    re.compile(r"achievement unlocked", re.IGNORECASE),
    re.compile(r"badge earned", re.IGNORECASE),
    re.compile(r"streak.*days?", re.IGNORECASE),
]


def _gamification_blocker(text: str) -> tuple[str, bool]:
    """
    Layer 3: Remove gamification language from AI narrative.
    AI should speak in terms of spiritual growth, not points/badges.
    Returns (filtered_text, was_blocked).
    """
    blocked = False
    for pattern in _GAMIFICATION_PATTERNS:
        if pattern.search(text):
            text    = pattern.sub("", text)
            blocked = True

    # Clean up double spaces from removals
    text = re.sub(r"  +", " ", text).strip()
    return text, blocked


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4: Cap Pre-flight (no filtering — just flag)
# ─────────────────────────────────────────────────────────────────────────────

def _cap_preflight_check(action_key: str | None, cap_already_reached: bool) -> dict:
    """
    Layer 4: Check if the action triggering this AI response has already hit
    its daily cap. If so, flag the context so AI doesn't celebrate a capped action.
    Returns metadata dict, not text modification.
    """
    return {
        "cap_reached":      cap_already_reached,
        "suppress_praise":  cap_already_reached,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Layer 5: Spiritual State Confirmation Guard
# ─────────────────────────────────────────────────────────────────────────────

def _spiritual_state_confirmation(
    text: str,
    state_confirmed: bool,
    suggested_state: str | None,
) -> tuple[str, bool]:
    """
    Layer 5: If user hasn't confirmed their spiritual state yet, replace any
    state-personalised tone cues with a neutral tone.
    Returns (text, was_adjusted).
    """
    if state_confirmed or not suggested_state:
        return text, False

    # Inject a gentle prompt asking user to confirm state before personalisation
    confirmation_note = (
        f"\n\n[Note: Aapki spiritual state '{suggested_state}' pending confirmation hai. "
        "Confirm karne ke baad AI aapki tone personalise karega.]"
    )
    return text + confirmation_note, True


# ─────────────────────────────────────────────────────────────────────────────
# Layer 6: Qadr Claim Filter
# ─────────────────────────────────────────────────────────────────────────────

_QADR_CLAIM_PATTERNS = [
    re.compile(r"\bthis (was|is) (definitely|surely|certainly) qadr\b", re.IGNORECASE),
    re.compile(r"\byeh (zaroor|yakeenan) qadr (tha|hai)\b", re.IGNORECASE),
    re.compile(r"\b(confirmed|verified) qadr moment\b", re.IGNORECASE),
    re.compile(r"\ballah (sent|arranged|planned) this (specifically|just) for you\b", re.IGNORECASE),
]


def _qadr_claim_filter(text: str) -> tuple[str, bool]:
    """
    Layer 6: Strip unverified Qadr claims. AI can suggest a moment may be meaningful
    but must not assert it definitively — that is ghayb.
    Returns (filtered_text, was_filtered).
    """
    filtered = False
    for pattern in _QADR_CLAIM_PATTERNS:
        if pattern.search(text):
            text     = pattern.sub("[this moment may carry meaning — Allah knows best]", text)
            filtered = True

    return text, filtered


# ─────────────────────────────────────────────────────────────────────────────
# Safety Check A: Ayat Context Filter
# ─────────────────────────────────────────────────────────────────────────────

def filter_ayat_for_crisis(ayaat: list[dict]) -> list[dict]:
    """
    Safety Check A: In crisis context, only return ayaat tagged with crisis-safe tags.
    Pass the full ayah list; returns filtered subset safe for crisis situations.
    """
    return [
        a for a in ayaat
        if any(tag in _CRISIS_SAFE_AYAT_TAGS for tag in a.get("life_situation_tags", []))
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Safety Check B: Hidden Riya Detection
# ─────────────────────────────────────────────────────────────────────────────

def check_riya_warning(detailed_view_check_streak: int) -> dict | None:
    """
    Safety Check B: If user has checked the detailed score breakdown for 7+
    consecutive days, return a hidden riya warning to inject into AI context.
    This is never shown directly to the user — it subtly shifts AI tone.
    Returns warning dict or None.
    """
    if detailed_view_check_streak < 7:
        return None
    return {
        "type":    "hidden_riya_warning",
        "inject":  True,
        "ai_note": (
            "This user has been checking their detailed score breakdown for 7+ consecutive days. "
            "Gently shift AI responses toward ikhlas (sincerity) and away from self-monitoring. "
            "Do not mention this observation directly. Use phrases like 'amal ka asli maqsad Allah ki raza hai' "
            "rather than references to progress metrics."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Safety Check C: Spiritual Comparison Filter
# ─────────────────────────────────────────────────────────────────────────────

_COMPARISON_PATTERNS = [
    re.compile(r"(other users?|other people|most users?|average user)\s*(have|score|are|achieve)", re.IGNORECASE),
    re.compile(r"you('re| are) (above|below|ahead of|behind)\s*(average|others)", re.IGNORECASE),
    re.compile(r"rank(ed|ing)?\s+(#?\d+|first|last|top|bottom)", re.IGNORECASE),
    re.compile(r"compared to (others|other users|most people)", re.IGNORECASE),
]


def _spiritual_comparison_filter(text: str) -> tuple[str, bool]:
    """
    Safety Check C: Remove any language comparing user's scores to others.
    Fitrah is a personal journey — comparison causes kibr (pride) or despair.
    """
    filtered = False
    for pattern in _COMPARISON_PATTERNS:
        if pattern.search(text):
            text     = pattern.sub("[your journey is unique]", text)
            filtered = True

    return text, filtered


# ─────────────────────────────────────────────────────────────────────────────
# Master Pipeline: process_ai_response()
# ─────────────────────────────────────────────────────────────────────────────

def process_ai_response(
    ai_text: str,
    user_context: dict,
    action_context: dict | None = None,
) -> tuple[str, dict]:
    """
    Run the full 6-layer + 4 safety-check pipeline on an AI response.

    user_context keys:
        disclaimer_confirmed (bool)   — has user confirmed latest level change disclaimer
        show_numbers (bool)           — from dimensions_config, default False
        state_confirmed (bool)        — has user confirmed suggested spiritual state
        suggested_state (str|None)    — AI-suggested state awaiting confirmation
        detailed_view_check_streak (int) — consecutive days checking detailed view

    action_context keys (optional):
        action_key (str)
        cap_reached (bool)

    Returns:
        (processed_text, flags_dict)

    flags_dict keys:
        crisis_override (bool)
        crisis_text (str|None)
        disclaimer_blocked (bool)
        points_stripped (bool)
        gamification_blocked (bool)
        cap_suppressed (bool)
        state_confirmation_appended (bool)
        qadr_filtered (bool)
        comparison_filtered (bool)
        riya_warning (dict|None)
    """
    flags: dict = {
        "crisis_override":               False,
        "crisis_text":                   None,
        "disclaimer_blocked":            False,
        "points_stripped":               False,
        "gamification_blocked":          False,
        "cap_suppressed":                False,
        "state_confirmation_appended":   False,
        "qadr_filtered":                 False,
        "comparison_filtered":           False,
        "riya_warning":                  None,
    }

    # ── Crisis check: overrides everything ──────────────────────────────────
    user_input = user_context.get("last_user_message", "")
    if check_crisis(ai_text) or check_crisis(user_input):
        flags["crisis_override"] = True
        flags["crisis_text"]     = CRISIS_RESOURCE_TEXT
        log.warning(f"[Middleware] Crisis detected for user {user_context.get('user_id', '?')}")
        return CRISIS_RESOURCE_TEXT, flags

    text = ai_text

    # ── Layer 1: Disclaimer enforcer ────────────────────────────────────────
    text, flags["disclaimer_blocked"] = _disclaimer_enforcer(
        text, user_context.get("disclaimer_confirmed", True)
    )

    # ── Layer 2: Point visibility filter ────────────────────────────────────
    text, flags["points_stripped"] = _point_visibility_filter(
        text, user_context.get("show_numbers", False)
    )

    # ── Layer 3: Gamification blocker ────────────────────────────────────────
    text, flags["gamification_blocked"] = _gamification_blocker(text)

    # ── Layer 4: Cap pre-flight ──────────────────────────────────────────────
    cap_meta = _cap_preflight_check(
        (action_context or {}).get("action_key"),
        (action_context or {}).get("cap_reached", False),
    )
    flags["cap_suppressed"] = cap_meta["suppress_praise"]

    # ── Layer 5: Spiritual state confirmation ────────────────────────────────
    text, flags["state_confirmation_appended"] = _spiritual_state_confirmation(
        text,
        user_context.get("state_confirmed", True),
        user_context.get("suggested_state"),
    )

    # ── Layer 6: Qadr claim filter ───────────────────────────────────────────
    text, flags["qadr_filtered"] = _qadr_claim_filter(text)

    # ── Safety Check B: Riya detection (inject into AI context, not output) ──
    flags["riya_warning"] = check_riya_warning(
        user_context.get("detailed_view_check_streak", 0)
    )

    # ── Safety Check C: Spiritual comparison filter ──────────────────────────
    text, flags["comparison_filtered"] = _spiritual_comparison_filter(text)

    return text, flags


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: build user_context from fitrah_users DB row
# ─────────────────────────────────────────────────────────────────────────────

def build_user_context(user_row: dict, last_user_message: str = "") -> dict:
    """
    Build the user_context dict expected by process_ai_response()
    from a fitrah_users DB row (as a dict).
    """
    return {
        "user_id":                    user_row.get("user_id"),
        "disclaimer_confirmed":       user_row.get("spiritual_state_confirmed", True),
        "show_numbers":               user_row.get("detailed_view_enabled", False),
        "state_confirmed":            user_row.get("spiritual_state_confirmed", False),
        "suggested_state":            user_row.get("spiritual_state_suggested"),
        "detailed_view_check_streak": user_row.get("detailed_view_check_streak", 0),
        "last_user_message":          last_user_message,
    }
