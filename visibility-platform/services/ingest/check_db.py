import os
import psycopg
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute("select table_name from information_schema.tables where table_schema = 'public' order by table_name")
print("all public tables:", [r[0] for r in cur.fetchall()])
