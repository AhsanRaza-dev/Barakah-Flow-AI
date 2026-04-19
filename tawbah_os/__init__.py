"""
Tawbah OS — Islamic Nafs Rehabilitation System.

Mounted at /api/tawbah alongside RAG + Fitrah engines.
All data is encrypted client-side before persistence (AES-256).
Shares the Barakah DB pool; never touches pgvector or existing tables.
"""
