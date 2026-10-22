"""Passwords, sessions, login throttling and role checks."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import config, store
from .util import ApiError

PBKDF2_ITERATIONS = 240_000
COOKIE_NAME = "swim_session"
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
MIN_PASSWORD = 8
# No 0/O, 1/l/I - generated passwords get read out loud or typed from a phone screen.
PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# --------------------------------------------------------------------------- passwords

def hash_password(password, iterations=PBKDF2_ITERATIONS):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (
        iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password, encoded):
    try:
        algo, iterations, salt, expected = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(dk, base64.b64decode(expected))
    except (ValueError, AttributeError):
        return False


# Computed once so unknown usernames cost the same time as wrong passwords.
_DUMMY_HASH = None


def _dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))
    return _DUMMY_HASH


def generate_password(length=10):
    while True:
        pw = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        # make sure it has a digit and both cases, so it passes any later policy
        if any(c.isdigit() for c in pw) and any(c.islower() for c in pw) and any(c.isupper() for c in pw):
            return pw


def validate_new_password(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        raise ApiError(400, "password_too_short", "password must have at least %d characters" % MIN_PASSWORD)
    if len(pw) > 200:
        raise ApiError(400, "invalid_field", "password is too long", field="password")


def normalise_username(username):
    u = (username or "").strip().lower() if isinstance(username, str) else ""
    if not USERNAME_RE.match(u):
        raise ApiError(400, "invalid_username", "username: 3-32 chars of a-z 0-9 . _ -")
    return u


def find_user(d, username):
    for u in d["users"].values():
        if u["username"] == username:
            return u
    return None


def authenticate(username, password):
    """Return the user dict for valid, active credentials, else None."""
    username = (username or "").strip().lower() if isinstance(username, str) else ""
    with store.LOCK:
        user = find_user(store.current(), username)
        encoded = user["pw_hash"] if user else _dummy_hash()
    ok = verify_password(password if isinstance(password, str) else "", encoded)
    if user and ok and user.get("active", True):
        return user
    return None


# --------------------------------------------------------------------------- login throttling

class RateLimiter:
    """At most `limit` failures per `window` seconds per key."""

    def __init__(self, limit=5, window=900):
        self.limit, self.window = limit, window
        self._fails = {}
        self._lock = threading.Lock()

    def _recent(self, key, now):
        return [t for t in self._fails.get(key, []) if now - t < self.window]

    def blocked(self, key):
        with self._lock:
            return len(self._recent(key, time.time())) >= self.limit

    def fail(self, key):
        with self._lock:
            now = time.time()
            self._fails[key] = self._recent(key, now) + [now]

    def reset(self, key):
        with self._lock:
            self._fails.pop(key, None)


LOGIN_LIMITER = RateLimiter()


# --------------------------------------------------------------------------- sessions
# Persisted (only the sha256 of each token) so a restart does not log everybody out.

_sessions = {}
_sessions_lock = threading.Lock()
_sessions_loaded = False


def _sessions_file():
    return os.path.join(config.path("data_dir"), "sessions.json")


def _token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _ensure_loaded():
    global _sessions_loaded
    if _sessions_loaded:
        return
    _sessions_loaded = True
    try:
        with open(_sessions_file(), encoding="utf-8") as f:
            _sessions.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _save_sessions():
    now = time.time()
    for k in [k for k, s in _sessions.items() if s["expires"] < now]:
        del _sessions[k]
    store.write_atomic(_sessions_file(), json.dumps(_sessions))


def reset_sessions():
    """Forget in-memory sessions (tests)."""
    global _sessions_loaded
    with _sessions_lock:
        _sessions.clear()
        _sessions_loaded = False


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _ensure_loaded()
        _sessions[_token_key(token)] = {
            "user_id": user_id,
            "expires": time.time() + config.CONFIG["session_ttl_days"] * 86400,
        }
        _save_sessions()
    return token


def session_user_id(token):
    if not token:
        return None
    with _sessions_lock:
        _ensure_loaded()
        s = _sessions.get(_token_key(token))
        if not s:
            return None
        if s["expires"] < time.time():
            del _sessions[_token_key(token)]
            return None
        return s["user_id"]


def destroy_session(token):
    with _sessions_lock:
        _ensure_loaded()
        if _sessions.pop(_token_key(token), None):
            _save_sessions()


def destroy_user_sessions(user_id, keep_token=None):
    keep = _token_key(keep_token) if keep_token else None
    with _sessions_lock:
        _ensure_loaded()
        for k in [k for k, s in _sessions.items() if s["user_id"] == user_id and k != keep]:
            del _sessions[k]
        _save_sessions()


# --------------------------------------------------------------------------- authorization

def is_coach(user):
    return user["role"] == "coach"


def is_admin(user):
    return user["role"] == "coach" and bool(user.get("is_admin"))


def require_coach(user):
    if not is_coach(user):
        raise ApiError(403, "forbidden", "coach only")


def require_admin(user):
    if not is_admin(user):
        raise ApiError(403, "forbidden", "admin only")


def can_manage_swimmer(user, swimmer):
    """Coaches manage their own swimmers; admins manage everyone."""
    if swimmer is None or swimmer["role"] != "swimmer":
        return False
    return is_admin(user) or (is_coach(user) and swimmer.get("coach_id") == user["id"])


def can_view_swimmer(user, swimmer):
    if swimmer is None:
        return False
    if user["id"] == swimmer["id"]:
        return True
    return can_manage_swimmer(user, swimmer)


def coach_scope(user):
    """Coach ids whose data `user` may see: all for admins, own for coaches, own coach for swimmers."""
    if is_admin(user):
        return None   # None = everyone
    if is_coach(user):
        return {user["id"]}
    return {user.get("coach_id")}
