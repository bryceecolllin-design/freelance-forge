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
        method = environ.get("REQUEST_METHOD", "GET")
        raw_path = environ.get("PATH_INFO")
        script = environ.get("SCRIPT_NAME") or ""
        ua = (environ.get("HTTP_USER_AGENT") or "")[:160]
        norm = _norm_path(environ)
        # If browser traffic never shows here, the request is not reaching Gunicorn (Railway edge / networking).
        if "RailwayHealthCheck" not in ua:
            sys.stderr.write(
                f"[wsgi-req] {method} PATH_INFO={raw_path!r} SCRIPT_NAME={script!r} norm={norm!r} ua={ua!r}\n"
            )
            sys.stderr.flush()
        if norm == "/ping":
            sys.stderr.write("[wsgi] /ping short-circuit -> 200\n")
            sys.stderr.flush()
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
            if method == "HEAD":
                return []
            return [b"pong"]
        if norm == "/health":
            sys.stderr.write("[wsgi] /health short-circuit -> 200\n")
            sys.stderr.flush()
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
            if method == "HEAD":
                return []
            return [b"OK"]
        return self._app(environ, start_response)


application = _HealthPingFirst(_flask_app)
