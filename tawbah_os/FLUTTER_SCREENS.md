# Tawbah OS — Flutter Screen Specification

Complete screen-by-screen spec mapping every `/api/tawbah/*` endpoint to a Flutter UI surface. Designed for a production-ready module that plugs into the existing Barakah Flow Flutter app.

**Base URL:** `http://<host>:8000/api/tawbah`
**Suggested stack:** Flutter 3.x, Riverpod (state), Dio (HTTP), go_router (nav), freezed (models), hive_flutter (offline cache).

---

## 1. Information architecture

```
TawbahApp (module root)
├── SplashGate            ← checks onboarding + crisis flags
├── Onboarding (5 screens)
├── HomeDashboard         ← main hub after onboarding
│   ├── QuickTiles        ← shortcuts to engines
│   ├── ActiveSessionCard ← if session ENGINES_ACTIVE
│   ├── TodaysMuhasabaNudge
│   ├── StreakRibbon      ← Engine 4 top chapters
│   ├── SahabaCard        ← rotating daily snippet
│   └── EmergencyFAB      ← global, opens Crisis / Emergency modal
├── NewSessionWizard      ← TierDetect → GoalSelect → EngineLaunch
├── Engine screens (0–6, detailed below)
├── KaffaratDashboard
├── SettingsProfile
└── CrisisOverlay         ← global, mounts over any screen
```

### Navigation tree (go_router suggestion)

```
/                         → SplashGate
/onboarding/:step         → OnboardingScreen (1..5)
/home                     → HomeDashboard
/session/new              → NewSessionWizard
/session/:id              → SessionDetail

/engine/muhasaba          → MuhasabaHome
/engine/muhasaba/daily
/engine/muhasaba/weekly
/engine/muhasaba/patterns

/engine/aqal-nafs         → AqalNafsLogger

/engine/roadmap           → TawbahRoadmap
/engine/roadmap/:id/step  → RoadmapStepScreen
/engine/roadmap/:id/nishaniyaan

/engine/habit             → HabitBreakingHome
/engine/habit/emergency   → EmergencyModeScreen

/engine/istiqamah         → IstiqamahDashboard
/engine/istiqamah/:chapter

/engine/tahajjud          → TahajjudSession
/engine/tahajjud/:id/step/:step

/engine/kaffarat          → KaffaratDashboard
/engine/kaffarat/activate
/engine/kaffarat/istighfar
/engine/kaffarat/sadaqah
/engine/kaffarat/hasanah
/engine/kaffarat/musibat
/engine/kaffarat/dua-others
/engine/kaffarat/hajj-umrah
/engine/kaffarat/summary

/settings/profile
/safety/mental-health
```

---

## 2. API client skeleton

```dart
// lib/tawbah/data/api_client.dart
final dio = Dio(BaseOptions(
  baseUrl: const String.fromEnvironment('TAWBAH_API'),
  connectTimeout: const Duration(seconds: 20),
  headers: {'Content-Type': 'application/json'},
));

class TawbahApi {
  // Onboarding
  Future<Map> startOnboarding(String userId) =>
    dio.post('/onboarding/start', data: {'user_id': userId}).then((r) => r.data);

  Future<Map> saveProfile(Profile p) =>
    dio.post('/onboarding/profile', data: p.toJson()).then((r) => r.data);

  // Session
  Future<Session> createSession(String userId, {String entryType = 'normal'}) =>
    dio.post('/session/create', data: {'user_id': userId, 'entry_type': entryType})
       .then((r) => Session.fromJson(r.data));

  // ... one method per endpoint
}
```

---

## 3. SplashGate  (route: `/`)

**Purpose:** decide first screen on app open.

**Flow:**
1. Read `user_id` from secure storage.
2. `GET /onboarding/profile/{user_id}` → if 404, route to `/onboarding/1`.
3. If profile exists and `onboarded_at != null` → route to `/home`.
4. While loading, show branded spinner + ayah of the day from `crisis_safe_ayaat` cache.

---

## 4. Onboarding flow (5 screens)

Route: `/onboarding/:step`

### 4.1 Screen 1 — Welcome + niyyah framing
- **API on load:** `GET /onboarding/screens` — cache all 5 screen bodies.
- **Widgets:** illustration, screen title, niyyah text, "Start with Bismillah" CTA.
- **On tap CTA:** `POST /onboarding/start { user_id }` → navigate `/onboarding/2`.

### 4.2 Screen 2 — Fiqh school
- **Widget:** `RadioListTile` for each of: `hanafi`, `shafi`, `maliki`, `hanbali`, `ahle_hadith`.
- **On next:** store selection in provider; `POST /onboarding/advance { user_id, next_screen: 3 }`.

### 4.3 Screen 3 — Tone preference
- **Widget:** segmented control with previews of each tone's sample line.
- Options: `urdu_english_mix | urdu_formal | english_formal | hindi_english_mix | arabic_emphasized`.
- **On next:** `POST /onboarding/advance { next_screen: 4 }`.

### 4.4 Screen 4 — Country + tier preference
- **Country:** searchable country picker (ISO-2 code) → used later for helplines.
- **Tier pref (optional):** 3 cards "Light / Medium / Severe" with short description from spec.
- **On next:** `POST /onboarding/advance { next_screen: 5 }`.

### 4.5 Screen 5 — Privacy & consent
- **Content:** AES-256 on-device encryption explained (plain-language); user taps "I understand".
- **On accept:** `POST /onboarding/profile` with the full profile payload → server marks onboarding complete → navigate `/home`.

> **State:** wrap all 5 screens in a `ChangeNotifier`/`StateNotifier` holding the `ProfileDraft` so the user can `< Back` without losing fields.

---

## 5. HomeDashboard  (route: `/home`)

**Top-to-bottom layout:**

| Widget | Data source |
|--------|-------------|
| `QalbStatePromptBanner` | (Fitrah side — existing) |
| `ActiveSessionCard` | last non-closed `tawbah_sessions` (via future `GET /session/active/:user_id` or derive locally) |
| `EngineQuickTiles` grid | static — links to each engine home |
| `MuhasabaTile` | `GET /engine0/daily/questions` + local "logged today?" flag |
| `StreakRibbon` | `POST /engine4/streak` for each tracked `chapter_id` |
| `SahabaCard` | `GET /engine0/sahaba-snippet?rotation_index=<day_of_year % N>` |
| `WeeklyKaffaratSummary` | `GET /engine6/weekly-summary/:user_id` |
| `EmergencyFAB` | opens `EmergencyModeScreen` |

**AppBar actions:**
- Settings (gear) → `/settings/profile`
- Safety (heart icon) → `/safety/mental-health`

---

## 6. NewSessionWizard  (route: `/session/new`)

Bottom-sheet wizard, 3 steps:

### Step A — TierDetect
- **Text field** "Briefly describe kya ho raha hai" + **3 chips** (I feel this is: Light / Medium / Severe).
- **On submit:** `POST /tier/detect { user_id, self_selected, user_text }`.
- Display returned `{ tier, scores }` with a "why this tier?" info tap.

### Step B — GoalSelect
- Pick dominant goal (single-choice): `make_tawbah`, `break_bad_habit`, `start_kaffarah`, `tahajjud_session`, `aqal_nafs_coaching`, `weekly_muhasaba`.
- **On select:** `POST /session/create` → get `session_id` → `POST /session/transition { new_state: TIER_DETECTED, tier }` → `POST /session/transition { new_state: GOAL_SELECTED, goal_type }`.

### Step C — Launch
- `POST /session/transition { new_state: ENGINES_ACTIVE }`.
- Before navigation, call `POST /safety/crisis-scan` with the tier-detect text. If `crisis_detected: true`, open `CrisisOverlay` instead of the engine.
- Otherwise navigate to the engine route mapped from `goal_type`.

---

## 7. CrisisOverlay (global modal)

**Triggered by:** any crisis-scan response with `crisis_detected: true`, `do_not_proceed_with_engines: true`.

**Layout:**
- Dim scrim, non-dismissible (except via "Stay with me" button).
- Compassionate headline (from response `message_ur_en`).
- Large "Call helpline" button (tel: link using `helpline.phone`).
- Crisis-safe ayah card (Arabic + translation).
- Secondary: "Talk to a professional" → routes to `MentalHealthBridgeScreen` (`GET /safety/mental-health-bridge/:user_id`).
- Bottom: "Log how I'm coping" → opens safer `ExitPathwayForm` with `exit_type: mental_health_bridge`.

---

## 8. MentalHealthBridgeScreen (route: `/safety/mental-health`)

- On load: `GET /safety/mental-health-bridge/:user_id`.
- Two columns: **Islamic side** (dua + zikr) | **Professional side** (seek licensed therapist).
- Footer: "Dono raste saath chalte hain" reminder.

---

## 9. Engine 0 — Muhasaba

### 9.1 MuhasabaHome (`/engine/muhasaba`)
- Tabs: **Today** | **Weekly** | **Patterns** | **Heart alerts**.

### 9.2 DailyMuhasabaScreen (`/engine/muhasaba/daily`)
- **On load:** `GET /engine0/daily/questions` → render 4 question cards.
- 4 text areas (encrypt on device only if implementing client-side; server also encrypts).
- **Submit:** `POST /engine0/daily { user_id, q1, q2, q3, q4 }` → success animation + disable until tomorrow.

### 9.3 WeeklyDeepDiveScreen (`/engine/muhasaba/weekly`)
- **On load:** `GET /engine0/weekly/categories`.
- 4-tab category layout (Zuban / Nafs / Qalb / Amal), one text field per tab.
- **Submit:** `POST /engine0/weekly`.
- Gate: only show button on Fridays (or when 7+ days since last submit).

### 9.4 SinPatternsScreen (`/engine/muhasaba/patterns`)
- **List** from local cache of prior `POST /engine0/sin-pattern` submissions (plus any future `GET` for listing).
- Add-new FAB opens form → `POST /engine0/sin-pattern`.

### 9.5 HeartDiseaseSignalsScreen (inside tab)
- **On load:** `GET /engine0/heart-disease-signals`.
- Each disease (kibr/hasad/riya/hub-al-dunya/bukhl/ghadab/shahwat) card → tap → if user answers "I recognize this in myself" → `POST /engine0/heart-disease-handoff` → deep-link to Tibb-e-Nabawi module.

### 9.6 SahabaCard (home widget)
- Swipeable card via `GET /engine0/sahaba-snippet?rotation_index=N`.

---

## 10. Engine 1 — Aqal vs Nafs

### 10.1 AqalNafsLogger (`/engine/aqal-nafs`)
- **Header:** "Externalize the voices inside."
- Text field 1: "Kya urge aa rahi hai?" → `urge_text`.
- Two side-by-side cards:
  - **Nafs voice** (red tint): what is Nafs telling you?
  - **Aqal voice** (green tint): what is Aqal/Fitrah telling you?
- Bottom **3 chips**: `aqal_won` / `nafs_won` / `undecided`.
- **Submit:** `POST /engine1/log { user_id, session_id, urge_text, nafs_voice, aqal_voice, resolution }`.
- Post-submit: display a relevant ayah from `GET /engine1/config` → "Negotiation examples" section.

---

## 11. Engine 2 — Tawbah Roadmap

### 11.1 TawbahRoadmap (`/engine/roadmap`)
- **Step 0 pre-check:** `POST /engine2/tier3-detect { user_description }`.
  - If match → bottom-sheet: "Yeh Tier-3 case hai, Mufti se rujoo karein" + link to AI Mufti module. User can still proceed with a disclaimer checkbox.
- **Then:** `POST /engine2/start` → receive `roadmap_id` → navigate to step screen.

### 11.2 RoadmapStepScreen (`/engine/roadmap/:id/step`)
- Linear 3-step stepper: **Imsak** → **Nadim** → **Azm** (+ optional **Huquq-ul-Ibaad** side branch).
- Each step: description + reflection text field + "Complete this step" button → `POST /engine2/step`.
- UI locks future steps until current completes.

### 11.3 NishaniyaanScreen (`/engine/roadmap/:id/nishaniyaan`)
- Auto-navigated after Azm completes.
- **On load:** `POST /engine2/nishaniyaan { tone: <user pref> }`.
- Render mandatory disclaimer banner (top), then 6 Nishaniyaan cards, then cross-fiqh note (footer).
- **IMPORTANT UX:** never show "tawbah accepted" — the server-side qabooliyat-strip guarantees it, but also enforce in client copy.

---

## 12. Engine 3 — Habit Breaking

### 12.1 HabitBreakingHome (`/engine/habit`)
- Tabs: **Replace** | **Patterns** | **Relapse log** | **Emergency**.

### 12.2 ReplacementSuggester (tab 1)
- Input: "What's triggering you right now?" → `POST /engine3/find-replacement { trigger_text }`.
- Renders up to 3 replacement cards (keyword, Islamic alternative, dua, micro-action).

### 12.3 ShaytanPatternLogger (tab 2)
- Form fields: `trigger_time` (time-of-day chips: fajr/morning/afternoon/evening/late_night), `location`, `emotion`, `gunah_category`.
- **Submit:** `POST /engine3/shaytan-pattern`.
- **List:** shows past patterns; at top a "prediction chip" from `GET /engine3/predict-risk/:user_id` if `occurrences >= 3`.

### 12.4 RelapseLogger (tab 3)
- Form: `context`, optional `minutes_before_predicted`.
- **Submit:** `POST /engine3/relapse`.
- Non-judgmental wording — frame as "data, not failure."

### 12.5 EmergencyModeScreen (`/engine/habit/emergency`)
- **Triggered from EmergencyFAB anywhere in app.**
- **On load:** `GET /engine3/emergency-mode`.
- Full-screen dark UI: immediate 3 steps (wudu + change position + 2 rakat).
- 60-second timer with tasbeeh counter.
- Background plays `audio/dhikr.mp3` if user has audio allowed.

### 12.6 InternalDialogueCoachScreen (deep-link card in ReplacementSuggester)
- **On load:** `GET /engine3/internal-dialogue`.
- Card-deck UI: each card shows a distorted thought + Islamic correction; swipe to next.

### 12.7 BadHabitSubtypesBrowser (from Settings menu)
- **On load:** `GET /engine3/bad-habit-subtypes`.
- Read-only reference library.

---

## 13. Engine 4 — Istiqamah

### 13.1 IstiqamahDashboard (`/engine/istiqamah`)
- Grid of **chapter cards** — each card is one tracked practice (e.g. `fajr_sunnah`, `quran_daily`, `dhikr_morning`).
- For each, call `POST /engine4/streak { chapter_id }` and show `current_day_count`.
- FAB: "Add a chapter" (local — maintain chapter list in app).

### 13.2 ChapterDetailScreen (`/engine/istiqamah/:chapter`)
- Large streak number, max-streak pill, calendar heat-map of last 30 days.
- **Tick today button:** `POST /engine4/tick { user_id, chapter_id }`.
  - On response `milestone_hit != null` → open `MilestoneCelebrationModal` (confetti + Sahaba example).
- "I relapsed — reset" (secondary destructive button) → confirmation dialog → `POST /engine4/relapse-reset`.

### 13.3 RuhaniFatigueEvaluator (accessed from dashboard if ≥2 chapters broke streak)
- Checkbox list of signals (low_istighfar_7d, no_tahajjud_14d, …). User self-selects.
- **Evaluate:** `POST /engine4/fatigue/evaluate { active_signal_ids }`.
- If `fatigue_detected: true` → "Log this" button → `POST /engine4/fatigue/log` + display `prescription`.

---

## 14. Engine 5 — Spiritual Resurrection (Tahajjud)

### 14.1 TahajjudSession (`/engine/tahajjud`)
- Landing: "Sayyid-ul-Istighfar" card at top (`GET /engine5/sayyid-ul-istighfar`) — Arabic + promise.
- "Begin Tahajjud" button (best shown between 2–4am local time) → `POST /engine5/tahajjud/start` → returns `tahajjud_id` + step list.

### 14.2 TahajjudStepScreen (`/engine/tahajjud/:id/step/:step`)
- 5-step linear flow:
  - `step_1`: 2 rakat nafil checklist
  - `step_2`: long sajdah — timer + dua text
  - `step_3`: brief muhasaba textarea
  - `step_4`: Sayyid-ul-Istighfar recitation (Arabic + phonetic)
  - `step_5`: personal dua textarea
- After each step: `POST /engine5/tahajjud/step { tahajjud_id, step, reflection, user_id }`.
- Completion → celebration screen + link to `POST /engine5/sacred-line { context: "tahajjud_complete" }`.

### 14.3 DuaTherapyPlaceholderCard (disabled card in Tahajjud home)
- **On load:** `GET /engine5/dua-therapy/status`.
- If `available: false` → "Coming soon" badge + fallback to Sayyid-ul-Istighfar.

---

## 15. Engine 6 — Kaffarat-ul-Dhunub

### 15.1 KaffaratDashboard (`/engine/kaffarat`)
- **Top:** active activations list with progress ring (days elapsed / duration_days). If none, "Start Kaffarah" CTA.
- **Middle:** weekly qualitative summary card via `GET /engine6/weekly-summary/:user_id` — shows counts with mandatory disclaimer "Qabooliyat sirf Allah ke paas".
- **Bottom grid (7 tiles, one per action type):** Istighfar, Sadaqah, Hasanah, Musibat+Sabr, Dua for others, Hajj/Umrah intention.

### 15.2 ActivateKaffarahScreen (`/engine/kaffarat/activate`)
- Segmented control for `30 / 60 / 90` days.
- Optional text field `target_gunah` (client-side redaction reminder: "this is encrypted on-server").
- **Submit:** `POST /engine6/activate { user_id, session_id, duration_days, target_gunah? }`.

### 15.3 IstighfarCounterScreen (`/engine/kaffarat/istighfar`)
- Large tasbeeh counter (tap or volume-button to increment).
- Dropdown: `basic | sayyid_morning | sayyid_evening`.
- Auto-persist every 100 counts: `POST /engine6/istighfar { count: 100, type }`.

### 15.4 SadaqahLoggerScreen (`/engine/kaffarat/sadaqah`)
- Form: `amount`, `currency` (default from profile), `recipient_type` (masjid/orphan/poor/water/other), `niyyah`, optional `linked_gunah`, checkbox `is_jariyah`.
- **Submit:** `POST /engine6/sadaqah`.

### 15.5 HasanahLoggerScreen (`/engine/kaffarat/hasanah`)
- Category dropdown (silah_rahmi, birr_al_walidayn, feeding_others, etc.), description, niyyah.
- **Submit:** `POST /engine6/hasanah`.

### 15.6 MusibatSabrLoggerScreen (`/engine/kaffarat/musibat`)
- Form: `category` (financial/health/family/other), `sensitivity` (low/medium/high), `reflection`.
- **Submit:** `POST /engine6/musibat-sabr`.

### 15.7 DuaForOthersScreen (`/engine/kaffarat/dua-others`)
- Mode radio: `specific_person | general_ummah | specific_group`.
- If specific — optional target text field (encrypted server-side).
- **Submit:** `POST /engine6/dua-for-others`.

### 15.8 HajjUmrahIntentionScreen (`/engine/kaffarat/hajj-umrah`)
- Type dropdown: `hajj_planned | hajj_completed | umrah_planned | umrah_completed`.
- Year target picker, niyyah, optional reflection.
- **Submit:** `POST /engine6/hajj-umrah`.

### 15.9 WeeklyKaffaratSummaryScreen (`/engine/kaffarat/summary`)
- Pulls `GET /engine6/weekly-summary/:user_id`.
- Render 5 count cards (istighfar / sadaqah / hasanat / duas / sabr) + mandatory disclaimer banner.

---

## 16. Session detail & exit  (route: `/session/:id`)

### 16.1 SessionDetailScreen
- Shows `GET /session/:id` with state timeline.
- "Close session" menu with sub-options:
  - **Complete** → `POST /session/transition { new_state: COMPLETED }` + `POST /safety/exit-pathway { exit_type: completed }`.
  - **Pause** → `exit_type: paused`.
  - **Abandon** → confirmation dialog → `new_state: ABANDONED` + `exit_type: abandoned`.
  - **Handoff to Mufti/Tibb/MH** → `exit_type: mufti_handoff | tibb_handoff | mental_health_bridge`.

---

## 17. Settings  (route: `/settings/profile`)

- Editable fields backed by `GET /onboarding/profile/:user_id` + re-save via `POST /onboarding/profile`.
- Sections:
  - **Profile** (fiqh, tone, country, tier pref)
  - **Privacy** (AES-256 explainer, "export my data", "delete account" — both open support emails).
  - **Safety resources** (shortcut to `MentalHealthBridgeScreen`).
  - **About Tawbah OS** (version, disclaimer).

---

## 18. State management cheat-sheet (Riverpod)

```dart
// Global user session
final userIdProvider = StateProvider<String?>((_) => null);
final profileProvider = FutureProvider<Profile?>((ref) async {
  final uid = ref.watch(userIdProvider);
  if (uid == null) return null;
  return TawbahApi.I.getProfile(uid);
});

// Active Tawbah session
final activeSessionProvider = StateNotifierProvider<SessionNotifier, Session?>(
  (ref) => SessionNotifier(ref));

// Engine-specific providers
final streakProvider = FutureProvider.family<Streak, String>(
  (ref, chapterId) async {
    final uid = ref.watch(userIdProvider)!;
    return TawbahApi.I.getStreak(uid, chapterId);
  });

// Crisis flag (global — any screen reads this)
final crisisActiveProvider = StateProvider<CrisisPayload?>((_) => null);
```

Wrap `MaterialApp` with `CrisisOverlayHost` that watches `crisisActiveProvider` and paints the overlay above all routes when non-null.

---

## 19. Offline & sync policy

| Endpoint family | Offline behavior |
|-----------------|------------------|
| `GET` configs (`/onboarding/screens`, `/engine*/config`, `/engine0/*/questions`) | Cache in Hive at app launch; refresh once per day. |
| Logging endpoints (`/engine*/log`, `/engine6/*`) | Queue in Hive outbox; retry on reconnect with exponential backoff. |
| Session / crisis endpoints | **Never offline-queue** — always require live network. |
| Streak tick | Queue with `(chapter_id, tick_date)` — server is idempotent on same-day ticks. |

---

## 20. Theming & copy rules (enforced in UI)

1. **Never** render "tawbah accepted" / "forgiveness granted" / numeric "gunah erased". Client code should also scrub AI responses (defense-in-depth — server already does this via middleware layer 4).
2. **Encrypted field hint** on all free-text submissions: lock-icon tooltip "Encrypted on device + server (AES-256)".
3. **Tone-aware rendering:** wherever a backend response has `text_ur_en_mix / text_urdu_formal / ...`, pick the field matching the user's `tone_preference`.
4. **Disclaimer banners** (kaffarat, nishaniyaan) must be visually prominent — not collapsed accordions.
5. **No dark-pattern streaks:** if user relapses, `relapse-reset` UI must celebrate their honesty, not shame. Max-streak always preserved and displayed.

---

## 21. Endpoint → Screen map (quick reference)

| Endpoint | Screen |
|----------|--------|
| `GET /health` | — (dev only) |
| `GET /onboarding/screens` | Onboarding shell |
| `GET /onboarding/screen/{n}` | Onboarding screen `n` |
| `POST /onboarding/start` | Onboarding 1 |
| `POST /onboarding/advance` | Onboarding 2–4 |
| `POST /onboarding/profile` | Onboarding 5 / Settings |
| `GET /onboarding/profile/{uid}` | SplashGate / Settings |
| `POST /session/create` | NewSessionWizard step B |
| `GET /session/{id}` | SessionDetailScreen |
| `POST /session/transition` | NewSessionWizard + SessionDetail |
| `POST /tier/detect` | NewSessionWizard step A |
| `POST /safety/crisis-scan` | NewSessionWizard pre-launch + any text input |
| `GET /safety/mental-health-bridge/{uid}` | MentalHealthBridgeScreen |
| `POST /safety/exit-pathway` | SessionDetail close menu |
| `POST /middleware/process` | (Backend AI wrapper — not usually called from client) |
| `GET /engine0/daily/questions` | DailyMuhasabaScreen |
| `GET /engine0/weekly/categories` | WeeklyDeepDiveScreen |
| `POST /engine0/daily` | DailyMuhasabaScreen submit |
| `POST /engine0/weekly` | WeeklyDeepDiveScreen submit |
| `POST /engine0/sin-pattern` | SinPatternsScreen |
| `POST /engine0/heart-disease-handoff` | HeartDiseaseSignalsScreen |
| `GET /engine0/sahaba-snippet` | SahabaCard (home) |
| `GET /engine0/heart-disease-signals` | HeartDiseaseSignalsScreen |
| `GET /engine1/config` | AqalNafsLogger (help sheet) |
| `POST /engine1/log` | AqalNafsLogger submit |
| `POST /engine2/start` | TawbahRoadmap launcher |
| `POST /engine2/step` | RoadmapStepScreen |
| `POST /engine2/tier3-detect` | TawbahRoadmap pre-check |
| `POST /engine2/nishaniyaan` | NishaniyaanScreen |
| `POST /engine3/find-replacement` | ReplacementSuggester |
| `POST /engine3/shaytan-pattern` | ShaytanPatternLogger |
| `POST /engine3/relapse` | RelapseLogger |
| `GET /engine3/predict-risk/{uid}` | ShaytanPatternLogger header chip |
| `GET /engine3/bad-habit-subtypes` | BadHabitSubtypesBrowser |
| `GET /engine3/internal-dialogue` | InternalDialogueCoachScreen |
| `GET /engine3/emergency-mode` | EmergencyModeScreen |
| `POST /engine4/streak` | IstiqamahDashboard + ChapterDetail |
| `POST /engine4/tick` | ChapterDetailScreen |
| `POST /engine4/relapse-reset` | ChapterDetailScreen |
| `POST /engine4/fatigue/evaluate` | RuhaniFatigueEvaluator |
| `POST /engine4/fatigue/log` | RuhaniFatigueEvaluator |
| `POST /engine5/tahajjud/start` | TahajjudSession launcher |
| `POST /engine5/tahajjud/step` | TahajjudStepScreen |
| `GET /engine5/sayyid-ul-istighfar` | TahajjudSession header |
| `POST /engine5/sacred-line` | TahajjudStepScreen (after step 5) |
| `GET /engine5/dua-therapy/status` | DuaTherapyPlaceholderCard |
| `POST /engine6/activate` | ActivateKaffarahScreen |
| `POST /engine6/istighfar` | IstighfarCounterScreen |
| `POST /engine6/sadaqah` | SadaqahLoggerScreen |
| `POST /engine6/hasanah` | HasanahLoggerScreen |
| `POST /engine6/musibat-sabr` | MusibatSabrLoggerScreen |
| `POST /engine6/dua-for-others` | DuaForOthersScreen |
| `POST /engine6/hajj-umrah` | HajjUmrahIntentionScreen |
| `GET /engine6/weekly-summary/{uid}` | KaffaratDashboard + WeeklyKaffaratSummaryScreen |
| `GET /engine6/config` | KaffaratDashboard (help sheet) |

---

## 22. MVP phasing

If you're shipping incrementally:

**Phase 1 — Core loop (ship first):**
1. Onboarding (5 screens) → 2. HomeDashboard → 3. NewSessionWizard → 4. TawbahRoadmap (Engine 2) → 5. NishaniyaanScreen → 6. CrisisOverlay.

**Phase 2 — Daily rituals:**
7. Engine 0 Muhasaba → 8. Engine 4 Istiqamah → 9. Engine 6 Istighfar counter + Sadaqah.

**Phase 3 — Deep tools:**
10. Engine 3 Habit Breaking + Emergency Mode → 11. Engine 1 Aqal vs Nafs → 12. Engine 5 Tahajjud.

**Phase 4 — Completeness:**
13. Remaining Engine 6 loggers → 14. Settings + MentalHealthBridge → 15. Ruhani Fatigue evaluator.

---

## 23. Testing hooks

- Provide a `/dev/reset_user` hidden gesture (triple-tap app version in Settings) that clears local storage and restarts onboarding — do **not** ship in production build.
- Add a debug overlay toggle that paints endpoint names on each screen for QA.
- Golden tests: render each screen at LTR (English) and RTL (Arabic/Urdu) + each of the 5 tones.
