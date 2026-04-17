# Fitrah Engine — API Reference

Barakah AI's Fitrah OS spiritual-tracking layer. Mounted under `/api/fitrah/*`
by the RAG app ([rag_engine/app/main.py:475-476](../rag_engine/app/main.py#L475-L476)).

All endpoints require a Supabase JWT (or the dev static token when
`SUPABASE_JWT_SECRET` is unset) as `Authorization: Bearer <token>`.
User ownership is enforced: JWT `sub` must match the `user_id` parameter
unless the caller is `anonymous`.

---

## Table of Contents

1. [Auth, CORS, rate limits](#auth-cors-rate-limits)
2. [Core user + scoring](#core-user--scoring)
3. [Maqsad Engine (AI)](#maqsad-engine-ai)
4. [Nafs Battlefield + Qalb + Spiritual State](#nafs-battlefield--qalb--spiritual-state)
5. [Barakah Time sessions](#barakah-time-sessions)
6. [Onboarding + profiler](#onboarding--profiler)
7. [Dua tracking](#dua-tracking)
8. [Kafarat + Ihtisab](#kafarat--ihtisab)
9. [Balance, penalties, suggestions](#balance-penalties-suggestions)
10. [Islamic red lines (PDF §04/§10/§15/§16/§23)](#islamic-red-lines)
11. [Background scheduler jobs](#background-scheduler-jobs)
12. [Known issues](#known-issues)

---

## Auth, CORS, rate limits

| Concern | Behaviour |
|---|---|
| JWT | Supabase HS256 when `SUPABASE_JWT_SECRET` set — always enforced if configured |
| Dev fallback | Static `API_BEARER_TOKEN` honoured **only** when JWT secret is unset AND the env var is explicitly set. No hardcoded default |
| CORS | Defaults to localhost; set `ALLOWED_ORIGINS=https://app.example.com,...` in prod |
| Rate limit (global) | 120 req/min per IP (slowapi) |
| Rate limit (AI) | 10 req/min per IP on `/maqsad/qadr`, `/maqsad/life_test`, `/battlefield/analyze`; 30/min on RAG `/api/ask` |
| 500 errors | Never leak psycopg2 text — server logs with `log.exception`, client sees `"Internal server error."` |

---

## Core user + scoring

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/user/setup` | Create/initialise a Fitrah user row (archetype, life stage, fiqh school). Idempotent on re-call |
| `GET`  | `/user/{user_id}/profile` | Full profile: dimensions, crystal score, nafs level, streak, sahaba match, riya warning, plus `drift_pause_until`, `quranic_mirror_muted` |
| `PATCH`| `/user/settings` | Toggle `detailed_view_enabled`, `tone_preference` (`urdu_english_mix`/`urdu_only`/`english_only`), `quranic_mirror_muted` |
| `POST` | `/log_action` | Log an Islamic action → award points, update dimensions + crystal + nafs level. Returns `regression_disclaimer` on downward nafs transitions (PDF §04 Rule 3) |
| `POST` | `/nafs/confirm-promotion` | Confirm pending promotion after user views disclaimer. Required for levels Radhiya / Mardhiyyah (mufti review) |
| `POST` | `/log_penalty` | Deduct points for a named sin/slip (tied to `actions_master` negative entries) |
| `GET`  | `/actions` | Enumerate all actions grouped by `source_module` |
| `GET`  | `/actions/module/{module_name}` | Actions filtered by module |
| `GET`  | `/nafs_levels` | All 6 Nafs levels config |
| `GET`  | `/user/{user_id}/today_summary` | Actions logged today + points earned + streak |
| `GET`  | `/user/{user_id}/action_logs` | Paginated history of user actions |
| `GET`  | `/user/{user_id}/weekly_summary` | Weekly dimension deltas (structured, no AI) |
| `GET`  | `/user/{user_id}/onboarding_status` | Stage flags: profiler done, archetype set, initial scores persisted |
| `GET`  | `/health` | Liveness probe |

---

## Maqsad Engine (AI)

All Maqsad endpoints hit Anthropic (`claude-sonnet-4-6` by default). Free-text
user fields have Pydantic `Field(..., max_length=...)` caps to protect budget.

| Method | Path | Purpose | Rate |
|---|---|---|---|
| `POST` | `/maqsad/statement` | Generate a personalised Maqsad (purpose) statement from dimensions + profiler answers |  |
| `POST` | `/maqsad/mirror` | Quranic Mirror daily tafseer tailored to user's nafs + life stage. Returns `{muted: true}` early if user has set `quranic_mirror_muted` (PDF §15) |  |
| `POST` | `/maqsad/report` | Monthly Fitrah report (strengths, drift, recommended focus) |  |
| `POST` | `/maqsad/weekly_summary` | AI-narrated weekly summary (complements structured `/weekly_summary`) |  |
| `POST` | `/maqsad/nafs_message` | Level-transition message when user crosses a nafs threshold |  |
| `POST` | `/maqsad/streak_break` | Compassionate message when user's istiqamah streak breaks |  |
| `POST` | `/maqsad/drift_check` | Detect "purpose drift" from recent action patterns. Short-circuits to `{drift_detected: false, paused_until: …}` if user set a pause |  |
| `POST` | `/maqsad/drift_acknowledge` | User response to drift: `realign` / `reassess` / `conscious_choice`. `conscious_choice` pauses drift checks for 30 days (PDF §10) |  |
| `POST` | `/maqsad/habit_simulate` | Simulate dimension impact of a habit over 7/14/30/90 days |  |
| `POST` | `/maqsad/qadr` | Classify a life situation (Test/Training/Consequence/Warning/Elevation). Crisis-safe | **10/min** |
| `POST` | `/maqsad/life_test` | Classify a specific problem with spiritual context for Akhlaq AI chat. Crisis-safe | **10/min** |
| `POST` | `/maqsad/sunnah_dna_refresh` | Re-derive qualitative Sunnah DNA labels from numeric scores (weekly or post-onboarding) |  |

All six high-risk AI endpoints run user input through `check_crisis()` first.
On match, the AI call is skipped and a crisis-resource message
(`CRISIS_RESOURCE_TEXT` with Pakistani helplines + Quran 4:29) is returned
with `crisis: true`. AI output text is also passed through the PDF §23
6-layer pipeline (disclaimer, point-visibility, gamification, cap, state
confirmation, qadr-claim, + comparison filter).

---

## Nafs Battlefield + Qalb + Spiritual State

| Method | Path | Purpose | Rate |
|---|---|---|---|
| `POST` | `/battlefield/analyze` | AI identifies 4 forces (nafs / aql / qalb / shaytan) active right now, returns an intervention (ayah + hadith + micro-action). Awards `nafs_battlefield_session` +5 TAZKIYA | **10/min** |
| `POST` | `/qalb/log` | Log current Qalb state (7 states per PDF) with optional emotional-state tag and free-text note |  |
| `GET`  | `/user/{user_id}/qalb_history` | Last N days (default 7, max 30) of Qalb state logs |  |
| `GET`  | `/user/{user_id}/spiritual_state` | Derived state from recent actions (suggested vs confirmed) |  |
| `POST` | `/spiritual_state/confirm` | User confirms the AI-suggested spiritual state. Awards +3 TAZKIYA once per week (PDF §18) |  |

---

## Barakah Time sessions

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/barakah/session/start` | Start a focused-work session after niyyah confirmation |
| `POST` | `/barakah/session/complete` | Mark session complete, award barakah points by duration + dimension |
| `POST` | `/barakah/track` | Track one work unit within an active session |
| `GET`  | `/user/{user_id}/barakah_report` | Session history + barakah score trend |
| `GET`  | `/user/{user_id}/resilience` | Resilience score (consistency × recovery-from-breaks) |

---

## Onboarding + profiler

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/profiler/status` | Has user completed the profiler? |
| `GET`  | `/profiler/questions` | Full habit + nature question set (from `profiler_questions.json`) |
| `POST` | `/profiler/submit` | Submit answers → derive initial dimension scores, sunnah DNA, sahaba match, mizaj |
| `POST` | `/onboarding/complete` | Final step: set archetype, persist initial state, mark `profiler_completed_at` |

---

## Dua tracking

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/dua/add` | Record a personal dua (free-text, capped at 1000 chars) with optional context + fiqh tag |
| `GET`  | `/user/{user_id}/duas` | List user's duas (private by default) |
| `PATCH`| `/dua/{dua_id}/status` | Update dua status: `answered` / `closed_gracefully`. Closed-gracefully message is **PDF §16 compliant** — no ghayb claims |

---

## Kafarat + Ihtisab

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/kafarat/scenarios` | List the 7 kafarat scenarios from `fiqh_rulings_kafarat.json` |
| `POST` | `/kafarat/ask` | Query by scenario key or free-text question — returns madhab-specific rulings |
| `GET`  | `/ihtisab/weekly` | Weekly self-accounting snapshot |
| `GET`  | `/ihtisab/history` | Historical ihtisab entries |

---

## Balance, penalties, suggestions

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/balance/check` | Check cross-dimension balance — flag if any dimension lags significantly |
| `GET`  | `/user/{user_id}/deed_suggestions` | Recommended actions targeted at the user's weakest dimension |

---

## Islamic red lines

These are **mandatory** and enforced in code — not comments or docs:

| PDF § | Rule | Where enforced |
|---|---|---|
| §04 Rule 3 | Nafs level disclaimer must show on **both** up and down transitions | `/log_action` response includes `regression_disclaimer` + `pending_promotion.disclaimer_text` |
| §10 | Purpose drift offers 3 responses; `conscious_choice` pauses checks 30 days | `/maqsad/drift_acknowledge` → sets `drift_pause_until` |
| §15 | User can mute Quranic Mirror pushes (autonomy) | `/maqsad/mirror` short-circuits when `quranic_mirror_muted = true` |
| §16 | Dua "closed gracefully" may **not** claim knowledge of ghayb | `/dua/{id}/status` — message frames sabr/tawakkul, never "shayad kuch behtar aa raha hai" |
| §23 Safety A | Crisis keywords (suicide / self-harm, English + Urdu) override all output | `check_crisis()` runs on user input before AI call; returns helpline message |
| §23 Layer 1-6 | Disclaimer enforcer, point visibility, gamification blocker, cap preflight, spiritual-state confirmation, qadr-claim filter | `process_ai_response()` applied to free-text AI output on the 3 high-risk endpoints |

See [fitrah_middleware.py](fitrah_middleware.py) for the full pipeline.

---

## Background scheduler jobs

Run via APScheduler (`BackgroundScheduler`, UTC). Started from
[scheduler.py:999](scheduler.py#L999) — 12 jobs total:

| Job | Cadence | Purpose |
|---|---|---|
| Decay | daily 03:00 | Apply per-dimension decay from `decay_per_day` |
| Nafs promotion check | daily 04:00 | Evaluate pending promotions, flag mufti review |
| Spiritual-state suggestor | daily 05:00 | Suggest new state from recent actions |
| Barakah daily calc | daily 06:00 | Aggregate barakah-session points |
| Streak expiry | daily 07:00 | Break streaks when user missed a day |
| Ruhani fatigue | daily 08:00 | Detect excessive intensity — flag user |
| Weekly ihtisab | Sunday 09:00 | Generate weekly self-accounting rows |
| Sunnah DNA rederivation | weekly | Refresh numeric sunnah_dna |
| Purpose drift detection | daily 10:00 | **Skips users with `drift_pause_until >= CURRENT_DATE`** (PDF §10) |
| Qadr moment sweep | daily 11:00 | Surface life-test classifications |
| Monthly report flag | month-end | Mark users eligible for `/maqsad/report` |
| Qalb state pattern | daily 12:00 | Detect concerning qalb-state streaks |
| Dua thread reminder | daily 13:00 | Nudge users about open duas |
| Relationship pulse | daily 14:00 | Update `relationship_neglect_days` |

---

## Known issues

**Pre-existing config-schema drift** (not introduced by recent hardening work):

- `dimensions_config.json` uses `"key"` + `"max_single_day_gain"`; `scoring_logic.py` had been reading `"dimension_key"` + `"daily_max_gain"`. Now tolerant of both names
- `profiler_questions.json` is missing `ummah_role_mapping`, `mizaj_mapping`, `initial_score_calculation` top-level sections — `scoring_logic.py` now falls back to empty dicts, but `/profiler/submit` will return degraded output until the JSON is restored
- `maqsad_engine_prompts.json` uses `ai_model_config` / `ai_prompts`; `fitrah_routes.py` expects `api_config` / `prompts`. **This still blocks server startup** — needs the JSON re-aligned or a top-level shim

**Migrations required before deploying** the recent changes:

```bash
python -X utf8 seed_database.py
```

adds `drift_pause_until DATE` and `quranic_mirror_muted BOOLEAN NOT NULL DEFAULT FALSE` to `fitrah_users`.

**What to verify on first deploy**:

1. `/health` returns 200
2. `/user/{id}/profile` for an existing user returns the two new fields
3. `PATCH /user/settings` accepts `{"quranic_mirror_muted": true}` and persists
4. `POST /maqsad/qadr` with a crisis phrase (e.g. `"mar jaana chahta hoon"`) returns the helpline message, not an AI response
