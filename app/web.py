"""HTTP layer: routing to api.py, static files, cookies, security headers."""

import json
import logging
import mimetypes
import os
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import api, auth, config, store
from .util import ApiError

log = logging.getLogger("swim.web")

STATIC_DIR = os.path.join(config.BASE_DIR, "static")
MAX_BODY = 256 * 1024
API_PREFIX = "/api/v1"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SwimCal/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ responses

    def _send(self, status, body, ctype, extra=None, cache=None):
        try:
            self._write(status, body, ctype, extra, cache)
        except (BrokenPipeError, ConnectionResetError):
            pass   # client went away (closed tab during a long-poll) - nothing to do

    def _write(self, status, body, ctype, extra, cache=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache or ("no-store" if ctype.startswith(("application/json", "text/csv"))
                                                    else "no-cache"))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, obj, extra=None):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", extra)

    def _cookie_header(self, token, max_age):
        parts = ["%s=%s" % (auth.COOKIE_NAME, token), "Path=/", "HttpOnly", "SameSite=Strict",
                 "Max-Age=%d" % max_age]
        if config.CONFIG.get("cookie_secure"):
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    # ------------------------------------------------------------------ request parsing

    def _token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = cookies.SimpleCookie()
        try:
            c.load(raw)
        except cookies.CookieError:
            return None
        m = c.get(auth.COOKIE_NAME)
        return m.value if m else None

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "too_large", "request body too large")
        if not length:
            return {}
        # JSON only: a cross-site HTML form cannot send this content type (CSRF guard,
        # on top of the SameSite=Strict cookie).
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ApiError(415, "json_required", "Content-Type must be application/json")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "invalid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_json", "JSON object expected")
        return body

    def _client_ip(self):
        return self.client_address[0]

    # ------------------------------------------------------------------ dispatch

    def _handle(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            return self._api(path[len(API_PREFIX):], url.query)
        if self.command not in ("GET", "HEAD"):
            return self._json(405, {"error": "method_not_allowed", "message": "method not allowed"})
        return self._static(path)

    def _api(self, path, query):
        extra = []
        try:
            for method, regex, fn, public, allow_pw_change, _ in api.ROUTES:
                m = regex.match(path)
                if m and method == (self.command if self.command != "HEAD" else "GET"):
                    break
            else:
                if any(r[1].match(path) for r in api.ROUTES):
                    raise ApiError(405, "method_not_allowed", "method not allowed")
                raise ApiError(404, "not_found", "no such endpoint")

            body = self._body() if self.command in ("POST", "PUT", "PATCH", "DELETE") else {}
            token = self._token()
            user = None
            uid = auth.session_user_id(token)
            if uid:
                with store.LOCK:
                    u = store.current()["users"].get(uid)
                    user = dict(u) if u and u.get("active", True) else None
            if not public:
                if not user:
                    raise ApiError(401, "unauthorized", "please log in")
                if user.get("must_change_password") and not allow_pw_change:
                    raise ApiError(403, "password_change_required", "change your password first")
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            params = {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}
            ctx = api.Ctx(user, token, q, body, params, self._client_ip())
            result = fn(ctx)
            if ctx.set_cookie is not None:
                extra.append(self._cookie_header(*ctx.set_cookie))
            if isinstance(result, api.Raw):
                if result.filename:
                    extra.append(("Content-Disposition", 'attachment; filename="%s"' % result.filename))
                return self._send(200, result.body, result.ctype, extra, cache=result.cache)
            return self._json(200, result, extra)
        except ApiError as e:
            return self._json(e.status, {"error": e.code, "message": e.message, **e.extra}, extra)
        except Exception:
            log.exception("unhandled error on %s %s", self.command, path)
            return self._json(500, {"error": "server_error", "message": "internal error"})

    def _static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = os.path.normpath(urllib.parse.unquote(path)).lstrip("/")
        full = os.path.join(STATIC_DIR, rel)
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            # unknown paths fall back to the app shell (client-side routes)
            if "." in os.path.basename(rel):
                return self._send(404, "not found", "text/plain; charset=utf-8")
            full = os.path.join(STATIC_DIR, "index.html")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host, port):
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    return Server((host, port), Handler)
