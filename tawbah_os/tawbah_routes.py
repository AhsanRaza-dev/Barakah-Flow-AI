"""
tawbah_routes.py — FastAPI APIRouter exposing Tawbah OS endpoints.

Mount in main.py:
    from tawbah_os.tawbah_routes import router as tawbah_router
    app.include_router(tawbah_router, prefix="/api/tawbah", tags=["Tawbah OS"])
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tawbah_os import (
    middleware,
    onboarding,
    session_state,
    special_protocols,
    tier_detection,
)
from tawbah_os.engines import (
    engine_0_muhasaba as eng0,
    engine_1_aqal_nafs as eng1,
    engine_2_tawbah_roadmap as eng2,
    engine_3_habit_breaking as eng3,
    engine_4_istiqamah as eng4,
    engine_5_spiritual_resurrection as eng5,
    engine_6_kaffarat as eng6,
)

router = APIRouter()


# ── Pydantic models ──────────────────────────────────────────────────────────

class OnboardingStart(BaseModel):
    user_id: str


class OnboardingScreenAdvance(BaseModel):
    user_id: str
    next_screen: int = Field(ge=1, le=5)


class ProfileSave(BaseModel):
    user_id: str
    fiqh_school: str = "hanafi"
    tone_preference: str = "urdu_english_mix"
    country_code: str | None = None
    tier_preference: str | None = None


class SessionCreate(BaseModel):
    user_id: str
    entry_type: str = "normal"


class SessionTransition(BaseModel):
    session_id: int
    new_state: str
    tier: str | None = None
    goal_type: str | None = None


class TierDetect(BaseModel):
    user_id: str
    self_selected: str | None = None
    user_text: str | None = None


class CrisisScan(BaseModel):
    user_id: str
    user_text: str
    country_code: str | None = None
    session_id: int | None = None


class MiddlewareCheck(BaseModel):
    ai_text: str
    user_text: str | None = None
    tier: str | None = None
    engine_id: str | None = None


# Engine 0
class DailyMuhasaba(BaseModel):
    user_id: str
    q1: str
    q2: str
    q3: str
    q4: str


class WeeklyMuhasaba(BaseModel):
    user_id: str
    zuban: str
    nafs: str
    qalb: str
    amal: str


class SinPatternObservation(BaseModel):
    user_id: str
    pattern_type: str
    signal_count: int
    description: str


class HeartDiseaseHandoff(BaseModel):
    user_id: str
    disease: str
    signals_count: int
    user_response: str | None = None


# Engine 1
class AqalNafsLog(BaseModel):
    user_id: str
    session_id: int
    urge_text: str
    nafs_voice: str
    aqal_voice: str
    resolution: str


# Engine 2
class RoadmapStart(BaseModel):
    user_id: str
    session_id: int
    gunah_description: str
    requires_huquq: bool = False


class RoadmapStep(BaseModel):
    roadmap_id: int
    user_id: str
    step: str
    reflection: str


class Tier3Detect(BaseModel):
    user_description: str


class NishaniyaanQuery(BaseModel):
    tone: str = "urdu_english_mix"


# Engine 3
class ReplacementQuery(BaseModel):
    trigger_text: str


class ShaytanPatternLog(BaseModel):
    user_id: str
    trigger_time: str
    location: str
    emotion: str
    gunah_category: str


class RelapseLog(BaseModel):
    user_id: str
    session_id: int
    context: str
    minutes_before_predicted: int | None = None


# Engine 4
class ChapterQuery(BaseModel):
    user_id: str
    chapter_id: str


class ChapterTick(BaseModel):
    user_id: str
    chapter_id: str


class RelapseReset(BaseModel):
    user_id: str
    chapter_id: str


class FatigueEvaluate(BaseModel):
    active_signal_ids: list[str]


class FatigueLog(BaseModel):
    user_id: str
    active_signals: list[str]
    composite_weight: float


# Engine 5
class TahajjudStart(BaseModel):
    user_id: str
    session_id: int


class TahajjudStep(BaseModel):
    tahajjud_id: int
    step: str
    reflection: str
    user_id: str


class SacredLineQuery(BaseModel):
    context: str


# Engine 6
class KaffarahActivate(BaseModel):
    user_id: str
    session_id: int
    duration_days: int
    target_gunah: str | None = None


class IstighfarLog(BaseModel):
    user_id: str
    count: int
    type: str = "basic"


class SadaqahLog(BaseModel):
    user_id: str
    amount: str
    currency: str
    recipient_type: str
    niyyah: str
    linked_gunah: str | None = None
    is_jariyah: bool = False


class HasanahLog(BaseModel):
    user_id: str
    category: str
    description: str
    niyyah: str


class MusibatSabrLog(BaseModel):
    user_id: str
    category: str
    sensitivity: str
    reflection: str


class DuaForOthersLog(BaseModel):
    user_id: str
    mode: str
    target: str | None = None


class HajjUmrahIntention(BaseModel):
    user_id: str
    type: str
    year_target: int
    niyyah: str
    reflection: str | None = None


class ExitPathwayLog(BaseModel):
    user_id: str
    exit_type: str
    session_id: int | None = None
    notes: str | None = None


# ── ROUTES ────────────────────────────────────────────────────────────────────

@router.get("/health")
def health():
    return {"status": "ok", "module": "tawbah_os"}


# ── Onboarding ────────────────────────────────────────────────────────────────

@router.get("/onboarding/screens")
def onboarding_all_screens():
    return {"screens": onboarding.get_all_screens()}


@router.get("/onboarding/screen/{screen_no}")
def onboarding_screen(screen_no: int):
    return onboarding.get_screen(screen_no)


@router.post("/onboarding/start")
def onboarding_start(p: OnboardingStart):
    return onboarding.start_onboarding(p.user_id)


@router.post("/onboarding/advance")
def onboarding_advance(p: OnboardingScreenAdvance):
    try:
        return onboarding.advance_screen(p.user_id, p.next_screen)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/onboarding/profile")
def onboarding_save_profile(p: ProfileSave):
    try:
        profile = onboarding.save_profile(
            user_id=p.user_id, fiqh_school=p.fiqh_school,
            tone_preference=p.tone_preference,
            country_code=p.country_code, tier_preference=p.tier_preference,
        )
        onboarding.complete_onboarding(p.user_id, profile)
        return profile
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/onboarding/profile/{user_id}")
def onboarding_get_profile(user_id: str):
    prof = onboarding.get_profile(user_id)
    if not prof:
        raise HTTPException(404, "profile not found")
    return prof


# ── Session ───────────────────────────────────────────────────────────────────

@router.post("/session/create")
def session_create(p: SessionCreate):
    sid = session_state.create_session(p.user_id, entry_type=p.entry_type)
    return session_state.get_session(sid)


@router.get("/session/{session_id}")
def session_get(session_id: int):
    s = session_state.get_session(session_id)
    if not s:
        raise HTTPException(404, "session not found")
    return s


@router.post("/session/transition")
def session_transition(p: SessionTransition):
    try:
        return session_state.transition(
            p.session_id, p.new_state, tier=p.tier, goal_type=p.goal_type,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Tier detection ────────────────────────────────────────────────────────────

@router.post("/tier/detect")
def tier_detect(p: TierDetect):
    prof = onboarding.get_profile(p.user_id) or {}
    historical = prof.get("tier_preference")
    return tier_detection.detect_tier(
        self_selected=p.self_selected,
        user_text=p.user_text,
        historical_tier=historical,
    )


# ── Middleware / Crisis / Safety ──────────────────────────────────────────────

@router.post("/safety/crisis-scan")
def safety_crisis_scan(p: CrisisScan):
    result = special_protocols.scan_and_route(
        user_text=p.user_text, user_id=p.user_id,
        country_code=p.country_code, session_id=p.session_id,
    )
    return result or {"crisis_detected": False}


@router.get("/safety/mental-health-bridge/{user_id}")
def safety_mental_health_bridge(user_id: str):
    return special_protocols.mental_health_bridge_payload(user_id)


@router.post("/safety/exit-pathway")
def safety_exit_pathway(p: ExitPathwayLog):
    try:
        eid = special_protocols.log_exit_pathway(
            user_id=p.user_id, exit_type=p.exit_type,
            session_id=p.session_id, notes=p.notes,
        )
        return {"exit_id": eid, "config": special_protocols.get_exit_pathway_config(p.exit_type)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/middleware/process")
def middleware_process(p: MiddlewareCheck):
    return middleware.process_response(
        ai_text=p.ai_text, user_text=p.user_text,
        tier=p.tier, engine_id=p.engine_id,
    )


# ── Engine 0 — Muhasaba ───────────────────────────────────────────────────────

@router.get("/engine0/daily/questions")
def eng0_daily_questions():
    return {"questions": eng0.get_daily_questions()}


@router.get("/engine0/weekly/categories")
def eng0_weekly_categories():
    return {"categories": eng0.get_weekly_categories(),
            "questions_raw": eng0.get_weekly_questions_raw()}


@router.post("/engine0/daily")
def eng0_daily(p: DailyMuhasaba):
    mid = eng0.daily_muhasaba(p.user_id, p.q1, p.q2, p.q3, p.q4)
    return {"muhasaba_id": mid}


@router.post("/engine0/weekly")
def eng0_weekly(p: WeeklyMuhasaba):
    wid = eng0.weekly_deep_dive(p.user_id, p.zuban, p.nafs, p.qalb, p.amal)
    return {"weekly_id": wid}


@router.post("/engine0/sin-pattern")
def eng0_sin_pattern(p: SinPatternObservation):
    pid = eng0.log_sin_pattern_observation(
        p.user_id, p.pattern_type, p.signal_count, p.description,
    )
    return {"pattern_id": pid}


@router.post("/engine0/heart-disease-handoff")
def eng0_heart_disease_handoff(p: HeartDiseaseHandoff):
    hid = eng0.log_heart_disease_handoff(
        p.user_id, p.disease, p.signals_count, p.user_response,
    )
    return {"handoff_id": hid, "route_to": "tibb_e_nabawi"}


@router.get("/engine0/sahaba-snippet")
def eng0_sahaba_snippet(rotation_index: int = 0):
    return eng0.get_sahaba_snippet(rotation_index) or {"snippet": None}


@router.get("/engine0/heart-disease-signals")
def eng0_heart_disease_signals():
    return eng0.get_heart_disease_signals()


# ── Engine 1 — Aqal vs Nafs ───────────────────────────────────────────────────

@router.get("/engine1/config")
def eng1_config():
    return eng1.get_config()


@router.post("/engine1/log")
def eng1_log(p: AqalNafsLog):
    nid = eng1.log_negotiation(
        p.user_id, p.session_id, p.urge_text,
        p.nafs_voice, p.aqal_voice, p.resolution,
    )
    return {"negotiation_id": nid}


# ── Engine 2 — Tawbah Roadmap ─────────────────────────────────────────────────

@router.post("/engine2/start")
def eng2_start(p: RoadmapStart):
    rid = eng2.start_roadmap(
        p.user_id, p.session_id, p.gunah_description, p.requires_huquq,
    )
    return {"roadmap_id": rid}


@router.post("/engine2/step")
def eng2_step(p: RoadmapStep):
    try:
        return eng2.complete_step(p.roadmap_id, p.step, p.reflection, p.user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/engine2/tier3-detect")
def eng2_tier3(p: Tier3Detect):
    match = eng2.detect_tier3_case(p.user_description)
    return {"tier3_case": match, "needs_mufti": match is not None}


@router.post("/engine2/nishaniyaan")
def eng2_nishaniyaan(p: NishaniyaanQuery):
    return eng2.get_nishaniyaan_payload(p.tone)


# ── Engine 3 — Habit Breaking ─────────────────────────────────────────────────

@router.post("/engine3/find-replacement")
def eng3_find_replacement(p: ReplacementQuery):
    return {"replacements": eng3.find_replacement(p.trigger_text)}


@router.post("/engine3/shaytan-pattern")
def eng3_shaytan_pattern(p: ShaytanPatternLog):
    pid = eng3.log_shaytan_pattern(
        p.user_id, p.trigger_time, p.location, p.emotion, p.gunah_category,
    )
    return {"pattern_id": pid}


@router.post("/engine3/relapse")
def eng3_relapse(p: RelapseLog):
    rid = eng3.log_relapse(
        p.user_id, p.session_id, p.context, p.minutes_before_predicted,
    )
    return {"relapse_id": rid}


@router.get("/engine3/predict-risk/{user_id}")
def eng3_predict(user_id: str):
    return eng3.predict_next_risk_window(user_id) or {"predicted_window": None}


@router.get("/engine3/bad-habit-subtypes")
def eng3_subtypes():
    return eng3.get_bad_habit_subtypes()


@router.get("/engine3/internal-dialogue")
def eng3_dialogue():
    return eng3.get_internal_dialogue_corrections()


@router.get("/engine3/emergency-mode")
def eng3_emergency():
    return eng3.emergency_mode_payload()


# ── Engine 4 — Istiqamah + Ruhani Fatigue ─────────────────────────────────────

@router.post("/engine4/streak")
def eng4_streak(p: ChapterQuery):
    return eng4.get_chapter_streak(p.user_id, p.chapter_id)


@router.post("/engine4/tick")
def eng4_tick(p: ChapterTick):
    return eng4.tick_chapter_day(p.user_id, p.chapter_id)


@router.post("/engine4/relapse-reset")
def eng4_relapse_reset(p: RelapseReset):
    return eng4.record_relapse_reset(p.user_id, p.chapter_id)


@router.post("/engine4/fatigue/evaluate")
def eng4_fatigue_evaluate(p: FatigueEvaluate):
    return eng4.evaluate_ruhani_fatigue(p.active_signal_ids)


@router.post("/engine4/fatigue/log")
def eng4_fatigue_log(p: FatigueLog):
    fid = eng4.log_fatigue_detection(
        p.user_id, p.active_signals, p.composite_weight,
    )
    return {"fatigue_id": fid}


# ── Engine 5 — Spiritual Resurrection ─────────────────────────────────────────

@router.post("/engine5/tahajjud/start")
def eng5_tahajjud_start(p: TahajjudStart):
    tid = eng5.start_tahajjud_session(p.user_id, p.session_id)
    return {"tahajjud_id": tid, "steps": [s[0] for s in eng5.TAHAJJUD_STEPS]}


@router.post("/engine5/tahajjud/step")
def eng5_tahajjud_step(p: TahajjudStep):
    try:
        return eng5.complete_tahajjud_step(
            p.tahajjud_id, p.step, p.reflection, p.user_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/engine5/sayyid-ul-istighfar")
def eng5_sayyid():
    return eng5.get_sayyid_ul_istighfar()


@router.post("/engine5/sacred-line")
def eng5_sacred_line(p: SacredLineQuery):
    text = eng5.get_sacred_line(p.context)
    return {"context": p.context, "line": text}


@router.get("/engine5/dua-therapy/status")
def eng5_dua_therapy_status():
    if eng5.dua_therapy_available():
        return {"available": True}
    return eng5.dua_therapy_placeholder()


# ── Engine 6 — Kaffarat ───────────────────────────────────────────────────────

@router.post("/engine6/activate")
def eng6_activate(p: KaffarahActivate):
    try:
        kid = eng6.activate(
            p.user_id, p.session_id, p.duration_days, p.target_gunah,
        )
        return {"kaffarah_id": kid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/engine6/istighfar")
def eng6_istighfar(p: IstighfarLog):
    try:
        lid = eng6.log_istighfar(p.user_id, p.count, p.type)
        return {"log_id": lid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/engine6/sadaqah")
def eng6_sadaqah(p: SadaqahLog):
    sid = eng6.log_sadaqah(
        p.user_id, p.amount, p.currency, p.recipient_type,
        p.niyyah, p.linked_gunah, p.is_jariyah,
    )
    return {"sadaqah_id": sid}


@router.post("/engine6/hasanah")
def eng6_hasanah(p: HasanahLog):
    hid = eng6.log_hasanah(p.user_id, p.category, p.description, p.niyyah)
    return {"hasanah_id": hid}


@router.post("/engine6/musibat-sabr")
def eng6_musibat(p: MusibatSabrLog):
    mid = eng6.log_musibat_sabr(
        p.user_id, p.category, p.sensitivity, p.reflection,
    )
    return {"log_id": mid}


@router.post("/engine6/dua-for-others")
def eng6_dua_others(p: DuaForOthersLog):
    try:
        did = eng6.log_dua_for_others(p.user_id, p.mode, p.target)
        return {"log_id": did}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/engine6/hajj-umrah")
def eng6_hajj_umrah(p: HajjUmrahIntention):
    try:
        hid = eng6.log_hajj_umrah_intention(
            p.user_id, p.type, p.year_target, p.niyyah, p.reflection,
        )
        return {"intention_id": hid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/engine6/weekly-summary/{user_id}")
def eng6_weekly_summary(user_id: str):
    return eng6.weekly_summary(user_id)


@router.get("/engine6/config")
def eng6_config():
    return eng6.get_config()
