from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks, Response, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import time
import jwt as pyjwt
import psycopg2
import psycopg2.pool
from pgvector.psycopg2 import register_vector
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from supabase import create_client, Client
from openai import OpenAI
from google import genai
from dotenv import load_dotenv

log = logging.getLogger("barakah.api")

# ==========================================
# 1. SETUP & KEYS
# ==========================================
load_dotenv()
openai_key    = os.getenv("OPENAI_API_KEY")
gemini_key    = os.getenv("GEMINI_API_KEY")
supabase_url  = os.getenv("SUPABASE_URL")
supabase_key  = os.getenv("SUPABASE_KEY")
jwt_secret    = os.getenv("SUPABASE_JWT_SECRET")
db_password   = os.getenv("DB_PASSWORD", "barakah_secret_2026")

if not all([openai_key, gemini_key, supabase_url, supabase_key]):
    raise RuntimeError("❌ API Keys missing in .env!")

openai_client  = OpenAI(api_key=openai_key)
gemini_client  = genai.Client(api_key=gemini_key)
supabase: Client = create_client(supabase_url, supabase_key)

# ==========================================
# 2. DATABASE CONNECTION POOLING
# ==========================================
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(  # type: ignore[attr-defined]
        1, 10,
        dbname="barakah_db",
        user="postgres",
        password=db_password,
        host="localhost",
        port="5433"
    )
except Exception as e:
    print(f"❌ DB Pool Error: {e}")
    db_pool = None

def get_db_connection():
    try:
        conn = db_pool.getconn()
        register_vector(conn)
        return conn
    except Exception as e:
        print(f"❌ PostgreSQL Connection Error: {e}")
        return None

def release_db_connection(conn):
    if conn:
        db_pool.putconn(conn)

# Semantic cache table
conn = get_db_connection()
if conn:
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS response_cache (
        id SERIAL PRIMARY KEY,
        query TEXT NOT NULL,
        madhab TEXT NOT NULL,
        response TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    """)
    conn.commit()
    release_db_connection(conn)

# ==========================================
# 3. FASTAPI APP & MIDDLEWARE
# ==========================================
app = FastAPI(title="Barakah AI API", version="4.0 - JWT Edition")

# Rate limiter (slowapi) — keyed by client IP. Fitrah/RAG AI endpoints apply
# explicit @limiter.limit() decorators. Default 120/min is a soft cap for the rest.
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — default to localhost only. Production MUST set ALLOWED_ORIGINS explicitly.
_cors_env = os.getenv("ALLOWED_ORIGINS", "").strip()
if _cors_env:
    _allowed_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _allowed_origins = ["http://localhost", "http://localhost:3000", "http://127.0.0.1"]
    log.warning("ALLOWED_ORIGINS env not set — defaulting to localhost. Set this in production.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=_allowed_origins != ["*"],  # credentials only when origins are explicit
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

security = HTTPBearer()

# Static dev token — only honoured when BOTH (a) SUPABASE_JWT_SECRET is unset
# (i.e. we're in dev) AND (b) API_BEARER_TOKEN is explicitly set. No hardcoded
# default: production with JWT configured never accepts a static token.
_DEV_STATIC_TOKEN = os.getenv("API_BEARER_TOKEN") if not jwt_secret else None
if _DEV_STATIC_TOKEN:
    log.warning("SUPABASE_JWT_SECRET unset — static API_BEARER_TOKEN fallback is active (dev mode).")


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials

    if jwt_secret:
        try:
            payload = pyjwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
            return payload
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired. Please sign in again.")
        except pyjwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")

    # Dev-mode fallback — only when JWT secret is unset and operator has set API_BEARER_TOKEN
    if _DEV_STATIC_TOKEN and token == _DEV_STATIC_TOKEN:
        return {"sub": "anonymous", "role": "anon", "is_anonymous": True}

    raise HTTPException(status_code=401, detail="Invalid or missing token.")

# ==========================================
# 4. DATA MODELS
# ==========================================
class AskRequest(BaseModel):
    query: str
    language: str = "roman_urdu"
    madhab: str = "Any"
    session_id: str = "guest_session_123"
    user_id: str = "anonymous"   # overridden by JWT sub below
    save_history: bool = True    # False when user has anonymous mode toggled on

    @property
    def safe_query(self) -> str:
        return self.query.strip()[:1500]  # cap at 1500 chars to protect OpenAI costs

def call_gemini_fast(prompt: str) -> str | None:
    """Single Gemini call with one retry. Kept short — speed over robustness."""
    for attempt in range(2):
        try:
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            return response.text.strip()
        except Exception as e:
            print(f"Gemini error (attempt {attempt + 1}): {e}")
            if attempt == 0:
                time.sleep(1)
    return None

# ── Background Supabase logging ───────────────────────────────────────────────
def save_to_supabase_bg(user_id: str, session_id: str, user_query: str, final_answer: str, save_history: bool = True):
    try:
        if save_history:
            supabase.table("conversation_history").insert({
                "user_id": user_id,
                "session_id": session_id,
                "query": user_query,
                "response": final_answer,
            }).execute()

        # Always increment usage count regardless of anonymous mode
        supabase.rpc("increment_prompt_count", {"uid": user_id}).execute()

        print("✅ Background Supabase log complete.")
    except Exception as e:
        print(f"❌ Background log error: {e}")

# ── Rate limiting helper ──────────────────────────────────────────────────────
def check_rate_limit(user_id: str, is_anonymous: bool):
    """Raises HTTP 403 when a guest exceeds 2 prompts or free user exceeds 20/day.
    For registered users, resets the count if updated_at is from a previous day."""
    try:
        res = supabase.table("ai_usage") \
            .select("prompt_count, updated_at") \
            .eq("user_id", user_id) \
            .single() \
            .execute()

        if not res.data:
            return  # no row yet → first prompt, allow it

        count      = res.data["prompt_count"]
        updated_at = res.data.get("updated_at", "")
        limit      = 2 if is_anonymous else 20

        # Daily reset for registered users: if last prompt was before today, reset count
        if not is_anonymous and updated_at:
            from datetime import datetime, timezone
            last_date = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).date()
            today     = datetime.now(timezone.utc).date()
            if last_date < today:
                supabase.table("ai_usage") \
                    .update({"prompt_count": 0, "updated_at": "now()"}) \
                    .eq("user_id", user_id) \
                    .execute()
                return  # reset done, allow this prompt

        if count >= limit:
            msg = (
                "Free limit reached. Please sign up to ask more questions."
                if is_anonymous
                else "Daily limit reached. Come back tomorrow!"
            )
            raise HTTPException(status_code=403, detail=msg)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Rate limit check failed: {e}")  # Don't block on failure

# ==========================================
# 5. ENDPOINTS
# ==========================================
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/")
def serve_ui():
    return FileResponse("index.html")

@app.post("/api/ask")
@limiter.limit("30/minute")
def ask_barakah_ai(request: Request, ask: AskRequest, jwt_payload: dict = Depends(verify_token)):
    user_query      = ask.safe_query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")
    selected_madhab = ask.madhab

    # Extract real user identity from JWT (overrides request body)
    user_id      = jwt_payload.get("sub", "anonymous")
    is_anonymous = jwt_payload.get("is_anonymous", jwt_payload.get("role") == "anon")

    # Rate limit before doing any expensive work
    check_rate_limit(user_id, is_anonymous)

    def event_stream():
        # ── 1. Cache check ────────────────────────────────────────────────────
        db = get_db_connection()
        if db:
            try:
                cursor = db.cursor()
                cursor.execute(
                    "SELECT response FROM response_cache WHERE query = %s AND madhab = %s LIMIT 1",
                    (user_query, selected_madhab)
                )
                cached = cursor.fetchone()
                if cached:
                    print("⚡ Cache hit — streaming saved response")
                    text = cached[0]
                    chunk_size = 80  # slightly bigger chunks = fewer yields = faster
                    for i in range(0, len(text), chunk_size):
                        yield f"data: {text[i:i+chunk_size].replace(chr(10), chr(92)+'n')}\n\n"
                    yield "data: [DONE]\n\n"
                    return
            finally:
                release_db_connection(db)

        # ── 2. PARALLEL: query reformulation + conversation history ───────────
        # Both tasks are independent so we run them at the same time.
        # Reformulation: enriches the search query with English + Arabic keywords.
        # History fetch: gets the last 2 exchanges for the final LLM context.
        # Combined wall time = max(reformulation ~2s, history ~0.3s) = ~2s
        # vs sequential = ~2.5s

        reformulate_prompt = (
            f"You are an Islamic search assistant.\n"
            f"Read this query and output EXACTLY 3 lines, no markdown:\n"
            f"ENGLISH: [clean English translation/keywords]\n"
            f"ARABIC: [core Arabic terminology]\n"
            f"ROMAN: [resolved Roman Urdu query]\n\n"
            f"Query: {user_query}"
        )

        def _reformulate():
            return call_gemini_fast(reformulate_prompt)

        def _fetch_history():
            try:
                res = supabase.table("conversation_history") \
                    .select("query, response") \
                    .eq("session_id", ask.session_id) \
                    .order("created_at", desc=True) \
                    .limit(2) \
                    .execute()
                if not res.data:
                    return ""
                lines = "PREVIOUS CONVERSATION:\n"
                for row in reversed(res.data):
                    lines += f"User: {row['query']}\nAI: {row['response']}\n\n"
                return lines
            except Exception:
                return ""

        with ThreadPoolExecutor(max_workers=2) as ex:
            reform_future  = ex.submit(_reformulate)
            history_future = ex.submit(_fetch_history)
            rewritten      = reform_future.result()
            chat_history   = history_future.result()

        # Parse reformulation output
        search_query_english = user_query
        search_query_arabic  = ""
        search_query_roman   = user_query

        if rewritten:
            rewritten = rewritten.replace("**", "").replace("```", "")
            for line in rewritten.splitlines():
                if line.startswith("ENGLISH:"):
                    search_query_english = line[8:].strip()
                elif line.startswith("ARABIC:"):
                    search_query_arabic = line[7:].strip()
                elif line.startswith("ROMAN:"):
                    search_query_roman = line[6:].strip()

        enriched_query = f"{search_query_english} {search_query_arabic}".strip()
        print(f"🔄 Original: {user_query}")
        print(f"🔥 Enriched query: {enriched_query}")

        # ── 3. Embedding ──────────────────────────────────────────────────────
        # MUST use text-embedding-3-large (3072 dims) — stored embeddings are
        # 3072-dimensional. Using a different model would corrupt search results.
        print("🔍 Generating embedding (text-embedding-3-large)…")
        emb_res      = openai_client.embeddings.create(input=enriched_query, model="text-embedding-3-large")
        query_vector = emb_res.data[0].embedding
        vector_str   = "[" + ",".join(map(str, query_vector)) + "]"

        # ── 4. pgvector search (top 8 — sufficient context, faster LLM) ──────
        books_context = ""
        db = get_db_connection()
        if db:
            try:
                cursor = db.cursor()
                cursor.execute("SET local work_mem = '256MB';")
                # HNSW ef_search: higher = better recall, lower = faster (default 40)
                cursor.execute("SET local hnsw.ef_search = 80;")

                if selected_madhab != "Any":
                    cursor.execute("""
                        WITH filtered AS MATERIALIZED (
                            SELECT metadata->>'title' AS title,
                                   metadata->>'author' AS author,
                                   text, embedding
                            FROM knowledge_base
                            WHERE LOWER(fiqh) = LOWER(%s)
                        )
                        SELECT title, author, text
                        FROM filtered
                        ORDER BY embedding <=> %s
                        LIMIT 8;
                    """, (selected_madhab, vector_str))
                else:
                    cursor.execute("""
                        SELECT metadata->>'title' AS title,
                               metadata->>'author' AS author,
                               text
                        FROM knowledge_base
                        ORDER BY embedding <=> %s
                        LIMIT 8;
                    """, (vector_str,))

                results = cursor.fetchall()
                for row in results:
                    title  = row[0] or "Classical Fiqh Book"
                    author = row[1] or "Unknown"
                    books_context += f"[{title} by {author}]\n{row[2]}\n\n"

                print(f"✅ DB search done — {len(results)} chunks retrieved.")
            except Exception as e:
                print(f"❌ pgvector error: {e}")
            finally:
                release_db_connection(db)

        # ── 5. Final AI generation ────────────────────────────────────────────
        comparator_logic = (
            """
            Since no specific Madhab is selected, provide a concise Markdown table comparing
            Hanafi, Shafi'i, Maliki, Hanbali, Ahle Hadith (max 5 rows, 6 columns).
            DO NOT put full Arabic duas inside table cells — list them below the table instead.
            Fill missing madhab cells from your internal knowledge.
            """
            if selected_madhab == "Any"
            else f"Answer STRICTLY according to the {selected_madhab} school of thought."
        )

        system_prompt = f"""You are Barakah AI, an elite Islamic Fiqh assistant.
Language: {ask.language}
{chat_history}
{comparator_logic}

RULES:
1. Base rulings ONLY on the provided classical Fiqh context.
2. For sensitive topics (Aqeedah, Sects, Politics, Talaq) append [NEEDS_MUFTI_REVIEW] at the end.
3. End EVERY response with a "**Sources / References:**" section listing all classical books used.
4. Write full Arabic text + Roman transliteration + translation for any Dua/Recitation.
5. NEVER mix different Madhab rulings.

[CLASSICAL FIQH CONTEXT]:
{books_context}

USER QUESTION: {search_query_roman}"""

        # ── 6. Stream the answer ──────────────────────────────────────────────
        full_answer = ""
        try:
            print("🚀 Streaming from Gemini…")
            for chunk in gemini_client.models.generate_content_stream(
                model='gemini-2.5-flash',
                contents=system_prompt
            ):
                if chunk.text:
                    full_answer += chunk.text
                    yield f"data: {chunk.text.replace(chr(10), chr(92)+'n')}\n\n"
        except Exception as e:
            yield f"data: [ERROR] AI generation failed: {e}\n\n"

        yield "data: [DONE]\n\n"

        # ── 7. Post-processing: cache + Supabase (non-blocking) ───────────────
        if full_answer:
            # Local cache
            db = get_db_connection()
            if db:
                try:
                    cursor = db.cursor()
                    cursor.execute(
                        "INSERT INTO response_cache (query, madhab, response) VALUES (%s, %s, %s)",
                        (user_query, selected_madhab, full_answer)
                    )
                    db.commit()
                except Exception:
                    pass
                finally:
                    release_db_connection(db)

            # Supabase conversation + usage (background — does NOT block response)
            # Skip saving history if user has anonymous mode enabled
            save_to_supabase_bg(
                user_id, ask.session_id, user_query, full_answer,
                save_history=ask.save_history
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Fitrah Engine routes ──────────────────────────────────────────────────────
import sys, os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "..", ".."))
from fitrah_engine.fitrah_routes import router as fitrah_router
app.include_router(fitrah_router, prefix="/api/fitrah")
