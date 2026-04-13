import chromadb
import chromadb.utils.embedding_functions as embedding_functions
from google import genai
from pydantic import BaseModel
from typing import List, Dict, Any
from app.config import get_settings
import logging
import asyncio
import time
import json

logger = logging.getLogger(__name__)

class Citation(BaseModel):
    source_id: str
    source_type: str
    display_text: str
    metadata: Dict[str, Any]

class AnswerResponse(BaseModel):
    answer: str
    citations: List[Citation]
    retrieved_chunks: List[Dict[str, Any]]

class RAGService:
    def __init__(self):
        self.settings = get_settings()
        
        if not self.settings.OPENAI_API_KEY or not self.settings.GEMINI_API_KEY:
            logger.error("API Keys missing in .env file!")
            return

        self.client = genai.Client(api_key=self.settings.GEMINI_API_KEY)
        
        self.openai_ef = embedding_functions.OpenAIEmbeddingFunction(
            api_key=self.settings.OPENAI_API_KEY, 
            model_name="text-embedding-3-large"
        )
        
        self.chroma_client = chromadb.PersistentClient(path=self.settings.CHROMADB_PATH)
        
        try:
            self.core_collection = self.chroma_client.get_collection(name="core_evidences_collection", embedding_function=self.openai_ef)
            self.fatawa_collection = self.chroma_client.get_collection(name="contemporary_fatawa_collection", embedding_function=self.openai_ef)
        except Exception as e:
            logger.error(f"Failed to load collections: {e}")
            self.core_collection = None
            self.fatawa_collection = None

    async def _query_core(self, query):
        return await asyncio.to_thread(self.core_collection.query, query_texts=[query], n_results=3)
        
    async def _query_fatawa(self, query, scholar=None):
        where_clause = {"scholar": scholar} if scholar else None
        return await asyncio.to_thread(self.fatawa_collection.query, query_texts=[query], n_results=2, where=where_clause)

    async def ask_barakah_ai_stream(self, user_query: str, scholar: str = None):
        start_time = time.time()
        print(f"\n[STREAM] User Question: '{user_query}'")

        if not self.core_collection or not self.fatawa_collection:
            yield f"data: {json.dumps({'error': 'Vector DB not initialized'})}\n\n"
            return
            
        yield f"data: {json.dumps({'status': 'Translating query...'})}\n\n"

        # 1. Translate Query (Fast Check)
        english_indicators = ["what", "how", "is", "ruling", "on", "can", "why", "who", "when", "does"]
        first_word = user_query.lower().split()[0] if user_query else ""
        
        translated_query = user_query
        if first_word not in english_indicators and " " in user_query:
            translation_prompt = f"""
            Translate the following Islamic query into highly accurate, searchable English keywords.
            If the query is in Roman Urdu or another language, translate it perfectly. Keep it short.
            Query: "{user_query}"
            Output ONLY the translated search query.
            """
            try:
                trans_res = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model='gemini-2.5-flash',
                    contents=translation_prompt
                )
                translated_query = trans_res.text.strip()
                print(f"[STREAM] AI Fast Translated Search Query: '{translated_query}'")
            except Exception as e:
                pass
        else:
            print(f"[STREAM] Skipped Translation, using direct query: '{translated_query}'")
        
        yield f"data: {json.dumps({'status': f'Searching vector databases for: {translated_query}...'})}\n\n"

        # 2. Parallel Search DB
        core_future = self._query_core(translated_query)
        fatawa_future = self._query_fatawa(translated_query, scholar)
        
        core_results, fatawa_results = await asyncio.gather(core_future, fatawa_future)
        
        core_context = ""
        core_chunks = []
        if core_results and 'documents' in core_results and len(core_results['documents'][0]) > 0:
            core_context = "\n".join(core_results['documents'][0])
            for i, doc in enumerate(core_results['documents'][0]):
                meta = core_results['metadatas'][0][i] if 'metadatas' in core_results and core_results['metadatas'] else {}
                doc_id = core_results['ids'][0][i] if 'ids' in core_results and core_results['ids'] else f"core_{i}"
                core_chunks.append({"text": doc, "metadata": meta, "id": doc_id})

        fatawa_context = ""
        fatawa_chunks = []
        if fatawa_results and 'documents' in fatawa_results and len(fatawa_results['documents'][0]) > 0:
            fatawa_context = "\n".join(fatawa_results['documents'][0])
            for i, doc in enumerate(fatawa_results['documents'][0]):
                meta = fatawa_results['metadatas'][0][i] if 'metadatas' in fatawa_results and fatawa_results['metadatas'] else {}
                doc_id = fatawa_results['ids'][0][i] if 'ids' in fatawa_results and fatawa_results['ids'] else f"fatwa_{i}"
                fatawa_chunks.append({"text": doc, "metadata": meta, "id": doc_id})

        citations = []
        for chunk in core_chunks:
            citations.append({
                "source_id": chunk["id"],
                "source_type": chunk["metadata"].get("type", "core"),
                "display_text": chunk["metadata"].get("source", "Unknown Source"),
                "metadata": chunk["metadata"]
            })
            
        for chunk in fatawa_chunks:
            citations.append({
                "source_id": chunk["id"],
                "source_type": chunk["metadata"].get("type", "fatwa"),
                "display_text": chunk["metadata"].get("scholar", "Unknown Scholar") + " - " + chunk["metadata"].get("source_website", "Unknown Site"),
                "metadata": chunk["metadata"]
            })
            
        
        # 3. Generate Answer Streaming
        yield f"data: {json.dumps({'status': 'Drafting fatwa...'})}\n\n"
        
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

        try:
            response_stream = await asyncio.to_thread(
                self.client.models.generate_content_stream,
                model='gemini-2.5-flash',
                contents=system_prompt
            )
            for chunk in response_stream:
                if chunk.text:
                    yield f"data: {json.dumps({'chunk': chunk.text})}\n\n"
                    # Small sleep to yield control loop (not strictly needed since we're in generator but good practice)
                    await asyncio.sleep(0.01)
                    
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            yield f"data: {json.dumps({'error': 'Failed to generate AI response'})}\n\n"
            
        # Send finally citations
        yield f"data: {json.dumps({'citations': citations})}\n\n"
        
        end_time = time.time()
        print(f"✅ Fast RAG generation complete in {round(end_time - start_time, 2)} seconds!")
        yield f"data: [DONE]\n\n"

    def get_source(self, document_id: str):
        if self.core_collection:
            core_res = self.core_collection.get(ids=[document_id])
            if core_res and 'documents' in core_res and core_res['documents']:
                return {
                    "id": core_res['ids'][0],
                    "text": core_res['documents'][0],
                    "metadata": core_res['metadatas'][0] if core_res['metadatas'] else {}
                }
        if self.fatawa_collection:
            fatwa_res = self.fatawa_collection.get(ids=[document_id])
            if fatwa_res and 'documents' in fatwa_res and fatwa_res['documents']:
                return {
                    "id": fatwa_res['ids'][0],
                    "text": fatwa_res['documents'][0],
                    "metadata": fatwa_res['metadatas'][0] if fatwa_res['metadatas'] else {}
                }
        return None

    async def search_pure(self, query: str):
        core_future = self._query_core(query)
        fatawa_future = self._query_fatawa(query)
        core_results, fatawa_results = await asyncio.gather(core_future, fatawa_future)
        
        chunks = []
        if core_results and 'documents' in core_results and len(core_results['documents'][0]) > 0:
            for i, doc in enumerate(core_results['documents'][0]):
                meta = core_results['metadatas'][0][i] if 'metadatas' in core_results and core_results['metadatas'] else {}
                chunks.append({"id": core_results['ids'][0][i], "text": doc, "metadata": meta})
                
        if fatawa_results and 'documents' in fatawa_results and len(fatawa_results['documents'][0]) > 0:
            for i, doc in enumerate(fatawa_results['documents'][0]):
                meta = fatawa_results['metadatas'][0][i] if 'metadatas' in fatawa_results and fatawa_results['metadatas'] else {}
                chunks.append({"id": fatawa_results['ids'][0][i], "text": doc, "metadata": meta})
                
        return chunks

    async def get_daily_wisdom(self):
        import random
        topics = ["patience", "prayer", "faith", "charity", "forgiveness", "mercy", "hereafter", "parents"]
        query = random.choice(topics)
        
        try:
            embedding = await asyncio.to_thread(self.openai_ef, [query])
            core_fut = asyncio.to_thread(self.core_collection.query, query_embeddings=embedding, n_results=2)
            fatwa_fut = asyncio.to_thread(self.fatawa_collection.query, query_embeddings=embedding, n_results=1)
        except Exception as e:
            core_fut = asyncio.to_thread(self.core_collection.query, query_texts=[query], n_results=2)
            fatwa_fut = asyncio.to_thread(self.fatawa_collection.query, query_texts=[query], n_results=1)
            
        core_res, fatawa_res = await asyncio.gather(core_fut, fatwa_fut)
        
        wisdom = []
        if core_res and 'documents' in core_res and len(core_res['documents'][0]) > 0:
            for i, doc in enumerate(core_res['documents'][0]):
                meta = core_res['metadatas'][0][i] if 'metadatas' in core_res and core_res['metadatas'] else {}
                wisdom.append({"id": core_res['ids'][0][i], "text": doc, "metadata": meta})
                    
        if fatawa_res and 'documents' in fatawa_res and len(fatawa_res['documents'][0]) > 0:
            for i, doc in enumerate(fatawa_res['documents'][0]):
                meta = fatawa_res['metadatas'][0][i] if 'metadatas' in fatawa_res and fatawa_res['metadatas'] else {}
                wisdom.append({"id": fatawa_res['ids'][0][i], "text": doc, "metadata": meta})
                    
        return wisdom
