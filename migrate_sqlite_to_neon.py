#!/usr/bin/env python3
"""One-time, conservative migration from cinema360.db to Neon PostgreSQL.

Run only after backing up the SQLite file and confirming it contains the latest data.
Set DATABASE_URL in the environment; never put credentials in this file.
"""
import os
import sqlite3
import sys

if not os.environ.get("DATABASE_URL"):
    sys.exit("ERROR: Set DATABASE_URL to your Neon connection string in the shell first.")

# Import app to create/verify the PostgreSQL schema using the same schema as production.
import app  # noqa: E402
import psycopg2
import psycopg2.extras

SOURCE = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "cinema360.db"))
TABLES = ["movies", "shows", "panoramas", "seat_positions", "users", "bookings", "messages", "settings"]

if not os.path.isfile(SOURCE):
    sys.exit(f"ERROR: SQLite file not found: {SOURCE}")

src = sqlite3.connect(SOURCE)
src.row_factory = sqlite3.Row
pg = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.DictCursor)
try:
    # Refuse to merge into a populated destination. Blank defaults created by init_db are okay.
    with pg.cursor() as cur:
        counts = {}
        for table in TABLES:
            cur.execute(f'SELECT COUNT(*) AS n FROM "{table}"')
            counts[table] = cur.fetchone()["n"]
        nonempty = {t: n for t, n in counts.items() if n and t != "settings"}
        if counts.get("settings", 0):
            cur.execute("SELECT key,value FROM settings")
            settings = {r["key"]: r["value"] for r in cur.fetchall()}
            if any(v not in (None, "") for v in settings.values()):
                nonempty["settings"] = counts["settings"]
        if nonempty and "--allow-nonempty" not in sys.argv:
            sys.exit("STOP: Neon already contains data: " + repr(nonempty) + ". No rows copied. Check it first; use --allow-nonempty only if you intentionally want a merge.")

    # Parents before children; preserve IDs so existing references remain valid.
    for table in TABLES:
        rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
        if not rows:
            print(f"{table}: 0 rows")
            continue
        columns = rows[0].keys()
        col_sql = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
        placeholders = ",".join(["%s"] * len(columns))
        if table == "settings":
            conflict = " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
        else:
            conflict = " ON CONFLICT DO NOTHING"
        statement = f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders}){conflict}'
        with pg.cursor() as cur:
            for row in rows:
                cur.execute(statement, tuple(row[c] for c in columns))
        pg.commit()
        print(f"{table}: processed {len(rows)} rows")

    # Ensure future auto-generated IDs continue above the highest imported ID.
    with pg.cursor() as cur:
        for table in ["movies", "shows", "panoramas", "seat_positions", "users", "bookings", "messages"]:
            cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
            seq = cur.fetchone()[0]
            if seq:
                cur.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')
                max_id = cur.fetchone()[0]
                cur.execute("SELECT setval(%s, %s, %s)", (seq, max_id if max_id else 1, bool(max_id)))
    pg.commit()
    print("Migration completed. Verify counts in Neon and test the website before changing production DATABASE_URL.")
except Exception:
    pg.rollback()
    raise
finally:
    src.close()
    pg.close()
