# database.py — Shared PostgreSQL connection for Barakah AI
# Re-exports the connection pool that is initialised by the RAG engine's main module.
# Both rag_engine and fitrah_engine use the same pool so we never open two
# connection pools to the same database.

from rag_engine.app.main import get_db_connection, release_db_connection

__all__ = ["get_db_connection", "release_db_connection"]
