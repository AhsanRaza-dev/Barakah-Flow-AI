# 🤖 Barakah Flow RAG Backend — Implementation Guide

> **Hybrid Architecture:** Supabase (Auth + User Data) + Dedicated VPS (FastAPI + pgvector)

**Last Updated:** March 13, 2026  
**Architecture Decision:** Hybrid (confirmed)

---

## 🏗 Architecture Overview

```
┌─ Flutter App ─────────────────────────────────┐
│  • Supabase JWT for auth (registered + guest) │
│  • API key + JWT for RAG server requests      │
└───────────────┬───────────────┬───────────────┘
                ↓               ↓
┌─ Supabase ──────────┐  ┌─ VPS (FastAPI) ──────────────┐
│ • Auth (Email,       │  │ • FastAPI RAG endpoint       │
│   Google, Anonymous) │  │ • PostgreSQL + pgvector      │
│ • User profiles      │  │ • OpenAI Multilingual        │
│ • ai_usage table     │  │   Embeddings (API)           │
│ • Bookmarks          │  │ • Gemini Flash (LLM)         │
│ • Conversation hist. │  │ • Nginx + SSL (Let's Encrypt)│
│ • RLS policies       │  │ • JWT validation middleware  │
│ • Auto backups       │  │ • Rate limiting              │
└──────────────────────┘  └──────────────────────────────┘
```

### Why Hybrid?

| Factor | Why It Wins |
|--------|-------------|
| **Cost** | ~$12-18/mo total (Supabase Free + VPS $5-8 + APIs) |
| **No migration** | Flutter already uses Supabase auth |
| **Unlimited RAG storage** | VPS disk, not pricing tiers |
| **OpenAI Embeddings** | Lighter VPS (no local model needed), 2GB RAM sufficient |
| **Sacred data privacy** | Quran/Hadith/Fiqh texts stay self-hosted |
| **Python native** | Full FastAPI + sentence-transformers ecosystem |

---

## 🛠 Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| **Web Framework** | FastAPI + Uvicorn | Async Python REST API |
| **Vector Database** | PostgreSQL + pgvector | On VPS, unlimited storage |
| **Embedding Model** | OpenAI `text-embedding-3-small` | Multilingual (Arabic/English/Urdu), 1536 dims |
| **LLM** | Google Gemini 1.5 Flash | Low cost, fast responses |
| **Auth Validation** | Supabase JWT (via PyJWT) | Validates tokens from Flutter app |
| **Rate Limiting** | Supabase `ai_usage` table | Guest: 2 prompts, Registered: unlimited |
| **Reverse Proxy** | Nginx + Let's Encrypt | HTTPS, rate limiting, security headers |
| **Deployment** | Hetzner/Contabo/Oracle Free VPS | 2+ GB RAM, 80GB+ SSD |

---

## 📂 Knowledge Base Data Sources

| Source | Size | Status | Description |
|--------|------|--------|-------------|
| Quran (Arabic + English + tags) | ~25 MB | ✅ Ready | Multiple enriched JSON versions |
| Hadith — 6 major collections (AR/EN/UR) | ~110 MB | ✅ Ready | Bukhari, Muslim, Abu Dawud, Tirmidhi, Nasai, Ibn Majah |
| Fiqh Books (4 madhahib) | ~200 MB+ | 🔄 In Progress | Hanafi, Shafi'i, Maliki, Hanbali texts from OpenITI |
| Fatawa | ~7 MB | 🔄 In Progress | Contemporary rulings with mufti attribution |
| Athar (Companion narrations) | ~6 KB+ | 🔄 In Progress | Expanding corpus |
| Tafsir | Planned | ⬜ Not Started | Quran commentary texts |
| **Total (raw text)** | **~500 MB → 2GB+** | | **Growing** |
| **With embeddings (1536-dim)** | **~2-8 GB** | | **Vectors add ~3-4x raw size** |

---

## 🔐 Guest User Rate Limiting

### Flow

```
Guest opens app
    ↓
Supabase Anonymous Sign-In (silent, automatic)
    → Generates unique anonymous UUID + JWT
    ↓
Guest hits "Ask AI"
    ↓
Flutter sends POST /api/ask with Supabase JWT
    ↓
FastAPI middleware:
    1. Validate JWT (Supabase JWKS)
    2. Decode: is_anonymous from JWT claims
    3. If anonymous → query Supabase ai_usage table
    4. If prompt_count >= 2 → HTTP 403 "Sign up to continue"
    5. If prompt_count < 2 → process RAG, increment count
    ↓
Return AI response with citations
```

### Supabase Tables Required

```sql
-- Table: ai_usage (tracks prompt limits per user)
CREATE TABLE ai_usage (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  prompt_count INT DEFAULT 0,
  last_prompt_at TIMESTAMPTZ DEFAULT now(),
  is_anonymous BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- RLS: Users can only read/update their own usage
ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users read own usage"
  ON ai_usage FOR SELECT
  USING (auth.uid() = user_id);

CREATE POLICY "Users update own usage"
  ON ai_usage FOR UPDATE
  USING (auth.uid() = user_id);

-- Auto-create usage row on signup (via Supabase trigger or Edge Function)
CREATE POLICY "Users insert own usage"
  ON ai_usage FOR INSERT
  WITH CHECK (auth.uid() = user_id);
```

### Tier Limits

| User Type | Prompt Limit | Storage | Features |
|-----------|-------------|---------|----------|
| **Guest (anonymous)** | 2 prompts total | None | Basic AI only |
| **Free (registered)** | 20 prompts/day | Bookmarks, history | Full AI + citations |
| **Premium (future)** | Unlimited | Everything | Priority responses |

---

## 🧠 RAG Pipeline Architecture

### Step 1: Data Ingestion

```
Raw JSON files (Quran, Hadith, Fiqh, Fatwa, Athar)
    ↓
Python ingestion script (scripts/ingest_knowledge_base.py)
    ↓
For each text chunk:
    1. Generate embedding via OpenAI text-embedding-3-small API
    2. Tag with source_type, fiqh, authority metadata
    3. INSERT into knowledge_base table (PostgreSQL + pgvector)
```

### Step 2: Vector Search (on query)

```
User question → OpenAI embedding → pgvector cosine similarity search
    ↓
Pre-filter by:
    - source_type (quran/hadith/fatwa)
    - fiqh (general/hanafi/shafi/maliki/hanbali)
    - user's madhab preference (from Supabase profile)
    ↓
Return top 5-10 relevant chunks with metadata
```

### Step 3: LLM Response Generation

```
Retrieved chunks + user question → Gemini Flash prompt
    ↓
System prompt enforces:
    - "Reporter" persona (no AI Ijtihad)
    - Strict citation with [Source ID: ...] format
    - Fatwa attribution to specific Mufti/Institution
    - Dual structure: general ruling + user's fiqh-specific
    ↓
Response with clickable citations → Flutter app
```

---

## 📁 Project Structure

```
RAG/
├── .env                          # API keys (NEVER commit)
├── requirements.txt              # Python dependencies
├── app/
│   ├── __init__.py
│   ├── config.py                 # Settings from env vars
│   ├── main.py                   # FastAPI app + endpoints
│   ├── middleware/
│   │   ├── auth.py               # Supabase JWT validation
│   │   └── rate_limiter.py       # Guest prompt counting
│   └── services/
│       ├── search_service.py     # pgvector search logic
│       └── rag_service.py        # LLM prompt + citation extraction
├── scripts/
│   ├── ingest_knowledge_base.py  # Bulk data ingestion
│   └── quran.py                  # Quran-specific processing
└── data/
    ├── quran/                    # Quran JSON files
    └── hadith/                   # Hadith JSON files (ar/en/ur)
```

---

## 🗄 Database Schema (PostgreSQL + pgvector on VPS)

```sql
-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Main knowledge base table
CREATE TABLE knowledge_base (
  id SERIAL PRIMARY KEY,
  source_id TEXT UNIQUE NOT NULL,       -- e.g., "quran_2_255", "hadith_bukhari_1"
  source_type TEXT NOT NULL,            -- "quran", "hadith", "fatwa", "fiqh", "athar"
  fiqh TEXT DEFAULT 'general',          -- "general", "hanafi", "shafi", "maliki", "hanbali"
  text TEXT NOT NULL,                   -- Combined searchable text
  metadata JSONB NOT NULL,             -- Source-specific metadata
  authority JSONB,                      -- For fatwa: {mufti_name, institution}
  embedding vector(1536) NOT NULL,     -- OpenAI text-embedding-3-small
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Vector similarity index (IVFFlat for large datasets)
CREATE INDEX ON knowledge_base
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

-- Filtering indexes
CREATE INDEX idx_source_type ON knowledge_base(source_type);
CREATE INDEX idx_fiqh ON knowledge_base(fiqh);
CREATE INDEX idx_source_id ON knowledge_base(source_id);
```

---

## 🔧 Key API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/` | None | Health check |
| `POST` | `/api/ask` | JWT (required) | Main RAG query endpoint |
| `GET` | `/api/sources/{source_id}` | JWT | Fetch full source text by ID |
| `POST` | `/api/feedback` | JWT (registered only) | Submit answer feedback |

### Request/Response Format

```json
// POST /api/ask
// Request:
{
  "query": "What is the ruling on combining prayers?",
  "source_filter": null,  // optional: "quran", "hadith", "fatwa"
  "fiqh_filter": null      // optional: overrides user's stored madhab
}

// Response:
{
  "answer": "Combining prayers is permitted during travel...",
  "citations": [
    {
      "source_id": "hadith_muslim_705",
      "source_type": "hadith",
      "display_text": "Sahih Muslim (Hadith 705)",
      "metadata": {"collection": "Muslim", "hadith_number": 705, "grade": "Sahih"}
    }
  ],
  "retrieved_chunks": [...],
  "prompt_count_remaining": 18  // for rate-limited users
}
```

---

## 🚀 Deployment Checklist

### VPS Setup (Hetzner / Contabo / Oracle Free)

- [ ] Provision VPS (min 2GB RAM, 80GB SSD)
- [ ] Install: Python 3.11+, PostgreSQL 16+, pgvector, Nginx
- [ ] Configure PostgreSQL with pgvector extension
- [ ] Clone RAG repository
- [ ] Set up `.env` with production keys
- [ ] Run data ingestion script
- [ ] Configure Nginx reverse proxy with SSL (Let's Encrypt)
- [ ] Set up systemd service for FastAPI (uvicorn)
- [ ] Configure firewall (ufw): allow 80, 443, 22 only
- [ ] Set up automated backups (pg_dump cron)
- [ ] Set up monitoring (uptime check)

### Supabase Setup

- [ ] Create `ai_usage` table with RLS policies
- [ ] Enable Anonymous Auth in Supabase dashboard
- [ ] Add `madhab` column to user profiles table
- [ ] Create or verify `conversation_history` table
- [ ] Test JWT validation from VPS → Supabase JWKS

### Security Hardening

- [ ] Remove all hardcoded credentials from code
- [ ] Use environment variables for ALL secrets
- [ ] Enable Nginx rate limiting (10 req/min per IP)
- [ ] Validate and sanitize all user query inputs
- [ ] Add CORS whitelist (app domain only, not `*`)
- [ ] Set up fail2ban for SSH protection
- [ ] PostgreSQL: password auth, no remote access except localhost

---

## 💰 Cost Estimate (Monthly)

| Item | Provider | Cost |
|------|----------|------|
| Supabase (Free tier) | Supabase | $0 |
| VPS (2GB RAM, 80GB SSD) | Hetzner CX22 / Oracle Free | $4-8 |
| OpenAI Embeddings (ingestion + queries) | OpenAI | $1-3 |
| Gemini Flash API (LLM responses) | Google | $5-10 |
| Domain + SSL | Cloudflare | $0 |
| **Total** | | **~$10-21/mo** |

> **Note:** Oracle Cloud Free Tier (ARM, 24GB RAM, 200GB) can reduce VPS cost to $0.