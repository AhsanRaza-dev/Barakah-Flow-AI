import json
import chromadb
import chromadb.utils.embedding_functions as embedding_functions
import os
import time
from dotenv import load_dotenv

print("🚀 Resuming Barakah AI Ingestion (FATAWA ONLY)...\n")

load_dotenv()
openai_key = os.getenv("OPENAI_API_KEY")

if not openai_key:
    print("❌ Error: .env file mein OPENAI_API_KEY nahi mili!")
    exit()

# OpenAI Embedding setup
openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=openai_key,
    model_name="text-embedding-3-large"
)

# ChromaDB Client setup
chroma_client = chromadb.PersistentClient(path="./barakah_vector_db")
fatawa_collection = chroma_client.get_or_create_collection(
    name="contemporary_fatawa_collection", 
    embedding_function=openai_ef
)

# Aapke terminal path ke mutabiq
FATWA_PATH = "data/fatawa_db_standard_mapped.json"

print(f"📂 Loading FATWA data from {FATWA_PATH}...")
with open(FATWA_PATH, 'r', encoding='utf-8') as f:
    data = json.load(f)

docs, metas, ids = [], [], []
seen_ids = set() 

# 👉 BULLETPROOF SAFETY LIMIT (8000 Chars)
MAX_CHARS_PER_FATWA = 8000 

for item in data:
    mapped_ids = item.get("mapped_issue_ids", [])
    if not mapped_ids:
        continue

    question = item.get("question", "")
    answer = item.get("answer", "")
    text = f"Question: {question}\n\nAnswer: {answer}"
    
    # ✂️ Agar fatwa limit se lamba hai, to cut kar do
    if len(text) > MAX_CHARS_PER_FATWA:
        text = text[:MAX_CHARS_PER_FATWA] + "\n...[Text Truncated for AI Limits]"
    
    raw_id = item.get("fatwa_id", "UNKNOWN_F")
    final_id = raw_id
    counter = 1
    while final_id in seen_ids:
        final_id = f"{raw_id}_{counter}"
        counter += 1
    seen_ids.add(final_id)

    meta = {
        "type": "fatwa",
        "source_website": item.get("source_website", "Unknown"),
        "scholar": item.get("scholar", "Unknown"),
        "mapped_issues": ",".join(mapped_ids)
    }

    if text:
        docs.append(text)
        metas.append(meta)
        ids.append(final_id)

total_docs = len(docs)
BATCH_SIZE = 100 

# 👉 Seedha 15400 se aage nikalte hain
START_INDEX = 15400 

print(f"⚙️ Ingesting from record {START_INDEX} to {total_docs}...")
print(f"📦 Batch Size: {BATCH_SIZE} | ⏱️ API Delay: 1.5s")

start_time = time.time()
for i in range(START_INDEX, total_docs, BATCH_SIZE):
    batch_docs = docs[i : i + BATCH_SIZE]
    batch_meta = metas[i : i + BATCH_SIZE]
    batch_ids = ids[i : i + BATCH_SIZE]
    
    fatawa_collection.upsert(
        documents=batch_docs,
        metadatas=batch_meta,
        ids=batch_ids
    )
    
    print(f"   🔄 Processed {min(i + BATCH_SIZE, total_docs)} / {total_docs} records...")
    time.sleep(1.5)

end_time = time.time()
print(f"\n✅ FATWA Ingestion Complete! Took {round((end_time - start_time) / 60, 2)} minutes.")
print("🎉 ALL DATA IS NOW SUCCESSFULLY SAVED!")