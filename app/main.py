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

# Initialize Limiter
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Barakah Flow Secure RAG API", version="1.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS middleware (secure limits to specific origins in prod, using * for dev but highly recommended to change)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust for production!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize service globally since it loads ML models/chromadb clients
rag_service = RAGService()

class QuestionRequest(BaseModel):
    query: str

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
