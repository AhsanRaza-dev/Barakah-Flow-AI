"""
tier_detection.py — Hybrid tier detection (self-select + NLP signals + history).

Output: one of "light" | "medium" | "severe".
"""
import re
from tawbah_os.data_loader import TIER_DETECTION

_SEVERE_PATTERNS = [
    r"\baddict(ion|ed)\b", r"\bcan(not|'t) stop\b",
    r"\bkabhi (theek|normal) nahi\b", r"\b3\+? mahine\b",
    r"\bmonths? of\b", r"\bcontrol (nahi|lost)\b",
    r"\bhar din\b", r"\bevery day\b",
    r"\bout of control\b", r"\bobsess(ed|ion)\b",
]
_MEDIUM_PATTERNS = [
    r"\bbaar baar\b", r"\brecurring\b", r"\bagain\b", r"\bphir se\b",
    r"\bkoshish kar raha\b", r"\btrying to\b",
    r"\b2.?4 hafte\b", r"\bweeks\b",
]
_LIGHT_PATTERNS = [
    r"\bek baar\b", r"\bonce\b", r"\bslipped\b", r"\bsmall (mistake|slip)\b",
    r"\bchhoti si (galti|baat)\b",
]


def detect_from_text(text: str) -> str | None:
    """NLP-based tier inference. Returns None if no clear signal."""
    if not text:
        return None
    t = text.lower()
    if any(re.search(p, t) for p in _SEVERE_PATTERNS):
        return "severe"
    if any(re.search(p, t) for p in _MEDIUM_PATTERNS):
        return "medium"
    if any(re.search(p, t) for p in _LIGHT_PATTERNS):
        return "light"
    return None


def detect_tier(*, self_selected: str = None, user_text: str = None,
                historical_tier: str = None) -> dict:
    """
    Weighted combiner.

    Priorities (per TIER_DETECTION spec):
      self_select 0.50 + nlp_signal 0.30 + history 0.20
    Locked rule: downgrade disallowed from historical.
    """
    cfg = TIER_DETECTION if isinstance(TIER_DETECTION, dict) else {}
    weights = cfg.get("weights") or {
        "self_select": 0.5, "nlp_signal": 0.3, "history": 0.2,
    }
    tiers = {"light": 0.0, "medium": 0.0, "severe": 0.0}
    if self_selected in tiers:
        tiers[self_selected] += weights["self_select"]
    nlp_tier = detect_from_text(user_text or "")
    if nlp_tier:
        tiers[nlp_tier] += weights["nlp_signal"]
    if historical_tier in tiers:
        tiers[historical_tier] += weights["history"]
    chosen = max(tiers, key=tiers.get) if max(tiers.values()) > 0 else "light"
    # Downgrade disallowed
    order = {"light": 0, "medium": 1, "severe": 2}
    if historical_tier and order.get(chosen, 0) < order.get(historical_tier, 0):
        chosen = historical_tier
    return {
        "tier": chosen,
        "scores": tiers,
        "nlp_inferred": nlp_tier,
        "self_selected": self_selected,
        "historical": historical_tier,
    }
