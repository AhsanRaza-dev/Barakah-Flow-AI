import os
import json
import time
import chromadb
import chromadb.utils.embedding_functions as embedding_functions
from dotenv import load_dotenv

print("🚀 Starting Smart Auto-Chunking Ingestion Pipeline...")

# 1. Setup API Key
load_dotenv()
openai_key = os.getenv("OPENAI_API_KEY")

if not openai_key:
    raise ValueError("❌ OPENAI_API_KEY is missing!")

# 2. Connect to ChromaDB
print("🔌 Connecting to Vector Database...")
openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=openai_key, model_name="text-embedding-3-large"
)
chroma_client = chromadb.PersistentClient(path="./barakah_vector_db")

books_collection = chroma_client.get_or_create_collection(
    name="classical_books_collection",
    embedding_function=openai_ef
)

# 🟢 THE MONEY SAVER
print("🔍 Checking existing records to avoid double-billing...")
existing_data = books_collection.get(include=[]) 
existing_ids = set(existing_data['ids'])
print(f"✅ Found {len(existing_ids)} records already safe in DB. These will be SKIPPED!")

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
                            
                            # 🟢 THE FIX: Lowered MAX_CHARS to 6000 for Arabic safety
                            MAX_CHARS = 6000 
                            
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

    # 4. Batch Ingestion
    BATCH_SIZE = 100
    for i in range(0, total_chunks, BATCH_SIZE):
        batch_ids = ids[i : i + BATCH_SIZE]
        batch_docs = documents[i : i + BATCH_SIZE]
        batch_meta = metadatas[i : i + BATCH_SIZE]
        
        try:
            books_collection.upsert(
                documents=batch_docs,
                metadatas=batch_meta,
                ids=batch_ids
            )
            print(f"   ✅ Ingested {min(i + BATCH_SIZE, total_chunks)} / {total_chunks} NEW records...")
            time.sleep(1.5) 
        except Exception as e:
            print(f"❌ Error inserting batch: {e}")
            break 

# Folder path
BOOKS_FOLDER_PATH = "./Fiqa Books" 

# Script run
ingest_fiqh_books(BOOKS_FOLDER_PATH)

print("\n🎉 Alhamdulilah! Process Completed!")