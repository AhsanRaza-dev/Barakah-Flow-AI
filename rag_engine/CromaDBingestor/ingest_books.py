import os
import json
import time
import psycopg2
from psycopg2.extras import execute_values
from openai import OpenAI
from dotenv import load_dotenv

print("🚀 Starting STRICT Auto-Chunking Ingestion Pipeline for pgvector...")

# 1. Setup API Key
load_dotenv()
openai_key = os.getenv("OPENAI_API_KEY")

if not openai_key:
    raise ValueError("❌ OPENAI_API_KEY is missing!")

# Initialize OpenAI Client
openai_client = OpenAI(api_key=openai_key)

# 2. Connect to PostgreSQL (Docker on port 5433)
print("🐘 Connecting to PostgreSQL Database...")
try:
    conn = psycopg2.connect(
        dbname="barakah_db",
        user="postgres",
        password="barakah_secret_2026",
        host="localhost",
        port="5433" # 🟢 Conflict bachane wala naya port
    )
    cursor = conn.cursor()
except Exception as e:
    print(f"❌ Database Connection Error: {e}")
    print("⚠️ Hint: Ensure Docker container is running on port 5433.")
    exit()

# 🟢 STRICT RULE 1: Ultra-Safe Memory Fetching (Checking Existing IDs)
print("🔍 Checking existing records to avoid double-billing...")
try:
    cursor.execute("SELECT source_id FROM knowledge_base;")
    existing_ids = {row[0] for row in cursor.fetchall()}
    print(f"✅ Found {len(existing_ids)} records already safe in DB. These will be SKIPPED!")
except Exception as e:
    print(f"❌ DB Read Error: {e}")
    print("⚠️ Hint: Kya database ki table ban chuki hai?")
    exit()

# 3. Data Load & Process Function
def ingest_fiqh_books(base_folder_path):
    documents = []
    metadatas = []
    ids = []
    
    print(f"📂 Scanning folder: {base_folder_path}")
    
    for root, dirs, files in os.walk(base_folder_path):
        for file in files:
            if file.endswith(".json"):
                file_path = os.path.join(root, file)
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    try:
                        data = json.load(f)
                        if isinstance(data, dict):
                            data = [data] 
                            
                        for item in data:
                            doc_id = item.get("book_id", "")
                            text = item.get("text", {}).get("arabic", "")
                            
                            if not doc_id or not text:
                                continue
                                
                            meta = {
                                "type": "classical_book",
                                "madhab": str(item.get("madhhab", "Unknown")).lower(), 
                                "title": str(item.get("title", "Unknown")),
                                "author": str(item.get("author", "Unknown")),
                                "volume": str(item.get("volume", "")),
                                "page": str(item.get("page", "")),
                                "chapter": str(item.get("chapter", ""))
                            }
                            
                            # 🟢 STRICT RULE 2: Max Characters locked to 5000 (Very Safe for Arabic)
                            MAX_CHARS = 5000 
                            
                            if len(text) > MAX_CHARS:
                                chunks = [text[i:i+MAX_CHARS] for i in range(0, len(text), MAX_CHARS)]
                                for idx, sub_text in enumerate(chunks):
                                    sub_id = f"{doc_id}_part{idx+1}"
                                    
                                    if sub_id in existing_ids:
                                        continue
                                        
                                    ids.append(sub_id)
                                    documents.append(sub_text)
                                    metadatas.append(meta)
                            else:
                                if doc_id in existing_ids:
                                    continue
                                    
                                ids.append(doc_id)
                                documents.append(text)
                                metadatas.append(meta)
                            
                    except Exception as e:
                        print(f"⚠️ Error reading {file_path}: {e}")

    total_chunks = len(ids)
    if total_chunks == 0:
        print("🎉 All chunks are already in the database! Nothing new to process.")
        return

    print(f"📊 Found {total_chunks} NEW Arabic text chunks. Starting vectorization...")

    # 🟢 STRICT RULE 3: Micro-Batching
    BATCH_SIZE = 50 
    
    for i in range(0, total_chunks, BATCH_SIZE):
        batch_ids = ids[i : i + BATCH_SIZE]
        batch_docs = documents[i : i + BATCH_SIZE]
        batch_meta = metadatas[i : i + BATCH_SIZE]
        
        # 🟢 STRICT RULE 4: Auto-Retry Logic (3 attempts)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 1. Fetch Embeddings direct from OpenAI
                response = openai_client.embeddings.create(
                    input=batch_docs,
                    model="text-embedding-3-large"
                )
                embeddings = [item.embedding for item in response.data]
                
                # 2. Format Data for PostgreSQL
                records_to_insert = []
                for j in range(len(batch_ids)):
                    meta = batch_meta[j]
                    records_to_insert.append((
                        batch_ids[j],                  # source_id
                        meta.get("type"),              # source_type
                        meta.get("madhab"),            # fiqh
                        batch_docs[j],                 # text
                        json.dumps(meta),              # metadata JSON
                        None,                          # authority
                        embeddings[j]                  # embedding vector
                    ))
                
                # 3. Insert into pgvector securely
                insert_query = """
                INSERT INTO knowledge_base (source_id, source_type, fiqh, text, metadata, authority, embedding)
                VALUES %s
                ON CONFLICT (source_id) DO NOTHING;
                """
                execute_values(cursor, insert_query, records_to_insert)
                conn.commit()
                
                print(f"   ✅ Ingested {min(i + BATCH_SIZE, total_chunks)} / {total_chunks} NEW records into pgvector...")
                
                # 🟢 STRICT RULE 5: API Cooldown (2 Seconds breather)
                time.sleep(2.0) 
                break # Success ho gaya to retry loop tod do
                
            except Exception as e:
                print(f"⚠️ Batch Insert Error on attempt {attempt+1}/{max_retries}: {e}")
                conn.rollback() # Error ke case mein transaction wapis le lein
                if attempt < max_retries - 1:
                    print("🔄 Retrying in 5 seconds...")
                    time.sleep(5) 
                else:
                    print("❌ Fatal Error: OpenAI or DB issue. Stopping script safely to prevent corruption.")
                    return

# Folder path
BOOKS_FOLDER_PATH = "./Fiqa Books" 

# Script run
ingest_fiqh_books(BOOKS_FOLDER_PATH)

print("\n🎉 Alhamdulilah! STRICT pgvector Process Completed Safely!")
cursor.close()
conn.close()