import psycopg2

print("🧹 Clearing the Semantic Cache...")
conn = None
try:
    conn = psycopg2.connect(
        dbname="barakah_db",
        user="postgres",
        password="barakah_secret_2026",
        host="localhost",
        port="5433" # Aapka Docker port
    )
    cursor = conn.cursor()
    
    # Cache table ko bilkul saaf (empty) kar do
    cursor.execute("TRUNCATE TABLE response_cache;")
    conn.commit()
    
    print("✅ Alhamdulilah! Cache bilkul saaf ho gaya hai.")
    print("Ab aapka naya sawal fresh Fiqh ki kitabon se hawale dhoondhega!")
    
except Exception as e:
    print(f"❌ Error: {e}")
finally:
    if conn:
        conn.close()