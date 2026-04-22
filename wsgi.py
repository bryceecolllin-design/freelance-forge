"""
WSGI entry for Gunicorn.

/ping and /health are answered here so Railway never waits on Flask middleware
(Flask-Login, lazy DB init, context processors) for those paths.
"""
import sys

from app import app as _flask_app


def _norm_path(environ):
    path = environ.get("PATH_INFO") or "/"
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    path = path.rstrip("/") or "/"
    return path


class _HealthPingFirst:
    __slots__ = ("_app",)

    def __init__(self, app):
        self._app = app

    def __call__(self, environ, start_response):
        norm = _norm_path(environ)
        if norm == "/ping":
            sys.stderr.write("[wsgi] /ping short-circuit -> 200 pong\n")
            sys.stderr.flush()
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"pong"]
        if norm == "/health":
            sys.stderr.write("[wsgi] /health short-circuit -> 200 OK\n")
            sys.stderr.flush()
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"OK"]
        return self._app(environ, start_response)


application = _HealthPingFirst(_flask_app)
