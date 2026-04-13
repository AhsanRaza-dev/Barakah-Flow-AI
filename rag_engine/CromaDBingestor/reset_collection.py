import chromadb

print("🔌 Connecting to Vector Database...")
try:
    chroma_client = chromadb.PersistentClient(path="./barakah_vector_db")
    
    # 1. Pehle dekhte hain total collections kitni hain
    collections = chroma_client.list_collections()
    print("\n📦 Mojooda Collections:")
    for c in collections:
        print(f" - {c.name}")
        
    # 2. Sirf kharab collection ko delete karte hain
    target_collection = "classical_books_collection"
    print(f"\n🗑️ Deleting corrupted collection: '{target_collection}'...")
    
    chroma_client.delete_collection(name=target_collection)
    print(f"✅ Alhamdulilah! '{target_collection}' successfully delete ho gayi.")
    print("🛡️ Aapki baqi collections bilkul safe hain!")

except Exception as e:
    print(f"❌ Error: {e}")