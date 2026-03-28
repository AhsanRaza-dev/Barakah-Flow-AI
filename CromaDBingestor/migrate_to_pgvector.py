import psycopg2
import chromadb
import json
from psycopg2.extras import execute_values
from pgvector.psycopg2 import register_vector

print("🚀 Starting Zero-Cost Migration to pgvector...")

# 1. Connect to Local ChromaDB
print("🔌 Connecting to old ChromaDB...")
chroma_client = chromadb.PersistentClient(path="./barakah_vector_db")
collections = chroma_client.list_collections()
print(f"📦 Found {len(collections)} collections to migrate.")

# 2. Connect to New PostgreSQL (Docker)
print("🐘 Connecting to new PostgreSQL (pgvector)...")
try:
    conn = psycopg2.connect(
        dbname="barakah_db",
        user="postgres",
        password="barakah_secret_2026",
        host="localhost",
        port="5433"
    )
    conn.autocommit = True
    cursor = conn.cursor()
    
    # 🟢 Extension aur Table banayen
    cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    register_vector(conn)
    
    table_query = """
    CREATE TABLE IF NOT EXISTS knowledge_base (
      id SERIAL PRIMARY KEY,
      source_id TEXT UNIQUE NOT NULL,
      source_type TEXT NOT NULL,
      fiqh TEXT DEFAULT 'general',
      text TEXT NOT NULL,
      metadata JSONB NOT NULL,
      authority JSONB,
      embedding vector(3072) NOT NULL,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    """
    cursor.execute(table_query)
    print("✅ Database Table 'knowledge_base' is ready!")

except Exception as e:
    print(f"❌ Database Connection Error: {e}")
    print("⚠️ Make sure your Docker container is running!")
    exit()

# 3. Migration Logic
BATCH_SIZE = 5000 # 5000 chunks ek waqt mein shift karenge

for collection in collections:
    col_name = collection.name
    print(f"\n🔄 Migrating Collection: '{col_name}'...")
    
    col_obj = chroma_client.get_collection(name=col_name)
    total_count = col_obj.count()
    print(f"📊 Total records in this collection: {total_count}")
    
    offset = 0
    while offset < total_count:
        # 🟢 Extract from Chroma
        batch_data = col_obj.get(
            include=["embeddings", "metadatas", "documents"],
            limit=BATCH_SIZE,
            offset=offset
        )
        
        if not batch_data['ids']:
            break
            
        ids = batch_data['ids']
        texts = batch_data['documents']
        metadatas = batch_data['metadatas']
        embeddings = batch_data['embeddings']
        
        # 🟢 Transform for Postgres
        records_to_insert = []
        for i in range(len(ids)):
            meta = metadatas[i] or {}
            source_type = meta.get("type", "unknown")
            fiqh = meta.get("madhab", "general")
            
            records_to_insert.append((
                ids[i],                 
                source_type,            
                fiqh,                   
                texts[i],               
                json.dumps(meta),       
                None,                   
                embeddings[i]           
            ))
            
        # 🟢 Insert into Postgres
        insert_query = """
        INSERT INTO knowledge_base (source_id, source_type, fiqh, text, metadata, authority, embedding)
        VALUES %s
        ON CONFLICT (source_id) DO NOTHING;
        """
        
        try:
            execute_values(cursor, insert_query, records_to_insert)
            offset += BATCH_SIZE
            print(f"   ✅ Successfully copied {min(offset, total_count)} / {total_count} records...")
        except Exception as e:
            print(f"❌ Error inserting into Postgres: {e}")
            break

print("\n🎉 ALHAMDULILAH! Migration Completed Successfully. Your data is safe in pgvector!")
conn.close()