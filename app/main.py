from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import os
import time
import psycopg2
from psycopg2 import pool
from pgvector.psycopg2 import register_vector
from supabase import create_client, Client
from openai import OpenAI
from google import genai
from dotenv import load_dotenv

# ==========================================
# 1. SETUP & KEYS
# ==========================================
load_dotenv()
openai_key = os.getenv("OPENAI_API_KEY")
gemini_key = os.getenv("GEMINI_API_KEY")
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if not all([openai_key, gemini_key, supabase_url, supabase_key]):
    raise RuntimeError("❌ API Keys missing in .env file (Ensure Supabase keys are added)!")

# Initialize AI Clients
openai_client = OpenAI(api_key=openai_key)
gemini_client = genai.Client(api_key=gemini_key)

# Initialize Supabase Client
supabase: Client = create_client(supabase_url, supabase_key)

# ==========================================
# 2. DATABASE CONNECTION POOLING (Speed Hack)
# ==========================================
try:
    # 🟢 10 Connections har waqt ready rahenge, server time bachane ke liye
    db_pool = psycopg2.pool.SimpleConnectionPool(
        1, 10,
        dbname="barakah_db",
        user="postgres",
        password="barakah_secret_2026",
        host="localhost",
        port="5433" # Aapka Docker port
    )
except Exception as e:
    print(f"❌ DB Pool Error: {e}")

def get_db_connection():
    try:
        conn = db_pool.getconn()
        register_vector(conn) # Official Vector Handler
        return conn
    except Exception as e:
        print(f"❌ PostgreSQL Connection Error: {e}")
        return None

def release_db_connection(conn):
    if conn:
        db_pool.putconn(conn)

# 🟢 Semantic Cache Table Setup
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
app = FastAPI(title="Barakah AI API", version="3.0 - Enterprise Edition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    if token != "barakah_flutter_secret_2026": 
        raise HTTPException(status_code=401, detail="❌ Unauthenticated: Invalid Token!")
    return token

# ==========================================
# 4. DATA MODELS & BACKGROUND TASKS
# ==========================================
class AskRequest(BaseModel):
    query: str
    language: str = "roman_urdu"
    madhab: str = "Any"
    session_id: str = "guest_session_123" 
    user_id: str = "anonymous"            

def call_gemini_with_retry(prompt, retries=3):
    for attempt in range(retries):
        try:
            response = gemini_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            return response.text.strip()
        except Exception as e:
            print(f"Gemini API Error (Attempt {attempt + 1}): {e}")
            time.sleep(2)
    return None

# 🟢 BACKGROUND TASK: Supabase logging user ko wait nahi karwayegi
def save_to_supabase_bg(user_id, session_id, user_query, final_answer):
    try:
        if user_id == "anonymous":
            supabase.table("ai_usage").upsert({"session_id": session_id, "prompt_count": 1}).execute() 
        
        supabase.table("conversation_history").insert({
            "session_id": session_id,
            "query": user_query,
            "response": final_answer
        }).execute()
        print("✅ Background Supabase Logging Complete!")
    except Exception as e:
        print(f"❌ Background Log Error: {e}")

# ==========================================
# 5. ENDPOINTS
# ==========================================

# 🟢 Favicon error fix (Terminal ko saaf rakhne ke liye)
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/")
def serve_ui():
    return FileResponse("index.html")

@app.post("/api/ask")
def ask_barakah_ai(request: AskRequest, token: str = Depends(verify_token)):
    user_query = request.query.strip()
    selected_madhab = request.madhab
    
    # 🟢 Rate Limiting
    if request.user_id == "anonymous":
        try:
            usage_res = supabase.table("ai_usage").select("prompt_count").eq("session_id", request.session_id).execute()
            if usage_res.data and usage_res.data[0]['prompt_count'] >= 2:
                raise HTTPException(status_code=403, detail="Free limit reached. Please sign up to ask more questions.")
        except Exception as e:
            print(f"Rate limit check failed: {e}")

    # 🟢 THE STREAMING GENERATOR
    def event_stream():
        # 1. Check Cache first (With Streaming Fix)
        db = get_db_connection()
        if db:
            try:
                cursor = db.cursor()
                cursor.execute("SELECT response FROM response_cache WHERE query = %s AND madhab = %s LIMIT 1", (user_query, selected_madhab))
                cached_result = cursor.fetchone()
                if cached_result:
                    print("⚡ CACHE HIT! Streaming saved response...")
                    cached_text = cached_result[0]
                    
                    # 🟢 Cache ko chote hisso mein tor kar stream karna (Typing Effect)
                    chunk_size = 50 
                    for i in range(0, len(cached_text), chunk_size):
                        chunk = cached_text[i:i+chunk_size].replace("\n", "\\n")
                        yield f"data: {chunk}\n\n"
                        time.sleep(0.01) # Smooth streaming pause
                        
                    yield "data: [DONE]\n\n"
                    return
            finally:
                release_db_connection(db)

        # 2. Fetch Memory Context
        chat_history = ""
        try:
            history_res = supabase.table("conversation_history").select("query, response").eq("session_id", request.session_id).order("created_at", desc=True).limit(2).execute()
            if history_res.data:
                chat_history = "PREVIOUS CONVERSATION CONTEXT:\n"
                for row in reversed(history_res.data): 
                    chat_history += f"User: {row['query']}\nAI: {row['response']}\n\n"
        except:
            pass 

        # 3. Triple-Threat Multi-Lingual Query Generation
        search_query_rich = user_query 
        search_query_roman = user_query 
        
        reformulate_prompt = f"""
        You are an expert Islamic Fiqh search assistant.
        1. Read the history to understand the context.
        2. Resolve pronouns (e.g., replace "is main" with the actual topic).
        3. Translate the resolved query into clear English (vital for semantic search).
        4. Provide the core Classical Arabic terminology.
        
        History: {chat_history}
        Latest Query: {user_query}
        
        Reply EXACTLY in this format (no bold, no markdown):
        ROMAN: [resolved query in Roman Urdu]
        ENGLISH: [English translation]
        ARABIC: [Arabic keywords]
        """
        
        rewritten = call_gemini_with_retry(reformulate_prompt)
        if rewritten:
            rewritten = rewritten.replace("**", "").replace("```", "")
            try:
                lines = rewritten.split('\n')
                english_part = user_query
                arabic_part = ""
                
                for line in lines:
                    if line.startswith("ROMAN:"):
                        search_query_roman = line.replace("ROMAN:", "").strip()
                    elif line.startswith("ENGLISH:"):
                        english_part = line.replace("ENGLISH:", "").strip()
                    elif line.startswith("ARABIC:"):
                        arabic_part = line.replace("ARABIC:", "").strip()
                        
                search_query_rich = f"{english_part} {arabic_part}"
                print(f"🔄 Original: {user_query}")
                print(f"🔥 Final Vector Query: {search_query_rich}")
            except Exception as e:
                print(f"Translation Parsing Error: {e}")

        # 4. Embeddings & DB Search
        print("🔍 Fetching OpenAI Embeddings...")
        emb_res = openai_client.embeddings.create(input=search_query_rich, model="text-embedding-3-large")
        query_vector = emb_res.data[0].embedding
        vector_str = "[" + ",".join(map(str, query_vector)) + "]"

        # RAM-Optimized Fast Search
        books_context = ""
        db = get_db_connection()
        if db:
            try:
                cursor = db.cursor()
                cursor.execute("SET local work_mem = '256MB';")

                if selected_madhab != "Any":
                    search_sql = """
                    WITH filtered_docs AS MATERIALIZED (
                        SELECT metadata->>'title' AS title, metadata->>'author' AS author, text, embedding 
                        FROM knowledge_base 
                        WHERE LOWER(fiqh) = LOWER(%s)
                    )
                    SELECT title, author, text 
                    FROM filtered_docs 
                    ORDER BY embedding <=> %s LIMIT 20;
                    """
                    cursor.execute(search_sql, (selected_madhab, vector_str))
                else:
                    search_sql = """
                    SELECT metadata->>'title' AS title, metadata->>'author' AS author, text 
                    FROM knowledge_base 
                    ORDER BY embedding <=> %s LIMIT 20;
                    """
                    cursor.execute(search_sql, (vector_str,))
                
                results = cursor.fetchall()
                for row in results:
                    title = row[0] if row[0] else "Classical Fiqh Book"
                    author = row[1] if row[1] else "Unknown"
                    books_context += f"Source: [{title} by {author}]\nText: {row[2]}\n\n"
                    
                print(f"✅ DB Search Done! {len(results)} Evidences found.")
            except Exception as e:
                print(f"❌ pgvector Search Error: {e}")
            finally:
                release_db_connection(db)

        # 5. Final AI Generation & Comparator Logic
        comparator_logic = ""
        if selected_madhab == "Any":
            comparator_logic = """
            Since no specific Madhab is selected, you MUST provide a CONCISE and beautifully structured Markdown Table comparing the views of Hanafi, Shafi'i, Maliki, Hanbali, and Ahle Hadith schools.
            🛑 CRITICAL TABLE RULES (TO PREVENT CRASHES):
            1. Columns MUST strictly be exactly 6: | Feature | Hanafi | Shafi'i | Maliki | Hanbali | Ahle Hadith |
            2. Keep the table extremely short (Max 4 to 5 rows).
            3. 🚫 DO NOT put long Arabic Duas, transliterations, or translations INSIDE the table cells. This breaks the formatting!
            4. Inside the table for the "Dua" row, just write brief text like "Yes, see details below".
            5. 🟢 PRINT DUAS OUTSIDE: Print the full Arabic Duas, Roman transliterations, and translations OUTSIDE and BELOW the table in a beautifully formatted separate section.
            6. 🟢 FALLBACK KNOWLEDGE: If the provided context is missing information for any specific Madhab, use your own internal Fiqh knowledge to fill that column. DO NOT leave the table broken.
            """
        else:
            comparator_logic = f"Answer STRICTLY according to the {selected_madhab} school of thought."

        system_prompt = f"""
        You are Barakah AI, an elite Islamic Fiqh assistant. 
        Language: {request.language}
        {chat_history}
        
        {comparator_logic}
        
        CRITICAL RULES:
        1. Base your Fiqh rulings ONLY on the provided CLASSICAL FIQH BOOKS context.
        2. If the user asks a sensitive question about Aqeedah, Sects, Politics, or Divorce (Talaq), add this EXACT tag at the very end of your response: [NEEDS_MUFTI_REVIEW]
        3. 🟢 EXPLICIT REFERENCES SECTION: You MUST include a dedicated "**Sources / References:**" heading at the VERY END of your response. Under this heading, list all the classical books and authors used to generate the answer. Do not skip this footer!
        4. 🟢 EXPLICIT DUAS & TEXTS: If the method involves a specific Dua or Recitation, you MUST write out its FULL Arabic text, Roman transliteration, and translation. EVEN IF the full text is NOT in the provided context, USE YOUR INTERNAL KNOWLEDGE to provide the standard, universally accepted wording.
        5. 🟢 MADHAB-SPECIFIC ACCURACY: You must STRICTLY differentiate between the texts and rulings of different Madhabs. Do not mix them up. (For example, accurately distinguish the Hanafi Dua-e-Qunoot starting with "اللهم إنا نستعينك" from the Shafi'i version starting with "اللهم اهدني"). Apply this strict level of differentiation to ALL prayers, rules, and topics across Islam.

        [CLASSICAL FIQH BOOKS CONTEXT]:
        {books_context}
        
        USER QUESTION: {search_query_roman}
        """

        # 🟢 6. STREAMING THE ANSWER
        full_answer = ""
        try:
            print("🚀 Generating AI Stream...")
            stream_response = gemini_client.models.generate_content_stream(
                model='gemini-2.5-flash', 
                contents=system_prompt
            )
            for chunk in stream_response:
                if chunk.text:
                    text_chunk = chunk.text.replace("\n", "\\n")
                    full_answer += chunk.text
                    yield f"data: {text_chunk}\n\n"
        except Exception as e:
            yield f"data: [ERROR] AI Generation Failed: {str(e)}\n\n"

        yield "data: [DONE]\n\n"

        # 🟢 7. Post-Processing (Cache & Supabase)
        if full_answer:
            db = get_db_connection()
            if db:
                try:
                    cursor = db.cursor()
                    cursor.execute("INSERT INTO response_cache (query, madhab, response) VALUES (%s, %s, %s)", (user_query, selected_madhab, full_answer))
                    db.commit()
                except Exception as e:
                    pass
                finally:
                    release_db_connection(db)
            
            # Save to Supabase
            save_to_supabase_bg(request.user_id, request.session_id, user_query, full_answer)

    # 🟢 Return Streaming Response
    return StreamingResponse(event_stream(), media_type="text/event-stream")