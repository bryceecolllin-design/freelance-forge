# init_db.py
from app import app, db

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("DB created / synchronized with current models.")
