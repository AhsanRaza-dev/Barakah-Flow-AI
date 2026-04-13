import chromadb
import time

print("🚀 Starting Metadata Migration for Barakah AI...")

# 1. Connect to the existing Vector DB (No OpenAI key needed for this!)
chroma_client = chromadb.PersistentClient(path="./barakah_vector_db")

try:
    fatawa_collection = chroma_client.get_collection(name="contemporary_fatawa_collection")
except ValueError:
    print("❌ Error: Collection not found. Check your database path.")
    exit()

# 2. Fetch all existing records (Sirf IDs aur Metadata layenge, Vectors nahi)
print("📥 Fetching all records from the database...")
existing_data = fatawa_collection.get(include=["metadatas"])

ids = existing_data['ids']
metadatas = existing_data['metadatas']

total_records = len(ids)
print(f"📊 Found {total_records} fatawa. Updating metadata...")

# 3. Modify the metadata (Adding Madhab)
updated_metadatas = []
for meta in metadatas:
    # Purana data copy karein taake kuch delete na ho
    new_meta = meta.copy() if meta else {}
    
    # Naya sticker (tag) laga dein
    new_meta["madhab"] = "Hanafi" 
    
    updated_metadatas.append(new_meta)

# 4. Save back to ChromaDB (Batches mein taake RAM full na ho)
BATCH_SIZE = 5000
start_time = time.time()

print("⚙️ Saving updates to the database...")
for i in range(0, total_records, BATCH_SIZE):
    batch_ids = ids[i : i + BATCH_SIZE]
    batch_metadatas = updated_metadatas[i : i + BATCH_SIZE]
    
    # ⚠️ UPDATE command sirf metadata change karti hai, embeddings ko nahi chherrti
    fatawa_collection.update(
        ids=batch_ids,
        metadatas=batch_metadatas
    )
    print(f"   ✅ Updated {min(i + BATCH_SIZE, total_records)} / {total_records} records...")

end_time = time.time()
print(f"\n🎉 Metadata Migration Complete in {round(end_time - start_time, 2)} seconds!")
print("🕌 Your database is now officially Madhab-Aware!")