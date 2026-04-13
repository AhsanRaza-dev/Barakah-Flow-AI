import os
import psycopg2
from dotenv import load_dotenv
load_dotenv()

print("🧹 Clearing the Semantic Cache...")
conn = None
try:
    conn = psycopg2.connect(
        dbname="barakah_db",
        user="postgres",
        password=os.getenv("DB_PASSWORD", "barakah_secret_2026"),
        host="localhost",
        port="5433"
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