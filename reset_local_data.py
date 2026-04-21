"""
Reset the local SQLite database (all users, projects, messages, etc.).

1. Stop the website: in the terminal where Flask is running, press Ctrl+C.
2. From this folder run:  python reset_local_data.py
3. Start the site again:  python app.py

Then you can register with the same email addresses as before.
"""
from app import app, db, ensure_schema

with app.app_context():
    db.drop_all()
    ensure_schema()
    print("Done. Database cleared and tables recreated. You can register fresh accounts now.")
