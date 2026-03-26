from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import os
import time
import chromadb
import chromadb.utils.embedding_functions as embedding_functions
from google import genai
from dotenv import load_dotenv

# 1. Setup & Keys
load_dotenv()
openai_key = os.getenv("OPENAI_API_KEY")
gemini_key = os.getenv("GEMINI_API_KEY")

if not openai_key or not gemini_key:
    raise RuntimeError("❌ API Keys missing in .env file!")

client = genai.Client(api_key=gemini_key)

# 2. Connect to ChromaDB (Ab 3 Collections hain!)
openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=openai_key, model_name="text-embedding-3-large"
)
chroma_client = chromadb.PersistentClient(path="./barakah_vector_db")

core_collection = chroma_client.get_collection(name="core_evidences_collection", embedding_function=openai_ef)
fatawa_collection = chroma_client.get_collection(name="contemporary_fatawa_collection", embedding_function=openai_ef)

# 👉 Kitabon ka naya kamra (Try-except isliye lagaya taake agar kitab na ho to API crash na ho)
try:
    books_collection = chroma_client.get_collection(name="classical_books_collection", embedding_function=openai_ef)
except:
    books_collection = None
    print("⚠️ Classical Books collection not found yet.")

# 3. Initialize FastAPI App
app = FastAPI(title="Barakah AI API", description="Islamic RAG Backend for Flutter", version="2.0")

# 🛡️ MIDDLEWARE: CORS & Security
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

# 🟢 4. DATA MODEL
class AskRequest(BaseModel):
    query: str
    language: str = "roman_urdu"
    madhab: str = "Any"

def call_gemini_with_retry(prompt, retries=3):
    for attempt in range(retries):
        try:
            # Aapka latest gemini-2.5-flash model
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            return response.text.strip()
        except Exception as e:
            print(f"Gemini API Error (Attempt {attempt + 1}): {e}")
            if attempt < retries - 1:
                time.sleep(2)
            else:
                return None

# 🚀 ENDPOINT 1: Serve UI (Aapka original index.html route)
@app.get("/")
def serve_ui():
    return FileResponse("index.html")

# 🚀 ENDPOINT 1.5: Health Check
@app.get("/health")
def health_check():
    return {"status": "success", "message": "Barakah AI Server is Running Perfectly! 🕌"}

# 🚀 ENDPOINT 2: The Main RAG Engine (Double Translation + Book & Fatawa Search)
@app.post("/api/ask")
def ask_barakah_ai(request: AskRequest, token: str = Depends(verify_token)):
    user_query = request.query
    selected_madhab = request.madhab if request.madhab != "Any" else None
    
    # 🟢 STEP A: Smart Double Translation (Arabic & English)
    translation_prompt = f"""
    Translate this Islamic query into two languages for a database search engine:
    1. Classical Arabic (for searching ancient Fiqh texts).
    2. English (for searching contemporary Fatawa).
    
    Query: "{user_query}"
    
    Format EXACTLY like this (do not add any other text):
    ARABIC: [arabic translation here]
    ENGLISH: [english translation here]
    """
    
    translations = call_gemini_with_retry(translation_prompt)
    arabic_query = user_query
    english_query = user_query
    
    if translations and "ARABIC:" in translations and "ENGLISH:" in translations:
        try:
            parts = translations.split("ENGLISH:")
            arabic_query = parts[0].replace("ARABIC:", "").strip()
            english_query = parts[1].strip()
        except:
            pass
            
    print(f"🔍 Searching Books (Arabic): {arabic_query}")
    print(f"🔍 Searching Fatawa (English): {english_query}")

    # 🟢 STEP B: Multi-Database Search
    core_context = ""
    books_context = ""
    fatawa_context = ""
    
    try:
        # 1. Core Evidences (Quran/Hadith)
        core_results = core_collection.query(query_texts=[english_query], n_results=2)
        if core_results['documents'] and core_results['documents'][0]:
            core_context = "\n".join(core_results['documents'][0])

        # 2. Classical Books (Arbi search + Madhab filter)
        if books_collection:
            book_args = {"query_texts": [arabic_query], "n_results": 2}
            if selected_madhab:
                book_args["where"] = {"madhab": selected_madhab.lower()} 
                
            book_res = books_collection.query(**book_args)
            
            if book_res['documents'] and book_res['documents'][0]:
                for i, doc in enumerate(book_res['documents'][0]):
                    meta = book_res['metadatas'][0][i]
                    book_name = meta.get('title', 'Classical Fiqh Book')
                    author = meta.get('author', 'Unknown')
                    books_context += f"Source: [{book_name} by {author}]\n{doc}\n\n"

        # 3. Contemporary Fatawa (English search + Madhab filter)
        fatwa_args = {"query_texts": [english_query], "n_results": 2}
        if selected_madhab:
            fatwa_args["where"] = {"madhab": selected_madhab.capitalize()}
            
        fatwa_results = fatawa_collection.query(**fatwa_args)
        if fatwa_results['documents'] and fatwa_results['documents'][0]:
            fatawa_context = "\n".join(fatwa_results['documents'][0])
        
    except Exception as e:
        print(f"❌ DB Search Error: {e}")

    # 🟢 STEP C: Final AI Generation
    madhab_instruction = f"IMPORTANT RULE: You MUST answer strictly according to the **{request.madhab}** school of thought (Madhab)." if selected_madhab else ""

    system_prompt = f"""
    You are Barakah AI, an elite Islamic Fiqh assistant. Answer STRICTLY based on the provided contexts.
    Reply in the same language/tone the user used ({request.language}).
    {madhab_instruction}
    
    CRITICAL RULES FOR RESPONDING (ACCURACY & ADAPTIVE VERBOSITY):
    1. THE MADHAB OVERRIDE: If the user has selected a specific Madhab (e.g., Hanafi), you MUST provide the specific ruling or Dua prevalent in THAT Madhab. For example, the primary Hanafi Dua Qunoot is "Allahumma inna nasta'eenuka...". 
    2. CONFLICT RESOLUTION: If the Contemporary Fatawa show a general ruling/Dua, but the CLASSICAL FIQH BOOK shows the specific Madhab's ruling/Dua, the CLASSICAL BOOK is your absolute source of truth. Ignore the contradicting contemporary fatawa.
    3. FOR SPECIFIC REQUESTS (e.g., "Give me a specific Dua"): Be extremely concise. Provide EXACTLY the Dua associated with the selected Madhab (Arabic, Roman, Translation) and briefly mention the source. Do not explain extra things.
    4. FOR COMPREHENSIVE QUESTIONS (e.g., "How to pray"): Provide a detailed, step-by-step guide synthesizing the evidences and rules.
    5. EXPLICIT CITATION: Always mention the Classical Book's name if used (e.g., "According to Al-Ikhtiyar...").
    
    Format beautifully with Markdown.
    
    [PRIMARY EVIDENCES (Quran/Hadith)]: 
    {core_context}
    
    [CLASSICAL FIQH BOOKS (Highly Authentic)]:
    {books_context}
    
    [CONTEMPORARY FATAWA]: 
    {fatawa_context}
    
    USER QUESTION: {user_query}
    """

    final_answer = call_gemini_with_retry(system_prompt)
    
    if final_answer:
        return {
            "status": "success",
            "query": user_query,
            "madhab_applied": request.madhab,
            "answer": final_answer
        }
    else:
        raise HTTPException(status_code=503, detail="AI Model failed to generate response.")