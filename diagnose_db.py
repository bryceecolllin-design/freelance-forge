# diagnose_db.py
import os
from app import app, db
from sqlalchemy import text

with app.app_context():
    print("CWD:", os.getcwd())
    print("DB URL:", db.engine.url)

    # list tables
    tables = db.engine.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    print("Tables:", [t[0] for t in tables])

    # show user table columns
    cols = db.engine.execute(text("PRAGMA table_info(user)")).fetchall()
    print("user columns:", [c[1] for c in cols])
