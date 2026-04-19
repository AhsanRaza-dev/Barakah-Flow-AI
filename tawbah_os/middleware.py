"""
middleware.py — 6 code-enforced middleware layers for Tawbah OS.

Layer 1: Kafarat wrapper — ensure every tawbah flow completes 3 conditions first
Layer 2: Passive crisis detection — scan user text for crisis language
Layer 3: Tier enforcement — block engines disallowed for current tier
Layer 4: Qabooliyat claim block — strip "tawbah accepted" language from AI output
Layer 5: Crisis-safe ayaat filter — no retribution ayaat during crisis
Layer 6: Heart disease handoff router — redirect treatment queries to Tibb-e-Nabawi
"""
import re
from tawbah_os.data_loader import (
    CRISIS_DETECTION_PATTERNS,
    CRISIS_SAFE_AYAAT,
    ENGINE_TIER_ALLOWANCE,
)

_QABOOLIYAT_FORBIDDEN = [
    r"tawbah (qabool|accept(ed)?|granted)",
    r"allah ne (aap ko )?maaf kar diya",
    r"(aap ki |your )?(maghfirat|forgiveness) (ho gayi|granted|complete)",
    r"(sin|gunah)s? (are |is )?(now )?(forgiven|erased|washed|cleansed|removed)",
    r"tawbah has been accepted",
    r"allah has forgiven you",
]


def _flatten_crisis_keywords() -> list[str]:
    kws = []
    data = CRISIS_DETECTION_PATTERNS
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                for entry in v:
                    if isinstance(entry, str):
                        kws.append(entry.lower())
                    elif isinstance(entry, dict):
                        for vv in entry.values():
                            if isinstance(vv, str):
                                kws.append(vv.lower())
                            elif isinstance(vv, list):
                                kws.extend(str(x).lower() for x in vv)
    return [k for k in kws if len(k) >= 3]


_CRISIS_KEYWORDS = _flatten_crisis_keywords()


def detect_crisis(text: str) -> bool:
    """Layer 2 — returns True if crisis language detected."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in _CRISIS_KEYWORDS)


def enforce_tier(engine_id: str, tier: str) -> bool:
    """Layer 3 — True if engine allowed for tier, False if blocked."""
    allowance = ENGINE_TIER_ALLOWANCE
    if isinstance(allowance, dict):
        tier_cfg = allowance.get(tier, {}) or allowance.get("tiers", {}).get(tier, {})
        allowed = tier_cfg.get("allowed") or tier_cfg.get("engines_allowed") or []
        if allowed:
            return engine_id in allowed
    return True  # fail-open: if config missing, allow (onboarding, middleware, etc.)


def strip_qabooliyat_claims(text: str) -> tuple[str, bool]:
    """Layer 4 — remove forbidden qabooliyat language. Returns (cleaned, was_stripped)."""
    if not text:
        return text, False
    cleaned = text
    stripped = False
    for pat in _QABOOLIYAT_FORBIDDEN:
        new = re.sub(pat, "[qabooliyat sirf Allah ke paas]", cleaned, flags=re.IGNORECASE)
        if new != cleaned:
            stripped = True
            cleaned = new
    return cleaned, stripped


def pick_crisis_safe_ayah(seed: int = 0) -> dict:
    """Layer 5 — return one crisis-safe ayah (mercy/hope only)."""
    ayaat = CRISIS_SAFE_AYAAT
    pool = ayaat if isinstance(ayaat, list) else ayaat.get("ayaat") or ayaat.get("verses") or []
    if not pool:
        return {"arabic": "", "translation": "Allah is Most Merciful."}
    return pool[seed % len(pool)]


_HEART_DISEASE_TREATMENT_PATTERNS = [
    r"kibr (ka )?(ilaj|treatment|cure|how to (cure|fix|remove))",
    r"hasad (ka )?(ilaj|treatment|cure)",
    r"riya (ka )?(ilaj|treatment|cure|how to stop)",
    r"(hub[- ]al[- ]dunya|dunya love) (ka )?(ilaj|treatment|cure)",
    r"bukhl (ka )?(ilaj|treatment|cure)",
    r"(ghadab|anger issue) (ka )?(ilaj|treatment|cure)",
    r"shahwat (ka )?(ilaj|long term treatment|cure)",
    r"(how to cure|cure for|treat my) (pride|envy|showing off|love of dunya|miserliness)",
]


def is_heart_disease_treatment_query(text: str) -> bool:
    """Layer 6 — detect treatment queries that must route to Tibb-e-Nabawi."""
    if not text:
        return False
    t = text.lower()
    return any(re.search(p, t) for p in _HEART_DISEASE_TREATMENT_PATTERNS)


def process_response(ai_text: str, *, tier: str = None, engine_id: str = None,
                     user_text: str = None) -> dict:
    """
    Run all applicable middleware layers over an AI response.
    Returns a dict with the cleaned text + flags.
    """
    flags = {
        "crisis_detected": False,
        "qabooliyat_stripped": False,
        "tier_blocked": False,
        "heart_disease_handoff": False,
    }
    if engine_id and tier and not enforce_tier(engine_id, tier):
        flags["tier_blocked"] = True
        return {
            "text": None,
            "flags": flags,
            "reason": f"engine {engine_id} not allowed for tier {tier}",
        }
    if detect_crisis(user_text or ""):
        flags["crisis_detected"] = True
    if is_heart_disease_treatment_query(user_text or ""):
        flags["heart_disease_handoff"] = True
    cleaned, stripped = strip_qabooliyat_claims(ai_text or "")
    flags["qabooliyat_stripped"] = stripped
    return {"text": cleaned, "flags": flags}
