import json
import chromadb
import chromadb.utils.embedding_functions as embedding_functions
import os
import time
from dotenv import load_dotenv

print("🚀 Starting Barakah AI ChromaDB Ingestion...\n")

# 1. API Key Setup
load_dotenv()
openai_key = os.getenv("OPENAI_API_KEY")

if not openai_key:
    print("❌ Error: .env file mein OPENAI_API_KEY nahi mili!")
    exit()

# 2. Embedding Function (Multilingual Large Model)
print("🧠 Initializing OpenAI text-embedding-3-large model...")
openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=openai_key,
    model_name="text-embedding-3-large"
)

# 3. ChromaDB Setup
db_path = "./barakah_vector_db"
chroma_client = chromadb.PersistentClient(path=db_path)

# Collections (Quran+Hadith ke liye alag, Fatawa ke liye alag)
core_collection = chroma_client.get_or_create_collection(
    name="core_evidences_collection", 
    embedding_function=openai_ef
)
fatawa_collection = chroma_client.get_or_create_collection(
    name="contemporary_fatawa_collection", 
    embedding_function=openai_ef
)

# 4. Ingestion Function (Batch Processing ke sath)
def ingest_json(file_path, collection, doc_type):
    if not os.path.exists(file_path):
        print(f"⚠️ File not found: {file_path}. Skipping...")
        return

    print(f"\n📂 Loading {doc_type.upper()} data from {file_path}...")
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    docs, metas, ids = [], [], []

    for item in data:
        # Sirf wo data ingest karein jis par Auto-Tagger ne ID lagayi hai
        mapped_ids = item.get("mapped_issue_ids", [])
        if not mapped_ids:
            continue

        # Extracting text and metadata based on document type
        if doc_type == "quran":
            text = item.get("text", {}).get("translation_en", "")
            doc_id = item.get("id", "UNKNOWN_Q")
            meta = {
                "type": "quran",
                "source": f"Surah {item.get('metadata', {}).get('surah_number')}, Ayah {item.get('metadata', {}).get('ayah_number')}",
                "mapped_issues": ",".join(mapped_ids)
            }
        elif doc_type == "hadith":
            text = item.get("text", {}).get("translation_en", "")
            doc_id = item.get("evidence_id", "UNKNOWN_H")
            meta = {
                "type": "hadith",
                "source": item.get("source", "Unknown"),
                "grade": item.get("grade", "Unknown"),
                "mapped_issues": ",".join(mapped_ids)
            }
        elif doc_type == "fatwa":
            question = item.get("question", "")
            answer = item.get("answer", "")
            # Fatwa ke liye Sawal aur Jawab dono search mein kaam aayenge
            text = f"Question: {question}\n\nAnswer: {answer}"
            doc_id = item.get("fatwa_id", "UNKNOWN_F")
            meta = {
                "type": "fatwa",
                "source_website": item.get("source_website", "Unknown"),
                "scholar": item.get("scholar", "Unknown"),
                "mapped_issues": ",".join(mapped_ids)
            }

        if text:
            docs.append(text)
            metas.append(meta)
            ids.append(doc_id)

    # Batch Ingestion (RAM aur API limits ko control karne ke liye)
    total_docs = len(docs)
    BATCH_SIZE = 500  # Ek waqt mein 500 records save honge
    print(f"⚙️ Ingesting {total_docs} records into Vector DB in batches of {BATCH_SIZE}...")
    
    start_time = time.time()
    for i in range(0, total_docs, BATCH_SIZE):
        batch_docs = docs[i : i + BATCH_SIZE]
        batch_meta = metas[i : i + BATCH_SIZE]
        batch_ids = ids[i : i + BATCH_SIZE]
        
        collection.add(
            documents=batch_docs,
            metadatas=batch_meta,
            ids=batch_ids
        )
        print(f"   🔄 Processed {min(i + BATCH_SIZE, total_docs)} / {total_docs} records...")
        time.sleep(0.5) # OpenAI API rate limits se bachne ke liye chota pause

    end_time = time.time()
    print(f"✅ {doc_type.upper()} Ingestion Complete! Took {round((end_time - start_time) / 60, 2)} minutes.")

# 5. Execute Pipeline
if __name__ == "__main__":
    # ✅ EXACT PATHS (Aapke folder structure ke mutabiq)
    quran_path = "data/quran_db_mapped.json"
    hadith_path = "data/athar_db_mapped.json"
    fatwa_path = "data/fatawa_db_standard_mapped.json" # <-- Yeh path update kar diya hai!

    # Execute Ingestion
    ingest_json(quran_path, core_collection, "quran")
    ingest_json(hadith_path, core_collection, "hadith")
    ingest_json(fatwa_path, fatawa_collection, "fatwa")

    print("\n🎉 ALL EVIDENCES SUCCESSFULLY INGESTED INTO CHROMADB!")
    print("💾 Aapka data ab './barakah_vector_db' folder mein mehfooz hai.")