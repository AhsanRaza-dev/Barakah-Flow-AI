import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
    GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
    API_BEARER_TOKEN   = os.getenv("API_BEARER_TOKEN", "barakah_flutter_secret_2026")
    SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
    CHROMADB_PATH      = os.getenv("CHROMADB_PATH", "./barakah_vector_db")
    ALLOWED_ORIGINS    = os.getenv("ALLOWED_ORIGINS", "*").split(",")

def get_settings():
    return Settings()
