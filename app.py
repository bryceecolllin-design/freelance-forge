"""
MINIMAL DEPLOY TEST — Railway / Gunicorn baseline (no DB, no Stripe).

Restore the full app: replace this file with `app_full.py` or run:
  git checkout app.py
"""
from flask import Flask

app = Flask(__name__)


@app.route("/ping")
def ping():
    return "pong"
