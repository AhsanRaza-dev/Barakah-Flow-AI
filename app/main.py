from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
import os
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.services.rag_service import RAGService, AnswerResponse
from app.middleware.auth import verify_api_key
from app.config import get_settings

# Initialize Limiter
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Barakah Flow Secure RAG API", version="1.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().ALLOWED_ORIGINS,  # Securely driven by .env
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize service globally since it loads ML models/chromadb clients
rag_service = RAGService()

from typing import Optional

class QuestionRequest(BaseModel):
    query: str

class AdvancedQuestionRequest(BaseModel):
    query: str
    scholar: Optional[str] = None

@app.get("/", response_class=HTMLResponse)
@limiter.limit("10/minute")
def read_root(request: Request):
    html_path = os.path.join(os.path.dirname(__file__), '..', 'index.html')
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            return HTMLResponse(content=f.read())
    except Exception:
        return HTMLResponse(content="<h1>Tester frontend not found</h1>", status_code=404)

@app.post("/api/ask")
@limiter.limit("5/minute")  # Apply rate limit: max 5 requests per minute per IP
async def ask_question(request: Request, body: QuestionRequest, token: str = Depends(verify_api_key)):
    """
    Secure endpoint to ask a question to Barakah AI.
    Streams SSE back to the client.
    """
    try:
        return StreamingResponse(rag_service.ask_barakah_ai_stream(body.query), media_type="text/event-stream")
    except ValueError as ve:
        raise HTTPException(status_code=503, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/source/{document_id}")
@limiter.limit("20/minute")
async def get_source(request: Request, document_id: str, token: str = Depends(verify_api_key)):
    source = rag_service.get_source(document_id)
    if not source:
        raise HTTPException(status_code=404, detail="Document not found")
    return source

@app.post("/api/search")
@limiter.limit("15/minute")
async def pure_search(request: Request, body: QuestionRequest, token: str = Depends(verify_api_key)):
    results = await rag_service.search_pure(body.query)
    return {"results": results}

@app.post("/api/ask/advanced")
@limiter.limit("5/minute")
async def ask_advanced(request: Request, body: AdvancedQuestionRequest, token: str = Depends(verify_api_key)):
    try:
        return StreamingResponse(rag_service.ask_barakah_ai_stream(body.query, scholar=body.scholar), media_type="text/event-stream")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/daily-wisdom")
@limiter.limit("10/minute")
async def daily_wisdom(request: Request, token: str = Depends(verify_api_key)):
    try:
        return {"daily_feed": await rag_service.get_daily_wisdom()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
