"""
WSGI entry for Gunicorn.

/ping and /health are answered here so Railway never waits on Flask middleware
(Flask-Login, lazy DB init, context processors) for those paths.
"""
from app import app as _flask_app


class _HealthPingFirst:
    __slots__ = ("_app",)

    def __init__(self, app):
        self._app = app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or ""
        norm = path.rstrip("/") or "/"
        if norm == "/ping":
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"pong"]
        if norm == "/health":
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"OK"]
        return self._app(environ, start_response)


application = _HealthPingFirst(_flask_app)
