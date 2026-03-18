import os
import chromadb
import chromadb.utils.embedding_functions as embedding_functions
from google import genai 
from dotenv import load_dotenv

# Setup Keys
load_dotenv()
openai_key = os.getenv("OPENAI_API_KEY")
gemini_key = os.getenv("GEMINI_API_KEY")

if not openai_key or not gemini_key:
    print("❌ Error: API Keys missing in .env file!")
    exit()

client = genai.Client(api_key=gemini_key)

# Connect to DB
openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=openai_key, model_name="text-embedding-3-large"
)
chroma_client = chromadb.PersistentClient(path="./barakah_vector_db")
core_collection = chroma_client.get_collection(name="core_evidences_collection", embedding_function=openai_ef)
fatawa_collection = chroma_client.get_collection(name="contemporary_fatawa_collection", embedding_function=openai_ef)

def ask_barakah_ai(user_query):
    print(f"\n🗣️ User Question: '{user_query}'")

    # 🟢 STEP 1: QUERY TRANSLATION (The Magic Fix)
    translation_prompt = f"""
    Translate the following Islamic query into highly accurate, searchable English keywords. 
    If the query is in Roman Urdu or another language, translate it perfectly. 
    Keep it short, like a Google search query.
    Query: "{user_query}"
    Output ONLY the translated search query.
    """
    
    try:
        translated_query = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=translation_prompt
        ).text.strip()
        print(f"🤖 AI Search Query: '{translated_query}'")
    except Exception as e:
        print("❌ Error translating query. Using original.")
        translated_query = user_query

    # 🟢 STEP 2: SEARCH THE DATABASE (Using Translated Query)
    print("🔍 Searching Database...")
    core_results = core_collection.query(query_texts=[translated_query], n_results=3)
    core_context = "\n".join(core_results['documents'][0])

    fatawa_results = fatawa_collection.query(query_texts=[translated_query], n_results=2)
    fatawa_context = "\n".join(fatawa_results['documents'][0])

    # 🟢 STEP 3: BUILD FINAL ANSWER (Using Original Query)
    system_prompt = f"""
    You are Barakah AI, a highly knowledgeable Islamic Fiqh assistant. 
    Your goal is to answer the user's question based STRICTLY on the provided context. 
    Do not invent any rulings. If the context does not contain the answer, say you don't know.
    
    Please reply in the same language/tone the user used (e.g., if Roman Urdu, reply in Roman Urdu).
    
    Format your response beautifully with Markdown:
    - Start with a direct, compassionate answer.
    - Cite the Primary Evidences (Quran/Hadith).
    - Provide the Contemporary Scholarly view (Fatawa).
    
    PRIMARY EVIDENCES (Quran/Hadith):
    {core_context}
    
    CONTEMPORARY FATAWA:
    {fatawa_context}
    
    USER QUESTION: {user_query}
    """

    print("🧠 Gemini is drafting the Fatwa...\n")
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=system_prompt
        )
        
        print("="*60)
        print("🕌 BARAKAH AI FATWA")
        print("="*60)
        print(response.text)
        print("="*60)
    except Exception as e:
        print(f"❌ Error generating response: {e}")

# 🚀 TEST YOUR SYSTEM HERE!
if __name__ == "__main__":
    question = input('Ask Your Question: ')
    ask_barakah_ai(question)