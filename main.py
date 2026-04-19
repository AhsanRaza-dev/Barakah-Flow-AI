"""
main.py — Barakah AI Enterprise (Combined Entry Point)

Merges the two engines into one FastAPI application:
  • RAG Engine  → all existing routes (/api/ask, /, etc.) — UNCHANGED
  • Fitrah Engine → /api/fitrah/* — new gamification layer

Run with:
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload

The RAG engine is imported as the base app.  The Fitrah router is mounted on
top of it so every existing Flutter API call continues to work without any change.
"""

import logging

# ── 1. Import the fully-initialised RAG FastAPI app ──────────────────────────
# This also initialises the shared DB connection pool (psycopg2) and creates the
# response_cache table.  Both engines will reuse that pool via database.py.
from rag_engine.app.main import app  # noqa: E402  (must be first FastAPI import)

# ── 2. Mount the Fitrah gamification router ───────────────────────────────────
from fitrah_engine.fitrah_routes import router as fitrah_router  # noqa: E402

app.title   = "Barakah AI Enterprise"
app.version = "6.0 — RAG + Fitrah + Tawbah OS"
app.description = (
    "Barakah AI — Islamic Fiqh RAG engine combined with Fitrah AI gamification "
    "and Tawbah OS (Nafs Rehabilitation System)."
)

app.include_router(
    fitrah_router,
    prefix="/api/fitrah",
    tags=["Fitrah Gamification Engine"],
)

# ── 3. Mount the Tawbah OS router ────────────────────────────────────────────
from tawbah_os.tawbah_routes import router as tawbah_router  # noqa: E402

app.include_router(
    tawbah_router,
    prefix="/api/tawbah",
    tags=["Tawbah OS — Nafs Rehabilitation"],
)

# ── 4. Start the nightly decay background scheduler ──────────────────────────
from fitrah_engine.scheduler import start_scheduler as start_fitrah_scheduler  # noqa: E402
from tawbah_os.scheduler import start_scheduler as start_tawbah_scheduler  # noqa: E402

start_fitrah_scheduler()
start_tawbah_scheduler()

logging.getLogger("fitrah").setLevel(logging.INFO)
logging.getLogger("tawbah_os").setLevel(logging.INFO)
logging.info(
    "✅ Barakah AI Enterprise started — RAG + Fitrah + Tawbah OS active."
)
