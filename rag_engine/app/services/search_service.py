import os
import json
import psycopg2
from typing import List, Dict
from sentence_transformers import SentenceTransformer

class PostgresVectorSearchService:
    def __init__(self):
        self.database_url = os.getenv("DATABASE_URL", "postgresql://postgres:Ahsan12345@localhost:5433/postgres")
        # Initialize local model for fast query embedding
        self.embedding_model = SentenceTransformer("BAAI/bge-m3")

    def get_db_connection(self):
        try:
            conn = psycopg2.connect(self.database_url)
            return conn
        except Exception as e:
            raise Exception(f"Database connection failed: {e}")

    def generate_query_embedding(self, query: str) -> List[float]:
        vector = self.embedding_model.encode(query)
        return vector.tolist()

    def hybrid_search(self, query: str, top_k: int = 5) -> List[Dict]:
        query_vector = self.generate_query_embedding(query)
        
        # Format the vector as a string for Postgres
        vector_str = f"[{','.join(map(str, query_vector))}]"
        
        # Keyword filtering
        q_lower = query.lower()
        filter_clause = ""
        filter_params = []
        
        if "verse" in q_lower or "ayah" in q_lower or "quran" in q_lower:
            filter_clause = "WHERE source_type = %s"
            filter_params = ['quran']
        elif "hadith" in q_lower or "prophet said" in q_lower:
            filter_clause = "WHERE source_type = %s"
            filter_params = ['hadith']
            
        sql_query = f"""
            SELECT source_id, source_type, text, metadata, embedding <=> %s AS distance
            FROM knowledge_base
            {filter_clause}
            ORDER BY distance ASC
            LIMIT %s;
        """
        
        query_params = [vector_str] + filter_params + [top_k]
        
        conn = self.get_db_connection()
        cur = conn.cursor()
        
        try:
            cur.execute(sql_query, tuple(query_params))
            results = cur.fetchall()
            
            formatted_results = []
            for row in results:
                formatted_results.append(self._format_result(row))
                
            return formatted_results
        finally:
            cur.close()
            conn.close()

    def _format_result(self, row) -> Dict:
        source_id = row[0]
        source_type = row[1]
        raw_text = row[2]
        meta = row[3] if isinstance(row[3], dict) else json.loads(row[3])
        # row[4] is distance
        
        # Build readable content
        if source_type == 'quran':
            display_text = f"Quran {meta.get('surah_id', '?')}:{meta.get('ayah', '?')}\n"
            display_text += f"{meta.get('arabic', '')}\n{meta.get('english', '')}"
        else:
            display_text = f"{meta.get('collection', 'Hadith')} {meta.get('hadith_number', '?')}\n"
            display_text += f"{meta.get('arabic', '')}\n{meta.get('english', '')}"
            if 'grade' in meta:
                display_text += f"\nGrade: {meta['grade']}"
                
        return {
            'source_id': source_id,
            'source_type': source_type,
            'text': display_text,
            'metadata': meta,
            'raw_text': raw_text
        }
