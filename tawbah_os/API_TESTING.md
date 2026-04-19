# Tawbah OS — API Testing Guide

All endpoints are mounted under `/api/tawbah`.

**Base URL (local):** `http://localhost:8000/api/tawbah`
**Swagger UI:** `http://localhost:8000/docs` → filter by tag `Tawbah OS`

Before running tests:
1. Set `TAWBAH_MASTER_KEY` in `.env` (32+ random chars) — required for AES-256 encryption.
2. `uvicorn main:app --reload --port 8000`
3. Use any `user_id` string (UUID recommended). Example used below: `test_user_001`.

> Tip: Windows PowerShell users — wrap JSON bodies in single quotes and escape inner quotes, or save payloads to a `.json` file and use `curl -d @payload.json`.

---

## 0. Health

```bash
curl http://localhost:8000/api/tawbah/health
```

**200** `{ "status": "ok", "module": "tawbah_os" }`

---

## 1. Onboarding (5-screen flow)

### 1.1 List all onboarding screens
```bash
curl http://localhost:8000/api/tawbah/onboarding/screens
```

### 1.2 Fetch one screen
```bash
curl http://localhost:8000/api/tawbah/onboarding/screen/1
```

### 1.3 Start onboarding
```bash
curl -X POST http://localhost:8000/api/tawbah/onboarding/start \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001"}'
```

### 1.4 Advance to next screen
```bash
curl -X POST http://localhost:8000/api/tawbah/onboarding/advance \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001","next_screen":2}'
```

### 1.5 Save profile (fiqh + tone + country + tier) — also marks onboarding complete
```bash
curl -X POST http://localhost:8000/api/tawbah/onboarding/profile \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "fiqh_school":"hanafi",
    "tone_preference":"urdu_english_mix",
    "country_code":"PK",
    "tier_preference":"medium"
  }'
```

Valid enum values:
- `fiqh_school`: `hanafi | shafi | maliki | hanbali | ahle_hadith`
- `tone_preference`: `urdu_english_mix | urdu_formal | english_formal | hindi_english_mix | arabic_emphasized`
- `tier_preference`: `light | medium | severe` (optional)

### 1.6 Get profile
```bash
curl http://localhost:8000/api/tawbah/onboarding/profile/test_user_001
```

---

## 2. Session state machine

State transitions:
`NEW_SESSION → TIER_DETECTED → GOAL_SELECTED → ENGINES_ACTIVE → COMPLETED | ABANDONED`

### 2.1 Create session
```bash
curl -X POST http://localhost:8000/api/tawbah/session/create \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001","entry_type":"normal"}'
```
Returns `{ "id": <session_id>, ... }` — save this id.

### 2.2 Get session
```bash
curl http://localhost:8000/api/tawbah/session/1
```

### 2.3 Transition
```bash
curl -X POST http://localhost:8000/api/tawbah/session/transition \
  -H "Content-Type: application/json" \
  -d '{
    "session_id":1,
    "new_state":"TIER_DETECTED",
    "tier":"medium"
  }'
```

Then:
```bash
curl -X POST http://localhost:8000/api/tawbah/session/transition \
  -H "Content-Type: application/json" \
  -d '{"session_id":1,"new_state":"GOAL_SELECTED","goal_type":"break_bad_habit"}'
```
```bash
curl -X POST http://localhost:8000/api/tawbah/session/transition \
  -H "Content-Type: application/json" \
  -d '{"session_id":1,"new_state":"ENGINES_ACTIVE"}'
```

---

## 3. Tier detection

```bash
curl -X POST http://localhost:8000/api/tawbah/tier/detect \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "self_selected":"medium",
    "user_text":"main baar baar phir se wohi gunah karta hoon"
  }'
```
Returns `{ "tier": "...", "scores": {...}, "nlp_inferred": "...", "historical": "..." }`.
Downgrade disallowed — if historical is severe, result never drops below it.

---

## 4. Safety / Middleware

### 4.1 Crisis scan — call before any engine invocation
```bash
curl -X POST http://localhost:8000/api/tawbah/safety/crisis-scan \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "user_text":"I feel hopeless",
    "country_code":"PK"
  }'
```
If crisis detected, returns helpline + crisis-safe ayah + `do_not_proceed_with_engines: true`.

### 4.2 Mental health bridge
```bash
curl http://localhost:8000/api/tawbah/safety/mental-health-bridge/test_user_001
```

### 4.3 Log exit pathway
```bash
curl -X POST http://localhost:8000/api/tawbah/safety/exit-pathway \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "exit_type":"paused",
    "session_id":1,
    "notes":"User needed break"
  }'
```
Valid `exit_type`: `completed | abandoned | paused | mufti_handoff | tibb_handoff | mental_health_bridge`

### 4.4 Middleware process (layer 4 — strip qabooliyat claims from AI output)
```bash
curl -X POST http://localhost:8000/api/tawbah/middleware/process \
  -H "Content-Type: application/json" \
  -d '{
    "ai_text":"Allah has forgiven you — tawbah accepted",
    "user_text":"I committed a sin",
    "tier":"medium",
    "engine_id":"engine_2"
  }'
```
Returns `{ "text": "<cleaned>", "flags": { "qabooliyat_stripped": true, ... } }`.

---

## 5. Engine 0 — Muhasaba

### 5.1 Get daily 4-question template
```bash
curl http://localhost:8000/api/tawbah/engine0/daily/questions
```

### 5.2 Get weekly deep-dive categories + questions
```bash
curl http://localhost:8000/api/tawbah/engine0/weekly/categories
```

### 5.3 Log daily muhasaba (4 encrypted answers)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine0/daily \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "q1":"Aaj Fajr qaza ho gayi",
    "q2":"Kuch der ghussa aaya",
    "q3":"Ek bar jhoot bola",
    "q4":"Kal Fajr time pe uthna hai"
  }'
```

### 5.4 Log weekly deep-dive (4 categories: zuban/nafs/qalb/amal)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine0/weekly \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "zuban":"Ghibat kam ki is hafte",
    "nafs":"Khwahishaat control mein rahin",
    "qalb":"Zikr thoda badhaya",
    "amal":"Sunnahs par amal behtar hua"
  }'
```

### 5.5 Log sin pattern observation
```bash
curl -X POST http://localhost:8000/api/tawbah/engine0/sin-pattern \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "pattern_type":"late_night_phone",
    "signal_count":5,
    "description":"Sleep delay 5 nights"
  }'
```

### 5.6 Log heart-disease handoff (awareness only — routes to Tibb-e-Nabawi)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine0/heart-disease-handoff \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "disease":"kibr",
    "signals_count":3,
    "user_response":"accepted"
  }'
```

### 5.7 Rotating Sahaba snippet
```bash
curl "http://localhost:8000/api/tawbah/engine0/sahaba-snippet?rotation_index=0"
```

### 5.8 Heart-disease signal triggers
```bash
curl http://localhost:8000/api/tawbah/engine0/heart-disease-signals
```

---

## 6. Engine 1 — Aqal vs Nafs Negotiation

### 6.1 Get config
```bash
curl http://localhost:8000/api/tawbah/engine1/config
```

### 6.2 Log a negotiation
```bash
curl -X POST http://localhost:8000/api/tawbah/engine1/log \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "session_id":1,
    "urge_text":"Phone check karna hai 2am pe",
    "nafs_voice":"Sirf 5 min, kuch nahi hoga",
    "aqal_voice":"Fajr miss ho jayegi, 5 min = 50 min",
    "resolution":"aqal_won"
  }'
```

---

## 7. Engine 2 — Tawbah Roadmap (Imsak → Nadim → Azm)

### 7.1 Detect Tier-3 case (routes to Mufti if matched)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine2/tier3-detect \
  -H "Content-Type: application/json" \
  -d '{"user_description":"Main ne zina kiya aur pregnancy hai"}'
```

### 7.2 Start roadmap
```bash
curl -X POST http://localhost:8000/api/tawbah/engine2/start \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "session_id":1,
    "gunah_description":"Late-night internet addiction",
    "requires_huquq":false
  }'
```
Returns `{ "roadmap_id": N }`.

### 7.3 Complete each step (must run in order: imsak → nadim → azm)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine2/step \
  -H "Content-Type: application/json" \
  -d '{"roadmap_id":1,"user_id":"test_user_001","step":"imsak","reflection":"Phone doosre kamre mein rakhna shuru kiya"}'
```
```bash
curl -X POST http://localhost:8000/api/tawbah/engine2/step \
  -H "Content-Type: application/json" \
  -d '{"roadmap_id":1,"user_id":"test_user_001","step":"nadim","reflection":"Itna waqt zaaya kar diya"}'
```
```bash
curl -X POST http://localhost:8000/api/tawbah/engine2/step \
  -H "Content-Type: application/json" \
  -d '{"roadmap_id":1,"user_id":"test_user_001","step":"azm","reflection":"Dobara kabhi nahi"}'
```

### 7.4 Get Tawbah Nishaniyaan + mandatory disclaimer (post-Azm display)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine2/nishaniyaan \
  -H "Content-Type: application/json" \
  -d '{"tone":"urdu_english_mix"}'
```

---

## 8. Engine 3 — Habit Breaking

### 8.1 Find Islamic replacements for a trigger
```bash
curl -X POST http://localhost:8000/api/tawbah/engine3/find-replacement \
  -H "Content-Type: application/json" \
  -d '{"trigger_text":"urge aa rahi hai scroll karne ki"}'
```

### 8.2 Log a Shaytan pattern
```bash
curl -X POST http://localhost:8000/api/tawbah/engine3/shaytan-pattern \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "trigger_time":"late_night",
    "location":"bedroom",
    "emotion":"boredom",
    "gunah_category":"time_waste"
  }'
```

### 8.3 Log a relapse
```bash
curl -X POST http://localhost:8000/api/tawbah/engine3/relapse \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "session_id":1,
    "context":"Late night stress eating",
    "minutes_before_predicted":12
  }'
```

### 8.4 Predict next high-risk window
```bash
curl http://localhost:8000/api/tawbah/engine3/predict-risk/test_user_001
```

### 8.5 Bad habit subtypes catalogue
```bash
curl http://localhost:8000/api/tawbah/engine3/bad-habit-subtypes
```

### 8.6 Internal dialogue corrections (self-talk CBT-style)
```bash
curl http://localhost:8000/api/tawbah/engine3/internal-dialogue
```

### 8.7 Emergency mode (crisis interrupt)
```bash
curl http://localhost:8000/api/tawbah/engine3/emergency-mode
```

---

## 9. Engine 4 — Istiqamah Tracker + Ruhani Fatigue

### 9.1 Get chapter streak
```bash
curl -X POST http://localhost:8000/api/tawbah/engine4/streak \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001","chapter_id":"fajr_sunnah"}'
```

### 9.2 Mark today active (tick)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine4/tick \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001","chapter_id":"fajr_sunnah"}'
```

### 9.3 Reset after relapse (preserves `max_streak_achieved`)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine4/relapse-reset \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001","chapter_id":"fajr_sunnah"}'
```

### 9.4 Evaluate Ruhani Fatigue (weighted sum — triggers at 3+ signals AND weight ≥ 0.65)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine4/fatigue/evaluate \
  -H "Content-Type: application/json" \
  -d '{"active_signal_ids":["low_istighfar_7d","no_tahajjud_14d","repeated_relapse_7d"]}'
```

### 9.5 Log fatigue detection
```bash
curl -X POST http://localhost:8000/api/tawbah/engine4/fatigue/log \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "active_signals":["low_istighfar_7d","no_tahajjud_14d","repeated_relapse_7d"],
    "composite_weight":0.78
  }'
```

---

## 10. Engine 5 — Spiritual Resurrection (Tahajjud)

### 10.1 Start a Tahajjud session
```bash
curl -X POST http://localhost:8000/api/tawbah/engine5/tahajjud/start \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001","session_id":1}'
```
Returns `{ "tahajjud_id": N, "steps": [...] }`.

### 10.2 Complete each of 5 steps (step_1 → step_5)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine5/tahajjud/step \
  -H "Content-Type: application/json" \
  -d '{"tahajjud_id":1,"user_id":"test_user_001","step":"step_1","reflection":"2 rakat nafil ada kar li"}'
```
Repeat for `step_2` (Sajdah dua), `step_3` (brief muhasaba), `step_4` (Sayyid-ul-Istighfar), `step_5` (personal dua).

### 10.3 Get Sayyid-ul-Istighfar (Bukhari 6306)
```bash
curl http://localhost:8000/api/tawbah/engine5/sayyid-ul-istighfar
```

### 10.4 Get context-mapped sacred line
```bash
curl -X POST http://localhost:8000/api/tawbah/engine5/sacred-line \
  -H "Content-Type: application/json" \
  -d '{"context":"post_azm_completion"}'
```

### 10.5 Dua Therapy status (on hold — returns graceful placeholder)
```bash
curl http://localhost:8000/api/tawbah/engine5/dua-therapy/status
```

---

## 11. Engine 6 — Kaffarat-ul-Dhunub

### 11.1 Activate kaffarah (30/60/90 days only)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine6/activate \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "session_id":1,
    "duration_days":60,
    "target_gunah":"Time-waste on social media"
  }'
```

### 11.2 Log istighfar
```bash
curl -X POST http://localhost:8000/api/tawbah/engine6/istighfar \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001","count":100,"type":"basic"}'
```
Valid `type`: `basic | sayyid_morning | sayyid_evening`

### 11.3 Log sadaqah
```bash
curl -X POST http://localhost:8000/api/tawbah/engine6/sadaqah \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "amount":"500",
    "currency":"PKR",
    "recipient_type":"masjid",
    "niyyah":"Kaffarah of time-waste",
    "linked_gunah":"Late-night scrolling",
    "is_jariyah":false
  }'
```

### 11.4 Log a hasanah (good deed)
```bash
curl -X POST http://localhost:8000/api/tawbah/engine6/hasanah \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "category":"silah_rahmi",
    "description":"Called my parents after 2 weeks",
    "niyyah":"For Allah"
  }'
```

### 11.5 Log musibat + sabr
```bash
curl -X POST http://localhost:8000/api/tawbah/engine6/musibat-sabr \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "category":"financial_stress",
    "sensitivity":"medium",
    "reflection":"Allah ki taraf se imtihan — sabar kar raha hoon"
  }'
```

### 11.6 Log dua for others
```bash
curl -X POST http://localhost:8000/api/tawbah/engine6/dua-for-others \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test_user_001","mode":"general_ummah"}'
```
Valid `mode`: `specific_person | general_ummah | specific_group`

### 11.7 Log Hajj/Umrah intention
```bash
curl -X POST http://localhost:8000/api/tawbah/engine6/hajj-umrah \
  -H "Content-Type: application/json" \
  -d '{
    "user_id":"test_user_001",
    "type":"umrah_planned",
    "year_target":2027,
    "niyyah":"Kaffarah ke taur par",
    "reflection":"Saving monthly"
  }'
```
Valid `type`: `hajj_planned | hajj_completed | umrah_planned | umrah_completed`

### 11.8 Weekly qualitative summary (NO numeric "gunah erased" claim)
```bash
curl http://localhost:8000/api/tawbah/engine6/weekly-summary/test_user_001
```

### 11.9 Kaffarah config
```bash
curl http://localhost:8000/api/tawbah/engine6/config
```

---

## 12. End-to-end smoke test (recommended flow)

```bash
# 1. Onboard
curl -X POST http://localhost:8000/api/tawbah/onboarding/start -H "Content-Type: application/json" -d '{"user_id":"smoke_001"}'
curl -X POST http://localhost:8000/api/tawbah/onboarding/profile -H "Content-Type: application/json" -d '{"user_id":"smoke_001","fiqh_school":"hanafi","tone_preference":"urdu_english_mix","country_code":"PK","tier_preference":"medium"}'

# 2. Session
curl -X POST http://localhost:8000/api/tawbah/session/create -H "Content-Type: application/json" -d '{"user_id":"smoke_001"}'
# → note session_id (assume 2)

curl -X POST http://localhost:8000/api/tawbah/session/transition -H "Content-Type: application/json" -d '{"session_id":2,"new_state":"TIER_DETECTED","tier":"medium"}'
curl -X POST http://localhost:8000/api/tawbah/session/transition -H "Content-Type: application/json" -d '{"session_id":2,"new_state":"GOAL_SELECTED","goal_type":"bad_habit"}'
curl -X POST http://localhost:8000/api/tawbah/session/transition -H "Content-Type: application/json" -d '{"session_id":2,"new_state":"ENGINES_ACTIVE"}'

# 3. Crisis gate
curl -X POST http://localhost:8000/api/tawbah/safety/crisis-scan -H "Content-Type: application/json" -d '{"user_id":"smoke_001","user_text":"ek galti ho gayi","country_code":"PK"}'

# 4. Roadmap (imsak → nadim → azm)
curl -X POST http://localhost:8000/api/tawbah/engine2/start -H "Content-Type: application/json" -d '{"user_id":"smoke_001","session_id":2,"gunah_description":"time-waste"}'
# → roadmap_id (assume 1)
curl -X POST http://localhost:8000/api/tawbah/engine2/step -H "Content-Type: application/json" -d '{"roadmap_id":1,"user_id":"smoke_001","step":"imsak","reflection":"stopped"}'
curl -X POST http://localhost:8000/api/tawbah/engine2/step -H "Content-Type: application/json" -d '{"roadmap_id":1,"user_id":"smoke_001","step":"nadim","reflection":"regret"}'
curl -X POST http://localhost:8000/api/tawbah/engine2/step -H "Content-Type: application/json" -d '{"roadmap_id":1,"user_id":"smoke_001","step":"azm","reflection":"resolve"}'

# 5. Post-azm display
curl -X POST http://localhost:8000/api/tawbah/engine2/nishaniyaan -H "Content-Type: application/json" -d '{"tone":"urdu_english_mix"}'

# 6. Log istighfar + sadaqah (kaffarat stack)
curl -X POST http://localhost:8000/api/tawbah/engine6/istighfar -H "Content-Type: application/json" -d '{"user_id":"smoke_001","count":100,"type":"basic"}'

# 7. Close session
curl -X POST http://localhost:8000/api/tawbah/session/transition -H "Content-Type: application/json" -d '{"session_id":2,"new_state":"COMPLETED"}'
curl -X POST http://localhost:8000/api/tawbah/safety/exit-pathway -H "Content-Type: application/json" -d '{"user_id":"smoke_001","session_id":2,"exit_type":"completed","notes":"tawbah flow complete"}'
```

---

## 13. Common errors

| Status | Meaning |
|--------|---------|
| `400` | Validation error (invalid enum, bad transition, unknown step) |
| `404` | Profile / session not found |
| `422` | Pydantic body validation failed (missing required field) |
| `500` | DB connection or encryption key missing — check `TAWBAH_MASTER_KEY` and Postgres at `localhost:5433` |

---

## 14. Database inspection (read-only checks)

```sql
-- Which tables were written to?
SELECT schemaname, relname, n_tup_ins
FROM pg_stat_user_tables
WHERE relname LIKE 'tawbah_%'
ORDER BY n_tup_ins DESC;

-- All configs persisted
SELECT config_key, updated_at FROM tawbah_system_configs ORDER BY config_key;

-- Recent sessions
SELECT id, user_id, state, tier, goal_type, started_at, closed_at
FROM tawbah_sessions ORDER BY started_at DESC LIMIT 10;

-- Encrypted columns are BYTEA — decryption happens only in Python via encryption.decrypt()
```
