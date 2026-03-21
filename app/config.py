import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Settings:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN", "barakah-secure-token-123") # Default for local testing
    CHROMADB_PATH = os.getenv("CHROMADB_PATH", "./barakah_vector_db")
    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

def get_settings():
    return Settings()
