"""/api/v1 endpoints.

Each handler gets a Ctx and returns a JSON-serialisable object (or raises ApiError).
Every permission check happens here, server side; the UI only hides what you cannot do.
"""

import base64
import binascii
import csv
import io
import os
import re
from datetime import date, timedelta

from . import acks, auth, config, credits, prefs, reports, schedule, store
from .util import (ApiError, add_minutes, clean_str, local_dt, new_id, now, parse_date, parse_int, parse_time, stamp,
                   today)

ROUTES = []
MAX_RANGE_DAYS = 400


def route(method, pattern, public=False, allow_pw_change=False):
    """Register a handler. Path params are {name} segments."""
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, regex, fn, public, allow_pw_change, pattern))
        return fn
    return deco


class Ctx:
    def __init__(self, user, token, query, body, params, client_ip):
        self.user, self.token, self.query, self.body = user, token, query, body
        self.params, self.client_ip = params, client_ip
        self.set_cookie = None   # (token, max_age) or ("", 0) to clear

    def q(self, name, default=None):
        return self.query.get(name, default)

    def b(self, name, default=None):
        return self.body.get(name, default)


class Raw:
    """Non-JSON response (CSV download, plain text, images)."""

    def __init__(self, body, ctype, filename=None, cache=None):
        self.body, self.ctype, self.filename, self.cache = body, ctype, filename, cache


# --------------------------------------------------------------------------- serializers

def public_user(u, viewer=None):
    out = {k: u.get(k) for k in ("id", "role", "username", "name", "phone", "email", "lang", "active",
                                 "created_at", "must_change_password", "avatar")}
    if u["role"] == "coach":
        out["is_admin"] = bool(u.get("is_admin"))
        out["skip_holidays"] = u.get("skip_holidays", True)
    else:
        out["coach_id"] = u.get("coach_id")
        out["members"] = u.get("members") or []
    if viewer is not None and auth.is_coach(viewer):
        out["notes"] = u.get("notes", "")
    return out


def booking_out(d, b, viewer, n=None):
    out = dict(b)
    out["status"] = credits.effective_status(b, n)
    sw = d["users"].get(b["swimmer_id"], {})
    out["swimmer_name"] = sw.get("name", "?")
    sess = schedule.get_session(d, b["session_key"])
    out["location"] = sess["location"] if sess else b.get("location", "")
    out["title"] = sess["title"] if sess else b.get("title", "")
    out["session_note"] = sess["note"] if sess else ""     # the coach's note for the whole training
    out["can_cancel"] = _can_cancel(b, viewer, n, d)
    out.update(acks.out_fields(d, b, n))
    return out


def _can_cancel(b, viewer, n=None, d=None):
    if auth.is_coach(viewer):
        return b["status"] != "cancelled"
    if b["status"] != "booked":
        return False
    hours = prefs.for_coach(d, b["coach_id"])["cancel_cutoff_hours"] if d else config.CONFIG["cancel_cutoff_hours"]
    cutoff = local_dt(date.fromisoformat(b["date"]), b["start"]) - timedelta(hours=hours)
    return (n or now()) < cutoff


# --------------------------------------------------------------------------- lookups

def _user(d, uid):
    u = d["users"].get(uid)
    if not u:
        raise ApiError(404, "not_found", "user not found")
    return u


def _managed_swimmer(d, viewer, uid):
    u = d["users"].get(uid)
    if not auth.can_manage_swimmer(viewer, u):
        raise ApiError(404, "not_found", "swimmer not found")
    return u


def _owned(d, collection, oid, viewer, what):
    obj = d[collection].get(oid)
    scope = auth.coach_scope(viewer)
    if not obj or (scope is not None and obj["coach_id"] not in scope) or not auth.is_coach(viewer):
        raise ApiError(404, "not_found", "%s not found" % what)
    return obj


def _target_coach(d, viewer, coach_id):
    """Coach that new slots/templates belong to: yourself, or anyone if you are admin."""
    if coach_id and coach_id != viewer["id"]:
        auth.require_admin(viewer)
        c = d["users"].get(coach_id)
        if not c or c["role"] != "coach":
            raise ApiError(400, "invalid_field", "unknown coach", field="coach_id")
        return coach_id
    return viewer["id"]


def _date_range(ctx, default_days=7, max_days=MAX_RANGE_DAYS):
    start = parse_date(ctx.q("from"), "from") if ctx.q("from") else today()
    end = parse_date(ctx.q("to"), "to") if ctx.q("to") else start + timedelta(days=default_days - 1)
    if end < start or (end - start).days >= max_days:
        raise ApiError(400, "invalid_range", "bad date range")
    return start, end


def _scope_for_query(viewer, coach_id):
    scope = auth.coach_scope(viewer)
    if coach_id:
        if scope is not None and coach_id not in scope:
            raise ApiError(403, "forbidden", "not your coach")
        return {coach_id}
    return scope


def _bool(v):
    return v in (True, 1, "1", "true", "yes")


# --------------------------------------------------------------------------- auth

@route("POST", "/login", public=True)
def login(ctx):
    username = ctx.b("username") or ""
    keys = ("u:%s|%s" % (str(username).lower(), ctx.client_ip), "u:%s" % str(username).lower())
    if auth.LOGIN_LIMITER.blocked(keys[0]):
        raise ApiError(429, "too_many_attempts", "too many failed logins, try again later")
    user = auth.authenticate(username, ctx.b("password"))
    if not user:
        auth.LOGIN_LIMITER.fail(keys[0])
        raise ApiError(401, "bad_credentials", "wrong username or password")
    auth.LOGIN_LIMITER.reset(keys[0])
    token = auth.create_session(user["id"])
    ctx.set_cookie = (token, config.CONFIG["session_ttl_days"] * 86400)
    return {"user": public_user(user, user)}


@route("POST", "/logout", public=True)
def logout(ctx):
    if ctx.token:
        auth.destroy_session(ctx.token)
    ctx.set_cookie = ("", 0)
    return {"ok": True}


@route("GET", "/me", allow_pw_change=True)
def me(ctx):
    return {
        "user": public_user(ctx.user, ctx.user),
        "settings": _effective_settings(ctx.user),
        "coach": _coach_contact(ctx.user),
        "today": today().isoformat(),
        "now": now().replace(microsecond=0).isoformat(),
    }


def _coach_contact(user):
    """Swimmers can see how to reach their coach."""
    if user["role"] != "swimmer":
        return None
    with store.LOCK:
        c = store.current()["users"].get(user.get("coach_id"))
    return {k: c.get(k) for k in ("id", "name", "phone", "email", "avatar")} if c else None


def _effective_settings(user):
    with store.LOCK:
        out = prefs.for_user(store.current(), user)
    out.update({k: config.CONFIG[k] for k in ("timezone", "default_lang", "max_capacity", "max_members", "ack_window_days", "checkin_early_minutes")})
    return out


@route("GET", "/me/settings")
def get_coach_settings(ctx):
    auth.require_coach(ctx.user)
    return {"settings": _effective_settings(ctx.user), "limits": {
        k: (list(v) if isinstance(v, tuple) else "bool") for k, v in prefs.COACH_SETTINGS.items()},
        "max_capacity": config.CONFIG["max_capacity"]}


@route("PUT", "/me/settings")
def put_coach_settings(ctx):
    """Coach-level settings; body: any subset of prefs.COACH_SETTINGS."""
    auth.require_coach(ctx.user)

    def fn(d):
        u = d["users"][ctx.user["id"]]
        cur = dict(u.get("settings") or {})
        for k, rule in prefs.COACH_SETTINGS.items():
            if k not in ctx.body:
                continue
            if rule is bool:
                val = bool(ctx.b(k))
            else:
                lo, hi = rule
                val = parse_int(ctx.b(k), k, lo, hi if hi is not None else config.CONFIG["max_capacity"])
            if k == "skip_holidays":
                u["skip_holidays"] = val
            else:
                cur[k] = val
        u["settings"] = cur
        store.audit(d, ctx.user["id"], "settings_changed", u["id"], coach_id=u["id"])
    store.mutate(fn)
    return {"settings": _effective_settings(ctx.user)}


@route("PATCH", "/me", allow_pw_change=True)
def update_me(ctx):
    def fn(d):
        u = d["users"][ctx.user["id"]]
        if "lang" in ctx.body:
            if ctx.b("lang") not in ("bg", "en"):
                raise ApiError(400, "invalid_field", "lang must be bg or en", field="lang")
            u["lang"] = ctx.b("lang")
        for f, n in (("phone", 40), ("email", 120)):
            if f in ctx.body:
                u[f] = clean_str(ctx.b(f), f, n)
        if auth.is_coach(u):
            if "name" in ctx.body:
                u["name"] = clean_str(ctx.b("name"), "name", 100, required=True)
            if "skip_holidays" in ctx.body:
                u["skip_holidays"] = bool(ctx.b("skip_holidays"))
        return public_user(u, u)
    return {"user": store.mutate(fn)}


@route("POST", "/me/password", allow_pw_change=True)
def change_password(ctx):
    new = ctx.b("new_password")
    auth.validate_new_password(new)
    if not auth.verify_password(ctx.b("current_password") or "", ctx.user["pw_hash"]):
        raise ApiError(400, "wrong_password", "current password is wrong")
    if new == ctx.b("current_password"):
        raise ApiError(400, "password_unchanged", "new password must differ from the current one")

    def fn(d):
        u = d["users"][ctx.user["id"]]
        u["pw_hash"] = auth.hash_password(new)
        u["must_change_password"] = False
        store.audit(d, ctx.user["id"], "password_changed", u["id"], coach_id=_coach_of(u))
    store.mutate(fn)
    auth.destroy_user_sessions(ctx.user["id"], keep_token=ctx.token)
    return {"ok": True}


def _coach_of(u):
    return u["id"] if u["role"] == "coach" else u.get("coach_id")


# --------------------------------------------------------------------------- users

def _clean_members(value):
    """Names on a joint account: [] for a normal one, else 2..max_members unique names."""
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise ApiError(400, "invalid_field", "members must be a list", field="members")
    names = []
    for v in value:
        name = clean_str(v, "members", 40)
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    if len(names) > config.CONFIG["max_members"]:
        raise ApiError(400, "too_many_members", "at most %d people per account" % config.CONFIG["max_members"])
    return names if len(names) > 1 else []


def _new_user(d, role, body, creator, coach_id=None):
    username = auth.normalise_username(body.get("username"))
    if auth.find_user(d, username):
        raise ApiError(409, "username_taken", "username already exists")
    password = auth.generate_password()
    u = {
        "id": new_id("u"),
        "role": role,
        "username": username,
        "name": clean_str(body.get("name"), "name", 100, required=True),
        "phone": clean_str(body.get("phone"), "phone", 40),
        "email": clean_str(body.get("email"), "email", 120),
        "notes": clean_str(body.get("notes"), "notes", 2000),
        "lang": body.get("lang") if body.get("lang") in ("bg", "en") else config.CONFIG["default_lang"],
        "pw_hash": auth.hash_password(password),
        "must_change_password": True,
        "active": True,
        "created_at": stamp(),
        "created_by": creator["id"] if creator else None,
    }
    if role == "coach":
        u["is_admin"] = bool(body.get("is_admin"))
        u["skip_holidays"] = True
    else:
        u["coach_id"] = coach_id
        u["members"] = _clean_members(body.get("members"))
    d["users"][u["id"]] = u
    store.audit(d, creator["id"] if creator else "cli", "user_created", u["id"],
                coach_id=_coach_of(u), role=role, username=username)
    return u, password


def create_user_cli(role, username, name, is_admin=False, coach_id=None):
    """Used by server.py --add-coach / --add-swimmer."""
    body = {"username": username, "name": name, "is_admin": is_admin}
    return store.mutate(lambda d: _new_user(d, role, body, None, coach_id))


@route("GET", "/swimmers")
def list_swimmers(ctx):
    auth.require_coach(ctx.user)
    scope = _scope_for_query(ctx.user, ctx.q("coach_id"))
    include_inactive = _bool(ctx.q("include_inactive"))
    n = now()
    with store.LOCK:
        d = store.current()
        out = []
        for u in d["users"].values():
            if u["role"] != "swimmer" or (scope is not None and u.get("coach_id") not in scope):
                continue
            if not u.get("active", True) and not include_inactive:
                continue
            bal = credits.balance(d, u["id"], n)
            nb = credits.next_booking(d, u["id"], n)
            out.append({**public_user(u, ctx.user),
                        "coach_name": d["users"].get(u.get("coach_id"), {}).get("name", ""),
                        "balance": {k: v for k, v in bal.items() if k != "packages"},
                        "last_visit": credits.last_visit(d, u["id"], n),
                        "next_booking": {"date": nb["date"], "start": nb["start"]} if nb else None})
    out.sort(key=lambda u: u["name"].lower())
    return {"swimmers": out}


@route("POST", "/swimmers")
def create_swimmer(ctx):
    auth.require_coach(ctx.user)

    def fn(d):
        coach_id = _target_coach(d, ctx.user, ctx.b("coach_id"))
        return _new_user(d, "swimmer", ctx.body, ctx.user, coach_id)
    u, password = store.mutate(fn)
    return {"user": public_user(u, ctx.user), "password": password}


@route("GET", "/coaches")
def list_coaches(ctx):
    auth.require_coach(ctx.user)
    with store.LOCK:
        d = store.current()
        out = [public_user(u, ctx.user) for u in d["users"].values()
               if u["role"] == "coach" and (auth.is_admin(ctx.user) or u["id"] == ctx.user["id"])]
    return {"coaches": sorted(out, key=lambda u: u["name"].lower())}


@route("POST", "/coaches")
def create_coach(ctx):
    auth.require_admin(ctx.user)
    u, password = store.mutate(lambda d: _new_user(d, "coach", ctx.body, ctx.user))
    return {"user": public_user(u, ctx.user), "password": password}


def _can_edit_user(viewer, u):
    if u["role"] == "swimmer":
        return auth.can_manage_swimmer(viewer, u)
    return auth.is_admin(viewer)


@route("GET", "/users/{uid}")
def get_user(ctx):
    n = now()
    with store.LOCK:
        d = store.current()
        u = d["users"].get(ctx.params["uid"])
        if not u or not (u["id"] == ctx.user["id"] or _can_edit_user(ctx.user, u)):
            raise ApiError(404, "not_found", "user not found")
        out = {"user": public_user(u, ctx.user)}
        if u["role"] == "swimmer":
            out["balance"] = credits.balance(d, u["id"], n)
            bs = sorted(credits.swimmer_bookings(d, u["id"]), key=lambda b: (b["date"], b["start"]), reverse=True)
            out["bookings"] = [booking_out(d, b, ctx.user, n) for b in bs]
            out["coach_name"] = d["users"].get(u.get("coach_id"), {}).get("name", "")
            if auth.is_coach(ctx.user):
                out["comments"] = _swimmer_comments(d, u["id"])
    return out


@route("PATCH", "/users/{uid}")
def update_user(ctx):
    def fn(d):
        u = d["users"].get(ctx.params["uid"])
        if not u or not _can_edit_user(ctx.user, u):
            raise ApiError(404, "not_found", "user not found")
        changed = []
        if "username" in ctx.body:
            un = auth.normalise_username(ctx.b("username"))
            other = auth.find_user(d, un)
            if other and other["id"] != u["id"]:
                raise ApiError(409, "username_taken", "username already exists")
            u["username"] = un
            changed.append("username")
        for f, n, req in (("name", 100, True), ("phone", 40, False), ("email", 120, False), ("notes", 2000, False)):
            if f in ctx.body:
                u[f] = clean_str(ctx.b(f), f, n, required=req)
                changed.append(f)
        if "members" in ctx.body and u["role"] == "swimmer":
            u["members"] = _clean_members(ctx.b("members"))
            changed.append("members")
        if "lang" in ctx.body and ctx.b("lang") in ("bg", "en"):
            u["lang"] = ctx.b("lang")
        if "coach_id" in ctx.body and u["role"] == "swimmer":
            auth.require_admin(ctx.user)
            c = d["users"].get(ctx.b("coach_id"))
            if not c or c["role"] != "coach":
                raise ApiError(400, "invalid_field", "unknown coach", field="coach_id")
            u["coach_id"] = c["id"]
            changed.append("coach_id")
        if "is_admin" in ctx.body and u["role"] == "coach":
            auth.require_admin(ctx.user)
            if u["id"] == ctx.user["id"] and not ctx.b("is_admin"):
                raise ApiError(400, "cannot_demote_self", "you cannot remove your own admin rights")
            u["is_admin"] = bool(ctx.b("is_admin"))
            changed.append("is_admin")
        deactivated = False
        if "active" in ctx.body:
            active = bool(ctx.b("active"))
            if u["id"] == ctx.user["id"] and not active:
                raise ApiError(400, "cannot_deactivate_self", "you cannot deactivate yourself")
            deactivated = u.get("active", True) and not active
            u["active"] = active
            changed.append("active")
            if deactivated and u["role"] == "swimmer":
                _cancel_future_bookings(d, lambda b: b["swimmer_id"] == u["id"], ctx.user, "deactivated")
        store.audit(d, ctx.user["id"], "user_updated", u["id"], coach_id=_coach_of(u), fields=changed)
        return u, deactivated
    u, deactivated = store.mutate(fn)
    if deactivated:
        auth.destroy_user_sessions(u["id"])
    return {"user": public_user(u, ctx.user)}


@route("POST", "/users/{uid}/reset-password")
def reset_password(ctx):
    password = auth.generate_password()

    def fn(d):
        u = d["users"].get(ctx.params["uid"])
        if not u or not _can_edit_user(ctx.user, u) or u["id"] == ctx.user["id"]:
            raise ApiError(404, "not_found", "user not found")
        u["pw_hash"] = auth.hash_password(password)
        u["must_change_password"] = True
        store.audit(d, ctx.user["id"], "password_reset", u["id"], coach_id=_coach_of(u))
        return u
    u = store.mutate(fn)
    auth.destroy_user_sessions(u["id"])
    return {"user": public_user(u, ctx.user), "password": password}


# --------------------------------------------------------------------------- avatars
# Stored as files in <data_dir>/avatars/<user id>.<ext>; the user record keeps a version
# number so the URL changes (and caches refresh) whenever the picture does.

AVATAR_TYPES = {"image/jpeg": ("jpg", b"\xff\xd8\xff"), "image/png": ("png", b"\x89PNG"),
                "image/webp": ("webp", b"RIFF")}
AVATAR_MAX = 200 * 1024


def _avatar_dir():
    return os.path.join(config.path("data_dir"), "avatars")


def _avatar_file(uid):
    for ctype, (ext, _) in AVATAR_TYPES.items():
        path = os.path.join(_avatar_dir(), "%s.%s" % (uid, ext))
        if os.path.exists(path):
            return path, ctype
    return None, None


def _can_see_avatar(viewer, u):
    """Yourself, your own swimmers (or everyone for admins), fellow coaches, and a swimmer's own coach."""
    return (u["id"] == viewer["id"] or _can_edit_user(viewer, u)
            or (auth.is_coach(viewer) and u["role"] == "coach")
            or u["id"] == viewer.get("coach_id"))


def _avatar_target(d, viewer, uid):
    u = d["users"].get(uid)
    if not u or not (u["id"] == viewer["id"] or _can_edit_user(viewer, u)):
        raise ApiError(404, "not_found", "user not found")
    return u


@route("GET", "/users/{uid}/avatar")
def get_avatar(ctx):
    with store.LOCK:
        u = store.current()["users"].get(ctx.params["uid"])
        if not u or not _can_see_avatar(ctx.user, u):
            raise ApiError(404, "not_found", "no avatar")
    path, ctype = _avatar_file(u["id"])
    if not path:
        raise ApiError(404, "not_found", "no avatar")
    with open(path, "rb") as f:
        # the URL carries ?v=<version>, so the browser may keep it
        return Raw(f.read(), ctype, cache="private, max-age=31536000, immutable")


@route("POST", "/users/{uid}/avatar")
def set_avatar(ctx):
    """Body: {"image": "data:image/jpeg;base64,..."} - the UI resizes to a small square first."""
    m = re.match(r"^data:(image/[a-z]+);base64,(.+)$", ctx.b("image") or "", re.S)
    if not m or m.group(1) not in AVATAR_TYPES:
        raise ApiError(400, "invalid_image", "a JPEG, PNG or WebP data URL is expected")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (ValueError, binascii.Error):
        raise ApiError(400, "invalid_image", "bad base64")
    ext, magic = AVATAR_TYPES[m.group(1)]
    if not raw.startswith(magic):
        raise ApiError(400, "invalid_image", "file content does not match its type")
    if len(raw) > AVATAR_MAX:
        raise ApiError(413, "image_too_large", "picture too large")

    def fn(d):
        u = _avatar_target(d, ctx.user, ctx.params["uid"])
        old, _ = _avatar_file(u["id"])
        if old:
            os.remove(old)
        store.write_atomic(os.path.join(_avatar_dir(), "%s.%s" % (u["id"], ext)), raw)
        u["avatar"] = (u.get("avatar") or 0) + 1
        store.audit(d, ctx.user["id"], "avatar_changed", u["id"], coach_id=_coach_of(u))
        return public_user(u, ctx.user)
    return {"user": store.mutate(fn)}


@route("DELETE", "/users/{uid}/avatar")
def delete_avatar(ctx):
    def fn(d):
        u = _avatar_target(d, ctx.user, ctx.params["uid"])
        old, _ = _avatar_file(u["id"])
        if old:
            os.remove(old)
        u["avatar"] = None
        return public_user(u, ctx.user)
    return {"user": store.mutate(fn)}


@route("GET", "/holidays")
def holidays(ctx):
    start, end = _date_range(ctx, default_days=42, max_days=400)
    return {"holidays": schedule.holidays_between(start, end)}


# --------------------------------------------------------------------------- coach absence

@route("GET", "/absences")
def list_absences(ctx):
    with store.LOCK:
        d = store.current()
        scope = auth.coach_scope(ctx.user)
        out = [a for a in d["absences"].values() if scope is None or a["coach_id"] in scope]
    return {"absences": sorted(out, key=lambda a: a["from"], reverse=True)}


@route("POST", "/absences")
def create_absence(ctx):
    """{from, to, reason, extend_cards (default true)}: cancels every training of the coach in
    that range (visits go back, swimmers see the reason) and moves active cards' expiry later."""
    auth.require_coach(ctx.user)
    start, end = parse_date(ctx.b("from"), "from"), parse_date(ctx.b("to"), "to")
    if end < start or (end - start).days > 180:
        raise ApiError(400, "invalid_range", "bad date range")
    reason = clean_str(ctx.b("reason"), "reason", 200)
    extend = ctx.b("extend_cards", True) is not False

    def fn(d):
        coach_id = ctx.user["id"]
        aid = new_id("ab")
        days = (end - start).days + 1
        note = ("🌴 " + reason) if reason else "🌴"
        changed, cancelled = {}, 0
        for sess in schedule.occurrences(d, start, end, {coach_id}):
            if sess["cancelled"]:
                continue
            before = d["overrides"].get(sess["key"])
            changed[sess["key"]] = before
            d["overrides"][sess["key"]] = dict(before or {}, cancelled=True, note=note, absence=aid)
            cancelled += _cancel_future_bookings(d, lambda b, k=sess["key"]: b["session_key"] == k, ctx.user, "session_cancelled")
        extended = []
        if extend:
            for p in d["packages"].values():
                if p.get("coach_id") == coach_id and p.get("valid_until") and p["valid_until"] >= start.isoformat():
                    extended.append({"id": p["id"], "before": p["valid_until"]})
                    p["valid_until"] = (date.fromisoformat(p["valid_until"]) + timedelta(days=days)).isoformat()
        a = {"id": aid, "coach_id": coach_id, "from": start.isoformat(), "to": end.isoformat(), "reason": reason,
             "days": days, "overrides": changed, "extended": extended, "cancelled_bookings": cancelled, "created_at": stamp()}
        d["absences"][aid] = a
        store.audit(d, ctx.user["id"], "absence_created", aid, coach_id=coach_id, days=days, cancelled=cancelled,
                    cards=len(extended))
        return {k: v for k, v in a.items() if k != "overrides"} | {"sessions": len(changed)}
    return {"absence": store.mutate(fn)}


@route("DELETE", "/absences/{aid}")
def delete_absence(ctx):
    """Undo an absence: trainings back on the schedule (bookings restored where possible), card dates back."""
    auth.require_coach(ctx.user)

    def fn(d):
        a = d["absences"].get(ctx.params["aid"])
        if not a or a["coach_id"] != ctx.user["id"]:
            raise ApiError(404, "not_found", "absence not found")
        restored = 0
        for key, before in a["overrides"].items():
            if (d["overrides"].get(key) or {}).get("absence") != a["id"]:
                continue                    # changed again since - leave the coach's later edit alone
            if before:
                d["overrides"][key] = before
            else:
                d["overrides"].pop(key, None)
            sess = schedule.get_session(d, key)
            if sess and not sess["cancelled"]:
                restored += _restore_bookings(d, sess)
        for e in a["extended"]:
            p = d["packages"].get(e["id"])
            if p:
                p["valid_until"] = e["before"]
        del d["absences"][a["id"]]
        store.audit(d, ctx.user["id"], "absence_removed", a["id"], coach_id=a["coach_id"], restored=restored)
        return restored
    return {"ok": True, "restored_bookings": store.mutate(fn)}


# --------------------------------------------------------------------------- messages to swimmers

@route("POST", "/messages")
def send_message(ctx):
    """{text, audience: all | day, date? (for day)} - shown to the swimmers as a banner until read."""
    auth.require_coach(ctx.user)
    text = clean_str(ctx.b("text"), "text", 1000, required=True)
    audience = ctx.b("audience") or "all"
    if audience not in ("all", "day"):
        raise ApiError(400, "invalid_field", "unknown audience", field="audience")
    day = parse_date(ctx.b("date"), "date").isoformat() if audience == "day" else None

    def fn(d):
        coach_id = ctx.user["id"]
        if audience == "all":
            rcpt = {u["id"] for u in d["users"].values()
                    if u["role"] == "swimmer" and u.get("coach_id") == coach_id and u.get("active", True)}
        else:
            rcpt = {b["swimmer_id"] for b in d["bookings"].values()
                    if b["date"] == day and b["coach_id"] == coach_id and b["status"] != "cancelled"}
        if not rcpt:
            raise ApiError(409, "no_recipients", "nobody to send to")
        m = {"id": new_id("m"), "coach_id": coach_id, "text": text, "audience": audience, "date": day,
             "recipients": sorted(rcpt), "read": [], "created_at": stamp()}
        d["messages"][m["id"]] = m
        store.audit(d, coach_id, "message_sent", m["id"], coach_id=coach_id, recipients=len(rcpt))
        return m
    return {"message": store.mutate(fn)}


@route("GET", "/messages")
def list_messages(ctx):
    """Coach: what they sent (with read counts). Swimmer: what they received (last 60 days)."""
    cutoff = (now() - timedelta(days=60)).isoformat()
    with store.LOCK:
        d = store.current()
        names = {u["id"]: u["name"] for u in d["users"].values()}
        if auth.is_coach(ctx.user):
            scope = auth.coach_scope(ctx.user)
            out = [dict(m, read_count=len(m["read"]), recipient_count=len(m["recipients"]),
                        unread_names=[names.get(r, "?") for r in m["recipients"] if r not in m["read"]])
                   for m in d["messages"].values() if scope is None or m["coach_id"] in scope]
        else:
            out = [{"id": m["id"], "text": m["text"], "created_at": m["created_at"], "from": names.get(m["coach_id"], ""),
                    "read": ctx.user["id"] in m["read"]}
                   for m in d["messages"].values() if ctx.user["id"] in m["recipients"] and m["created_at"] >= cutoff]
    return {"messages": sorted(out, key=lambda m: m["created_at"], reverse=True)}


@route("POST", "/messages/{mid}/read")
def read_message(ctx):
    def fn(d):
        m = d["messages"].get(ctx.params["mid"])
        if not m or ctx.user["id"] not in m["recipients"]:
            raise ApiError(404, "not_found", "message not found")
        if ctx.user["id"] not in m["read"]:
            m["read"].append(ctx.user["id"])
    store.mutate(fn)
    return {"ok": True}


# --------------------------------------------------------------------------- insights for the coach

@route("GET", "/insights")
def insights(ctx):
    auth.require_coach(ctx.user)
    n = now()
    today_iso = n.date().isoformat()
    soon = (n.date() + timedelta(days=14)).isoformat()
    three_weeks = (n.date() - timedelta(days=21)).isoformat()
    with store.LOCK:
        d = store.current()
        scope = auth.coach_scope(ctx.user)
        swimmers = [u for u in d["users"].values() if u["role"] == "swimmer" and u.get("active", True)
                    and (scope is None or u.get("coach_id") in scope)]
        inactive, expiring, low = [], [], []
        for u in swimmers:
            bal = credits.balance(d, u["id"], n)
            last = credits.last_visit(d, u["id"], n)
            nxt = credits.next_booking(d, u["id"], n)
            row = {"id": u["id"], "name": u["name"], "avatar": u.get("avatar"), "phone": u.get("phone", ""),
                   "available": bal["available"], "last_visit": last}
            if (not last or last < three_weeks) and not nxt:
                inactive.append(row)
            for e in bal["expiry"]:
                if e["date"] and today_iso <= e["date"] <= soon:
                    expiring.append(dict(row, expires=e["date"], remaining=e["remaining"]))
            if bal["total"] and bal["low"]:
                low.append(row)
        # the last 8 weeks: when do people come, and how full are the slots by weekday
        start = n.date() - timedelta(weeks=8)
        coaches = scope
        per_time, per_wd_att, per_wd_cap = {}, [0] * 7, [0] * 7
        for s in schedule.occurrences(d, start, n.date(), coaches):
            if s["cancelled"] or schedule.session_start(s) > n:
                continue
            per_wd_cap[date.fromisoformat(s["date"]).weekday()] += s["capacity"]
        for b in d["bookings"].values():
            if b["date"] < start.isoformat() or b["date"] > today_iso or (coaches is not None and b["coach_id"] not in coaches):
                continue
            if credits.effective_status(b, n) == "attended":
                per_time[b["start"]] = per_time.get(b["start"], 0) + 1
                per_wd_att[date.fromisoformat(b["date"]).weekday()] += 1
    return {
        "inactive": sorted(inactive, key=lambda r: r["last_visit"] or ""),
        "expiring": sorted(expiring, key=lambda r: r["expires"]),
        "low": sorted(low, key=lambda r: r["available"]),
        "busiest": sorted(per_time.items(), key=lambda kv: -kv[1])[:5],
        "occupancy_by_weekday": [round(a / c, 3) if c else None for a, c in zip(per_wd_att, per_wd_cap)],
    }


# --------------------------------------------------------------------------- comments (coach only)

def _comment_out(d, c):
    author = d["users"].get(c["author_id"], {})
    return dict(c, author_name=author.get("name", "?"), author_avatar=author.get("avatar"))


def _swimmer_comments(d, swimmer_id):
    cs = [_comment_out(d, c) for c in d["comments"].values() if c["swimmer_id"] == swimmer_id]
    return sorted(cs, key=lambda c: c["created_at"], reverse=True)


@route("GET", "/users/{uid}/comments")
def list_comments(ctx):
    auth.require_coach(ctx.user)
    with store.LOCK:
        d = store.current()
        sw = _managed_swimmer(d, ctx.user, ctx.params["uid"])
        return {"comments": _swimmer_comments(d, sw["id"])}


@route("POST", "/users/{uid}/comments")
def add_comment(ctx):
    auth.require_coach(ctx.user)

    def fn(d):
        sw = _managed_swimmer(d, ctx.user, ctx.params["uid"])
        c = {"id": new_id("c"), "swimmer_id": sw["id"], "coach_id": sw.get("coach_id"), "author_id": ctx.user["id"],
             "text": clean_str(ctx.b("text"), "text", 4000, required=True), "created_at": stamp(), "edited_at": None}
        if ctx.b("session_key"):
            sess = schedule.get_session(d, ctx.b("session_key"))
            if not sess:
                raise ApiError(404, "session_not_found", "session not found")
            c.update(session_key=sess["key"], session_date=sess["date"], session_start=sess["start"])
        d["comments"][c["id"]] = c
        store.audit(d, ctx.user["id"], "comment_added", sw["id"], coach_id=sw.get("coach_id"))
        return {"comment": _comment_out(d, c)}
    return store.mutate(fn)


def _own_comment(d, viewer, cid):
    c = d["comments"].get(cid)
    if not c or not auth.can_manage_swimmer(viewer, d["users"].get(c["swimmer_id"])):
        raise ApiError(404, "not_found", "comment not found")
    if c["author_id"] != viewer["id"] and not auth.is_admin(viewer):
        raise ApiError(403, "forbidden", "only the author can change a comment")
    return c


@route("PATCH", "/comments/{cid}")
def edit_comment(ctx):
    auth.require_coach(ctx.user)

    def fn(d):
        c = _own_comment(d, ctx.user, ctx.params["cid"])
        c["text"] = clean_str(ctx.b("text"), "text", 4000, required=True)
        c["edited_at"] = stamp()
        return {"comment": _comment_out(d, c)}
    return store.mutate(fn)


@route("DELETE", "/comments/{cid}")
def delete_comment(ctx):
    auth.require_coach(ctx.user)

    def fn(d):
        c = _own_comment(d, ctx.user, ctx.params["cid"])
        del d["comments"][c["id"]]
        store.audit(d, ctx.user["id"], "comment_deleted", c["swimmer_id"], coach_id=c["coach_id"])
        return {"ok": True}
    return store.mutate(fn)


# --------------------------------------------------------------------------- sessions & schedule

def _occupying(b, n):
    return credits.effective_status(b, n) in ("booked", "attended", "no_show")


# Bookings cancelled because the coach cancelled/removed the training (not by the swimmer).
COACH_CANCEL_REASONS = {"session_cancelled", "availability_removed", "slot_removed", "session_removed",
                        "schedule_changed"}


def _bookings_by_key(d, keys, cancelled_by_coach=False):
    out = {}
    for b in d["bookings"].values():
        if b["session_key"] not in keys:
            continue
        if (b["status"] != "cancelled" or b.get("reason") == "pending_approval") if not cancelled_by_coach else \
                (b["status"] == "cancelled" and b.get("reason") in COACH_CANCEL_REASONS):
            out.setdefault(b["session_key"], []).append(b)
    return out


def _attendee_detail(d, swimmer_id, key, n):
    u = d["users"].get(swimmer_id, {})
    bal = credits.balance(d, swimmer_id, n)
    comments = _swimmer_comments(d, swimmer_id)
    return {
        "phone": u.get("phone", ""),
        "notes": u.get("notes", ""),
        "credits": {k: bal[k] for k in ("total", "used", "reserved", "available", "unpaid", "low")},
        "last_comment": next((c for c in comments if c.get("session_key") != key), None),
        "session_comments": [c for c in comments if c.get("session_key") == key],
    }


@route("GET", "/sessions")
def list_sessions(ctx):
    start, end = _date_range(ctx, default_days=7, max_days=62)
    detail = _bool(ctx.q("detail")) and auth.is_coach(ctx.user)
    n = now()
    viewer = ctx.user
    with store.LOCK:
        d = store.current()
        scope = _scope_for_query(viewer, ctx.q("coach_id"))
        sessions = schedule.occurrences(d, start, end, scope)
        coach_prefs = {}

        def pref(coach_id):
            if coach_id not in coach_prefs:
                coach_prefs[coach_id] = prefs.for_coach(d, coach_id)
            return coach_prefs[coach_id]
        keys = {s["key"] for s in sessions}
        by_key = _bookings_by_key(d, keys)
        called_off = _bookings_by_key(d, keys, cancelled_by_coach=True)
        waits = {}
        for w in d["waitlist"].values():
            if w["session_key"] in keys:
                waits.setdefault(w["session_key"], []).append(w)
        out = []
        for s in sessions:
            bs = by_key.get(s["key"], [])
            taken = sum(1 for b in bs if _occupying(b, n))
            s = dict(s, booked=taken, free=max(0, s["capacity"] - taken),
                     started=schedule.session_start(s) <= n)
            if auth.is_coach(viewer):
                s["attendees"] = sorted(({"booking_id": b["id"], "swimmer_id": b["swimmer_id"],
                                          "member": b.get("member"), "note": b.get("note"),
                                          "protected": acks.is_protected(b, n), "ack": b.get("ack"),
                                          "pending": b.get("pending"),
                                          "swimmer_note": b.get("swimmer_note"),
                                          "avatar": d["users"].get(b["swimmer_id"], {}).get("avatar"),
                                          "name": d["users"].get(b["swimmer_id"], {}).get("name", "?"),
                                          "status": credits.effective_status(b, n)} for b in bs),
                                        key=lambda a: (a["name"].lower(), a["member"] or ""))
                if detail:
                    for a in s["attendees"]:
                        a.update(_attendee_detail(d, a["swimmer_id"], s["key"], n))
                s["called_off"] = sorted(d["users"].get(b["swimmer_id"], {}).get("name", "?")
                                         for b in called_off.get(s["key"], []))
                s["waitlist"] = [d["users"].get(w["swimmer_id"], {}).get("name", "?") + (" (%s)" % w["member"] if w.get("member") else "")
                                 for w in sorted(waits.get(s["key"], []), key=lambda w: w["created_at"])]
                for a in s["attendees"]:
                    raw = d["bookings"][a["booking_id"]]
                    a["checked"] = raw["status"] != "booked" and not raw.get("auto")    # explicitly marked by the coach
            else:
                own = [b for b in bs if b["swimmer_id"] == viewer["id"]]
                mine = own[0] if own else None
                s["my_bookings"] = [{"id": b["id"], "member": b.get("member"), "status": credits.effective_status(b, n),
                                     "note": b.get("note"), "swimmer_note": b.get("swimmer_note"),
                                     "can_cancel": _can_cancel(b, viewer, n, d)} for b in own]
                members = d["users"][viewer["id"]].get("members") or []
                s["free_members"] = [m for m in members if m not in {b.get("member") for b in own}]
                if mine:
                    s["my_booking"] = {"id": mine["id"], "status": credits.effective_status(mine, n),
                                       "member": mine.get("member"), "can_cancel": _can_cancel(mine, viewer, n, d)}
                else:
                    gone = next((b for b in called_off.get(s["key"], []) if b["swimmer_id"] == viewer["id"]), None)
                    s["my_booking"] = ({"id": gone["id"], "status": "cancelled", "reason": gone["reason"],
                                        "by_coach": True, "can_cancel": False} if gone else None)
                room_for_me = s["free_members"] if members else not own
                mine_w = sorted(waits.get(s["key"], []), key=lambda w: w["created_at"])
                s["my_waitlist"] = [{"id": w["id"], "member": w.get("member"), "position": i + 1}
                                    for i, w in enumerate(mine_w) if w["swimmer_id"] == viewer["id"]]
                p = pref(s["coach_id"])
                horizon = today() + timedelta(days=p["booking_horizon_days"])
                s["can_book"] = (p["swimmers_can_book"] and bool(room_for_me) and not s["cancelled"] and not s["started"]
                                 and s["free"] > 0 and date.fromisoformat(s["date"]) <= horizon)
            out.append(s)
    return {"from": start.isoformat(), "to": end.isoformat(), "sessions": out,
            "holidays": schedule.holidays_between(start, end)}


def _capacity(value, default=None):
    if value in (None, "") and default is not None:
        return default
    return parse_int(value, "capacity", 1, config.CONFIG["max_capacity"])


def _session_fields(body, partial):
    """Fields of a one-off session."""
    out = {}
    if not partial or "start" in body:
        out["start"] = parse_time(body.get("start"), "start")
    if not partial or "duration_min" in body or "slot_minutes" in body:
        out["duration_min"] = parse_int(body.get("duration_min") or body.get("slot_minutes")
                                        or config.CONFIG["default_slot_minutes"], "duration_min", 15, 480)
    if not partial or "capacity" in body:
        out["capacity"] = _capacity(body.get("capacity"), config.CONFIG["max_capacity"])
    for f, n in (("location", 100), ("title", 100)):
        if not partial or f in body:
            out[f] = clean_str(body.get(f), f, n)
    return out


TIMING = ("weekday", "start", "end", "slot_minutes")


def _window_fields(body, partial):
    out = {}
    if not partial or "start" in body:
        out["start"] = parse_time(body.get("start"), "start")
    if not partial or "end" in body:
        out["end"] = parse_time(body.get("end"), "end")
    if not partial or "slot_minutes" in body:
        out["slot_minutes"] = parse_int(body.get("slot_minutes") or config.CONFIG["default_slot_minutes"],
                                        "slot_minutes", 15, 240)
    if not partial or "capacity" in body:
        out["capacity"] = _capacity(body.get("capacity"), config.CONFIG["max_capacity"])
    if partial and "weekday" in body:
        out["weekday"] = parse_int(body.get("weekday"), "weekday", 0, 6)
    for f, n in (("location", 100), ("title", 100)):
        if not partial or f in body:
            out[f] = clean_str(body.get(f), f, n)
    return out


def _check_window(w):
    if schedule.minutes(w["end"]) - schedule.minutes(w["start"]) < w["slot_minutes"]:
        raise ApiError(400, "window_too_short", "the time range must fit at least one slot", field="end")


def _window_out(d, w):
    return dict(w, coach_name=d["users"].get(w["coach_id"], {}).get("name", ""),
                slots=len(schedule.slot_starts(w)))


def _window_in_use_from(w, n):
    """First date a change to this window can apply to without rewriting history."""
    starts = schedule.slot_starts(w)
    started_today = (n.weekday() == w["weekday"] and starts and local_dt(n.date(), starts[0]) <= n
                     and schedule.runs_on(w, n.date()))
    return n.date() + timedelta(days=1 if started_today else 0)


def _rehome_bookings(d, old_id, new_id_, actor):
    """Future bookings of a changed window: move them to the same time in the new version,
    or cancel them if that time no longer exists. Returns (moved, cancelled)."""
    n, moved, cancelled = now(), 0, 0
    for b in d["bookings"].values():
        if b["status"] != "booked" or not b["session_key"].startswith(old_id + "@") or \
                local_dt(date.fromisoformat(b["date"]), b["start"]) <= n:
            continue
        if schedule.get_session(d, b["session_key"]):
            continue
        if new_id_:
            key = new_id_ + "@" + b["session_key"].split("@", 1)[1]
            sess = schedule.get_session(d, key)
            if sess:
                b.update(session_key=key, start=sess["start"], end=sess["end"], updated_at=stamp())
                moved += 1
                continue
        b.update(status="cancelled", updated_at=stamp(), by=actor["id"], reason="schedule_changed")
        cancelled += 1
    return moved, cancelled


@route("GET", "/availability")
def list_availability(ctx):
    auth.require_coach(ctx.user)
    scope = _scope_for_query(ctx.user, ctx.q("coach_id"))
    with store.LOCK:
        d = store.current()
        out = [_window_out(d, w) for w in d["availability"].values()
               if (scope is None or w["coach_id"] in scope) and (w.get("active", True) or _bool(ctx.q("all")))]
        p = prefs.for_coach(d, ctx.user["id"])
    return {"availability": sorted(out, key=lambda w: (w["weekday"], w["start"])),
            "defaults": {"slot_minutes": p["default_slot_minutes"],
                         "capacity": p["default_capacity"], "max_capacity": config.CONFIG["max_capacity"]}}


@route("POST", "/availability")
def create_availability(ctx):
    """One window per weekday in `weekdays` (or a single `weekday`)."""
    auth.require_coach(ctx.user)
    days = ctx.b("weekdays")
    if days is None:
        days = [ctx.b("weekday")]
    if not isinstance(days, list) or not days:
        raise ApiError(400, "invalid_field", "weekdays must be a non-empty list", field="weekdays")
    days = sorted({parse_int(x, "weekdays", 0, 6) for x in days})

    def fn(d):
        coach_id = _target_coach(d, ctx.user, ctx.b("coach_id"))
        fields = _window_fields(ctx.body, False)
        valid_from = parse_date(ctx.b("valid_from"), "valid_from").isoformat() if ctx.b("valid_from") \
            else today().isoformat()
        valid_to = parse_date(ctx.b("valid_to"), "valid_to").isoformat() if ctx.b("valid_to") else None
        out = []
        for wd in days:
            w = {"id": new_id("a"), "coach_id": coach_id, "weekday": wd, **fields, "valid_from": valid_from,
                 "valid_to": valid_to, "active": True, "created_at": stamp()}
            _check_window(w)
            d["availability"][w["id"]] = w
            store.audit(d, ctx.user["id"], "availability_created", w["id"], coach_id=coach_id,
                        weekday=wd, start=w["start"], end=w["end"])
            out.append(_window_out(d, w))
        return out
    return {"availability": store.mutate(fn)}


@route("PATCH", "/availability/{aid}")
def update_availability(ctx):
    """Changing days/hours/slot length starts a new version from today, so past sessions
    keep their history; upcoming bookings move along when their time still exists."""
    def fn(d):
        w = _owned(d, "availability", ctx.params["aid"], ctx.user, "availability")
        fields = _window_fields(ctx.body, True)
        if "valid_to" in ctx.body:
            fields["valid_to"] = parse_date(ctx.b("valid_to"), "valid_to").isoformat() if ctx.b("valid_to") else None
        timing_changed = any(k in fields and fields[k] != w[k] for k in TIMING)
        effective = _window_in_use_from(w, now()).isoformat()
        target = w
        if timing_changed and (w.get("valid_from") or "") < effective:
            new = dict(w, **fields, id=new_id("a"), valid_from=effective, created_at=stamp())
            w["valid_to"] = (date.fromisoformat(effective) - timedelta(days=1)).isoformat()
            w["active"] = False
            _check_window(new)
            d["availability"][new["id"]] = new
            target = new
        else:
            w.update(fields)
            _check_window(w)
        moved, cancelled = _rehome_bookings(d, w["id"], target["id"] if target is not w else None, ctx.user)
        store.audit(d, ctx.user["id"], "availability_updated", target["id"], coach_id=w["coach_id"],
                    moved=moved or None, cancelled=cancelled or None)
        return {"availability": _window_out(d, target), "moved_bookings": moved, "cancelled_bookings": cancelled}
    return store.mutate(fn)


@route("DELETE", "/availability/{aid}")
def delete_availability(ctx):
    def fn(d):
        w = _owned(d, "availability", ctx.params["aid"], ctx.user, "availability")
        # past sessions stay in the calendar history; today's stay if they have already started
        effective = _window_in_use_from(w, now())
        w["active"] = False
        w["valid_to"] = (effective - timedelta(days=1)).isoformat()
        n = _cancel_future_bookings(d, lambda b: b["session_key"].startswith(w["id"] + "@"),
                                    ctx.user, "availability_removed")
        store.audit(d, ctx.user["id"], "availability_removed", w["id"], coach_id=w["coach_id"], cancelled=n)
        return n
    return {"ok": True, "cancelled_bookings": store.mutate(fn)}


def _cancel_future_bookings(d, match, actor, reason):
    n_now, count = now(), 0
    for b in d["bookings"].values():
        if b["status"] == "booked" and match(b) and \
                local_dt(date.fromisoformat(b["date"]), b["start"]) > n_now:
            b.update(status="cancelled", updated_at=stamp(), by=actor["id"], reason=reason)
            count += 1
    return count


def _coach_session(d, viewer, key):
    sess = schedule.get_session(d, key)
    scope = auth.coach_scope(viewer)
    if not sess or not auth.is_coach(viewer) or (scope is not None and sess["coach_id"] not in scope):
        raise ApiError(404, "session_not_found", "session not found")
    return sess


@route("PUT", "/overrides/{key}")
def put_override(ctx):
    def fn(d):
        sess = _coach_session(d, ctx.user, ctx.params["key"])
        ov = {}
        if "cancelled" in ctx.body:
            ov["cancelled"] = bool(ctx.b("cancelled"))
        if ctx.b("capacity") is not None:
            ov["capacity"] = _capacity(ctx.b("capacity"))
        if ctx.b("note"):
            ov["note"] = clean_str(ctx.b("note"), "note", 300)
        if ov:
            d["overrides"][sess["key"]] = ov
        else:
            d["overrides"].pop(sess["key"], None)
        n = restored = 0
        if ov.get("cancelled"):
            n = _cancel_future_bookings(d, lambda b: b["session_key"] == sess["key"], ctx.user, "session_cancelled")
        elif sess["cancelled"] and not schedule.get_session(d, sess["key"])["cancelled"]:
            restored = _restore_bookings(d, sess)
        _promote_waitlist(d, sess["key"])          # more places, or back on the schedule
        store.audit(d, ctx.user["id"], "session_override", sess["key"], coach_id=sess["coach_id"], **ov)
        return {"session": schedule.get_session(d, sess["key"]), "cancelled_bookings": n,
                "restored_bookings": restored}
    return store.mutate(fn)


def _restore_bookings(d, sess):
    """Re-book swimmers whose booking was dropped when this training was cancelled."""
    n_now, count = now(), 0
    if schedule.session_start(sess) <= n_now:
        return 0
    for b in d["bookings"].values():
        if b["session_key"] == sess["key"] and b["status"] == "cancelled" and b.get("reason") == "session_cancelled" \
                and credits.can_afford(d, b["swimmer_id"], b["date"], n_now):
            b.update(status="booked", updated_at=stamp())
            b.pop("reason", None)
            count += 1
    return count


def _has_attendees(d, key):
    """Booked/visited swimmers - and those whose booking the coach called off, since they
    still see this training as cancelled in their calendar."""
    return any(b["session_key"] == key and (b["status"] != "cancelled" or b.get("reason") in COACH_CANCEL_REASONS)
               for b in d["bookings"].values())


@route("DELETE", "/sessions/{key}")
def delete_session(ctx):
    """Delete one training (one-off, or a single date of a repeating slot) - only when empty."""
    def fn(d):
        sess = _coach_session(d, ctx.user, ctx.params["key"])
        if _has_attendees(d, sess["key"]):
            raise ApiError(409, "session_has_attendees", "remove the booked swimmers or cancel the training instead")
        if sess["kind"] == "extra":
            del d["extra_sessions"][sess["source_id"]]
        else:
            d["overrides"][sess["key"]] = {"removed": True}
        store.audit(d, ctx.user["id"], "session_deleted", sess["key"], coach_id=sess["coach_id"],
                    date=sess["date"], start=sess["start"])
        return {"ok": True}
    return store.mutate(fn)


@route("DELETE", "/overrides/{key}")
def delete_override(ctx):
    def fn(d):
        sess = _coach_session(d, ctx.user, ctx.params["key"])
        d["overrides"].pop(sess["key"], None)
        store.audit(d, ctx.user["id"], "session_override_removed", sess["key"], coach_id=sess["coach_id"])
        return {"session": schedule.get_session(d, sess["key"])}
    return store.mutate(fn)


@route("POST", "/extra-sessions")
def create_extra(ctx):
    auth.require_coach(ctx.user)

    def fn(d):
        coach_id = _target_coach(d, ctx.user, ctx.b("coach_id"))
        day = parse_date(ctx.b("date")).isoformat()
        fields = _session_fields(ctx.body, False)
        starts = [fields["start"]]
        if ctx.b("end"):
            # a time range: split it into consecutive slots of duration_min
            w = {"start": fields["start"], "end": parse_time(ctx.b("end"), "end"), "slot_minutes": fields["duration_min"]}
            _check_window(w)
            starts = schedule.slot_starts(w)
        out = []
        for start in starts:
            x = {"id": new_id("x"), "coach_id": coach_id, "date": day, **fields, "start": start, "created_at": stamp()}
            d["extra_sessions"][x["id"]] = x
            store.audit(d, ctx.user["id"], "extra_session_created", x["id"], coach_id=coach_id, date=day, start=start)
            out.append(schedule.get_session(d, "x:" + x["id"]))
        return out
    sessions = store.mutate(fn)
    return {"session": sessions[0], "sessions": sessions}


@route("PATCH", "/extra-sessions/{xid}")
def update_extra(ctx):
    def fn(d):
        x = _owned(d, "extra_sessions", ctx.params["xid"], ctx.user, "session")
        x.update(_session_fields(ctx.body, True))
        for b in d["bookings"].values():
            if b["session_key"] == "x:" + x["id"]:
                b["start"], b["end"] = x["start"], add_minutes(x["start"], x["duration_min"])
        store.audit(d, ctx.user["id"], "extra_session_updated", x["id"], coach_id=x["coach_id"])
        return schedule.get_session(d, "x:" + x["id"])
    return {"session": store.mutate(fn)}


@route("DELETE", "/extra-sessions/{xid}")
def delete_extra(ctx):
    def fn(d):
        x = _owned(d, "extra_sessions", ctx.params["xid"], ctx.user, "session")
        if _has_attendees(d, "x:" + x["id"]):
            raise ApiError(409, "session_has_attendees", "remove the booked swimmers or cancel the training instead")
        del d["extra_sessions"][x["id"]]
        store.audit(d, ctx.user["id"], "extra_session_removed", x["id"], coach_id=x["coach_id"])
        return 0
    return {"ok": True, "cancelled_bookings": store.mutate(fn)}


# --------------------------------------------------------------------------- bookings

def _needs_attention(b, viewer):
    """Swimmer: coach requests and unseen changes. Coach: disputes and requests still open."""
    if auth.is_coach(viewer):
        return (b.get("ack") or {}).get("status") == "disputed" or bool(b.get("pending"))
    return bool(b.get("pending")) or bool(b.get("unseen")) or bool(b.get("waitlist_notice"))


@route("GET", "/bookings")
def list_bookings(ctx):
    n = now()
    with store.LOCK:
        d = store.current()
        if auth.is_coach(ctx.user):
            if ctx.q("swimmer_id"):
                ids = {_managed_swimmer(d, ctx.user, ctx.q("swimmer_id"))["id"]}
            else:
                ids = {u["id"] for u in d["users"].values() if auth.can_manage_swimmer(ctx.user, u)}
        else:
            ids = {ctx.user["id"]}
        start = ctx.q("from") and parse_date(ctx.q("from"), "from").isoformat()
        end = ctx.q("to") and parse_date(ctx.q("to"), "to").isoformat()
        out = []
        for b in d["bookings"].values():
            if b["swimmer_id"] not in ids or (start and b["date"] < start) or (end and b["date"] > end):
                continue
            if _bool(ctx.q("attention")) and not _needs_attention(b, ctx.user):
                continue
            bo = booking_out(d, b, ctx.user, n)
            if ctx.q("status") and bo["status"] != ctx.q("status"):
                continue
            out.append(bo)
    out.sort(key=lambda b: (b["date"], b["start"]), reverse=True)
    return {"bookings": out}


def _book(d, viewer, swimmer, sess, wanted, force, n, check_window=True):
    """Book `swimmer` (and the given joint-account members) into `sess`. Raises ApiError and
    leaves `d` untouched if it can't be done. Returns the new bookings."""
    if sess["cancelled"]:
        raise ApiError(409, "session_cancelled", "this session is cancelled")
    if not auth.is_coach(viewer) and check_window:
        p = prefs.for_coach(d, sess["coach_id"])
        if not p["swimmers_can_book"]:
            raise ApiError(403, "self_booking_disabled", "your coach books the trainings")
        if schedule.session_start(sess) <= n:
            raise ApiError(409, "too_late", "this session has already started")
        if date.fromisoformat(sess["date"]) > n.date() + timedelta(days=p["booking_horizon_days"]):
            raise ApiError(409, "too_far", "booking is not open yet for this date")
    existing = [b for b in d["bookings"].values() if b["session_key"] == sess["key"] and b["status"] != "cancelled"]
    # a joint account books one place per member (e.g. two kids on one card)
    account_members = swimmer.get("members") or []
    if account_members:
        if not isinstance(wanted, list) or not wanted:
            raise ApiError(400, "pick_members", "choose who is coming")
        if [m for m in wanted if m not in account_members]:
            raise ApiError(400, "invalid_field", "unknown member", field="members")
        wanted = list(dict.fromkeys(wanted))
    else:
        wanted = [None]
    taken = {b.get("member") for b in existing if b["swimmer_id"] == swimmer["id"]}
    if any(m in taken for m in wanted):
        raise ApiError(409, "already_booked", "already booked")
    if not force and sum(1 for b in existing if _occupying(b, n)) + len(wanted) > sess["capacity"]:
        raise ApiError(409, "full", "no free places")
    unpaid_before = credits.balance(d, swimmer["id"], n)["unpaid"]
    probe = {"date": sess["date"], "end": sess["end"]}
    locked = acks.ended(probe, n) and n >= acks.ack_deadline(probe)
    made = []
    for member in wanted:
        b = {"id": new_id("b"), "swimmer_id": swimmer["id"], "coach_id": sess["coach_id"],
             "session_key": sess["key"], "date": sess["date"], "start": sess["start"], "end": sess["end"],
             "location": sess["location"], "title": sess["title"], "status": "booked", "member": member,
             "created_at": stamp(), "updated_at": stamp(), "by": viewer["id"]}
        if acks.ended(b, n):
            b["unseen"] = True
            if locked:
                # a visit added to an old, locked training costs the swimmer: needs their OK
                b.update(status="cancelled", reason="pending_approval",
                         pending={"status": "attended", "by": viewer["id"], "at": stamp()})
                acks.log(b, viewer, "requested", to="attended")
            else:
                acks.log(b, viewer, "added", to="attended")
        d["bookings"][b["id"]] = b
        made.append(b)
    if not force and credits.balance(d, swimmer["id"], n)["unpaid"] > unpaid_before:
        for b in made:
            del d["bookings"][b["id"]]
        raise ApiError(409, "no_credits", "not enough visits left")
    for b in made:
        store.audit(d, viewer["id"], "booking_created", b["id"], coach_id=sess["coach_id"],
                    swimmer=swimmer["id"], session=sess["key"], member=b["member"], forced=force or None)
    return made


@route("POST", "/bookings")
def create_booking(ctx):
    """{session_key, members?, swimmer_id? (coach), force? (coach)}"""
    viewer = ctx.user
    force = auth.is_coach(viewer) and _bool(ctx.b("force"))

    def fn(d):
        n = now()
        swimmer = _managed_swimmer(d, viewer, ctx.b("swimmer_id")) if auth.is_coach(viewer) else d["users"][viewer["id"]]
        sess = schedule.get_session(d, ctx.b("session_key"))
        if not sess or sess["coach_id"] != swimmer.get("coach_id"):
            raise ApiError(404, "session_not_found", "session not found")
        return [booking_out(d, b, viewer, n) for b in _book(d, viewer, swimmer, sess, ctx.b("members"), force, n)]
    made = store.mutate(fn)
    return {"booking": made[0], "bookings": made}


# --------------------------------------------------------------------------- waiting list

def _promote_waitlist(d, key, n=None):
    """A place freed up: book the first people in line who still fit and can pay."""
    n = n or now()
    sess = schedule.get_session(d, key)
    if not sess or sess["cancelled"] or schedule.session_start(sess) <= n:
        return 0
    promoted = 0
    queue = sorted((w for w in d["waitlist"].values() if w["session_key"] == key), key=lambda w: w["created_at"])
    for w in queue:
        sw = d["users"].get(w["swimmer_id"])
        if not sw or not sw.get("active", True):
            del d["waitlist"][w["id"]]
            continue
        try:
            made = _book(d, sw, sw, sess, [w["member"]] if w.get("member") else None, False, n, check_window=False)
        except ApiError as e:
            if e.code in ("full",):
                break                      # still no room - keep the queue as it is
            del d["waitlist"][w["id"]]     # can't be booked (no visits, already booked...)
            continue
        for b in made:
            b["waitlist_notice"] = True
            acks.log(b, sw, "from_waitlist")
        del d["waitlist"][w["id"]]
        promoted += 1
    return promoted


@route("POST", "/waitlist")
def join_waitlist(ctx):
    """{session_key, member?} - swimmers only, for a full future session."""
    if auth.is_coach(ctx.user):
        raise ApiError(403, "forbidden", "swimmers join the waiting list themselves")

    def fn(d):
        n = now()
        sw = d["users"][ctx.user["id"]]
        sess = schedule.get_session(d, ctx.b("session_key"))
        if not sess or sess["coach_id"] != sw.get("coach_id") or sess["cancelled"] or schedule.session_start(sess) <= n:
            raise ApiError(404, "session_not_found", "session not found")
        if not prefs.for_coach(d, sess["coach_id"])["swimmers_can_book"]:
            raise ApiError(403, "self_booking_disabled", "your coach books the trainings")
        member = ctx.b("member") or None
        if (sw.get("members") or []) and member not in sw["members"]:
            raise ApiError(400, "pick_members", "choose who is coming")
        if any(w["session_key"] == sess["key"] and w["swimmer_id"] == sw["id"] and w.get("member") == member
               for w in d["waitlist"].values()):
            raise ApiError(409, "already_waiting", "already on the waiting list")
        w = {"id": new_id("w"), "session_key": sess["key"], "swimmer_id": sw["id"], "member": member,
             "coach_id": sess["coach_id"], "date": sess["date"], "start": sess["start"], "created_at": stamp()}
        d["waitlist"][w["id"]] = w
        store.audit(d, sw["id"], "waitlist_joined", w["id"], coach_id=sess["coach_id"], session=sess["key"])
        # maybe a place is free right now
        _promote_waitlist(d, sess["key"], n)
        return w
    return {"entry": store.mutate(fn)}


@route("DELETE", "/waitlist/{wid}")
def leave_waitlist(ctx):
    def fn(d):
        w = d["waitlist"].get(ctx.params["wid"])
        if not w or not (w["swimmer_id"] == ctx.user["id"] or auth.can_manage_swimmer(ctx.user, d["users"].get(w["swimmer_id"]))):
            raise ApiError(404, "not_found", "not on the waiting list")
        del d["waitlist"][w["id"]]
    store.mutate(fn)
    return {"ok": True}


def _visible_booking(d, viewer, bid):
    b = d["bookings"].get(bid)
    if not b:
        raise ApiError(404, "not_found", "booking not found")
    sw = d["users"].get(b["swimmer_id"])
    if not (b["swimmer_id"] == viewer["id"] or auth.can_manage_swimmer(viewer, sw)):
        raise ApiError(404, "not_found", "booking not found")
    return b


@route("DELETE", "/bookings/{bid}")
def cancel_booking(ctx):
    def fn(d):
        n = now()
        b = _visible_booking(d, ctx.user, ctx.params["bid"])
        if b["status"] == "cancelled":
            return booking_out(d, b, ctx.user, n)
        if not _can_cancel(b, ctx.user, n, d):
            raise ApiError(409, "cancel_too_late", "it is too late to cancel - ask your coach")
        b.update(status="cancelled", updated_at=stamp(), by=ctx.user["id"])
        store.audit(d, ctx.user["id"], "booking_cancelled", b["id"], coach_id=b["coach_id"], swimmer=b["swimmer_id"])
        _promote_waitlist(d, b["session_key"], n)
        return booking_out(d, b, ctx.user, n)
    return {"booking": store.mutate(fn)}


@route("POST", "/bookings/{bid}/note")
def booking_note(ctx):
    """A note on one visit. The coach's note and the swimmer's note are both visible to both."""
    def fn(d):
        b = _visible_booking(d, ctx.user, ctx.params["bid"])
        text = clean_str(ctx.b("text"), "text", 500)
        field_ = "note" if auth.is_coach(ctx.user) else "swimmer_note"
        b[field_] = text or None
        b["updated_at"] = stamp()
        store.audit(d, ctx.user["id"], "visit_note", b["id"], coach_id=b["coach_id"], swimmer=b["swimmer_id"])
        return booking_out(d, b, ctx.user)
    return {"booking": store.mutate(fn)}


@route("PATCH", "/bookings/{bid}")
def update_booking(ctx):
    auth.require_coach(ctx.user)
    status = ctx.b("status")
    if status not in credits.STATUSES:
        raise ApiError(400, "invalid_field", "unknown status", field="status")

    def fn(d):
        n = now()
        b = _visible_booking(d, ctx.user, ctx.params["bid"])
        opens = local_dt(date.fromisoformat(b["date"]), b["start"]) - timedelta(minutes=config.CONFIG["checkin_early_minutes"])
        if status in ("attended", "no_show", "excused") and opens > n:
            raise ApiError(409, "not_started", "the session has not started yet")
        old = credits.effective_status(b, n)
        if old == status:
            return booking_out(d, b, ctx.user, n), False
        if acks.is_protected(b, n) and acks.costs_swimmer(old, status):
            # the record is locked: the swimmer has to agree to anything that costs them
            b["pending"] = {"status": status, "by": ctx.user["id"], "at": stamp()}
            acks.log(b, ctx.user, "requested", **{"from": old, "to": status})
            b["unseen"] = True
            store.audit(d, ctx.user["id"], "booking_change_requested", b["id"], coach_id=b["coach_id"],
                        swimmer=b["swimmer_id"], old=old, new=status)
            return booking_out(d, b, ctx.user, n), True
        b.update(status=status, updated_at=stamp(), by=ctx.user["id"])
        b.pop("auto", None)
        b.pop("pending", None)
        if acks.ended(b, n):
            acks.log(b, ctx.user, "status", **{"from": old, "to": status})
            b["unseen"] = True
            if (b.get("ack") or {}).get("status") == "confirmed":
                b["ack"] = {"status": "changed", "at": stamp()}     # what they confirmed is no longer the record
        if "note" in ctx.body:
            b["note"] = clean_str(ctx.b("note"), "note", 300)
        if status in ("cancelled", "excused"):
            _promote_waitlist(d, b["session_key"], n)
        store.audit(d, ctx.user["id"], "booking_status", b["id"], coach_id=b["coach_id"],
                    swimmer=b["swimmer_id"], old=old, new=status)
        return booking_out(d, b, ctx.user, n), False
    booking, requested = store.mutate(fn)
    return {"booking": booking, "requested": requested}


@route("POST", "/bookings/{bid}/ack")
def ack_booking(ctx):
    """Swimmer: confirm / dispute a past visit, approve / reject a coach request, or mark changes seen."""
    action = ctx.b("action")
    if action not in ("confirm", "dispute", "approve", "reject", "seen"):
        raise ApiError(400, "invalid_field", "unknown action", field="action")
    if auth.is_coach(ctx.user):
        raise ApiError(403, "forbidden", "this is the swimmer's decision")

    def fn(d):
        n = now()
        b = d["bookings"].get(ctx.params["bid"])
        if not b or b["swimmer_id"] != ctx.user["id"]:
            raise ApiError(404, "not_found", "booking not found")
        if action in ("confirm", "dispute"):
            if not acks.can_ack(b, n) and not (action == "confirm" and (b.get("ack") or {}).get("status") == "changed"):
                raise ApiError(409, "ack_closed", "this visit can no longer be confirmed or disputed")
            comment = clean_str(ctx.b("comment"), "comment", 500)
            if action == "dispute" and not comment:
                raise ApiError(400, "invalid_field", "please say what is wrong", field="comment")
            b["ack"] = {"status": "confirmed" if action == "confirm" else "disputed", "at": stamp(),
                        "comment": comment or None}
            acks.log(b, ctx.user, action, comment=comment or None)
        elif action in ("approve", "reject"):
            p = b.get("pending")
            if not p:
                raise ApiError(409, "no_request", "nothing to approve")
            if action == "approve":
                old = credits.effective_status(b, n)
                b.update(status=p["status"], updated_at=stamp(), by=p["by"])
                b.pop("reason", None)
                b.pop("auto", None)
                acks.log(b, ctx.user, "approved", **{"from": old, "to": p["status"]})
            else:
                acks.log(b, ctx.user, "rejected", to=p["status"])
            b.pop("pending", None)
        b.pop("unseen", None)
        b.pop("waitlist_notice", None)
        store.audit(d, ctx.user["id"], "visit_" + action, b["id"], coach_id=b["coach_id"], swimmer=b["swimmer_id"])
        return booking_out(d, b, ctx.user, n)
    return {"booking": store.mutate(fn)}


@route("POST", "/bookings/{bid}/resolve")
def resolve_dispute(ctx):
    """Coach: mark a disputed visit as dealt with (after talking to the swimmer)."""
    auth.require_coach(ctx.user)

    def fn(d):
        b = _visible_booking(d, ctx.user, ctx.params["bid"])
        if (b.get("ack") or {}).get("status") != "disputed":
            raise ApiError(409, "not_disputed", "this visit is not disputed")
        b["ack"] = dict(b["ack"], status="resolved", resolved_at=stamp(), resolved_by=ctx.user["id"])
        acks.log(b, ctx.user, "resolved")
        b["unseen"] = True
        store.audit(d, ctx.user["id"], "dispute_resolved", b["id"], coach_id=b["coach_id"], swimmer=b["swimmer_id"])
        return booking_out(d, b, ctx.user)
    return {"booking": store.mutate(fn)}


# --------------------------------------------------------------------------- credits

@route("GET", "/credits/{uid}")
def get_credits(ctx):
    with store.LOCK:
        d = store.current()
        u = d["users"].get(ctx.params["uid"])
        if not auth.can_view_swimmer(ctx.user, u) or u["role"] != "swimmer":
            raise ApiError(404, "not_found", "swimmer not found")
        return {"balance": credits.balance(d, u["id"])}


@route("GET", "/package-templates")
def list_templates(ctx):
    auth.require_coach(ctx.user)
    scope = _scope_for_query(ctx.user, ctx.q("coach_id"))
    with store.LOCK:
        d = store.current()
        out = [t for t in d["package_templates"].values() if scope is None or t["coach_id"] in scope]
    return {"templates": sorted(out, key=lambda t: (t["visits"], t["name"]))}


def _template_fields(body, partial):
    out = {}
    if not partial or "name" in body:
        out["name"] = clean_str(body.get("name"), "name", 100, required=True)
    if not partial or "visits" in body:
        out["visits"] = parse_int(body.get("visits"), "visits", 1, 500)
    if not partial or "valid_days" in body:
        v = body.get("valid_days")
        out["valid_days"] = parse_int(v, "valid_days", 1, 3650) if v not in (None, "", 0) else None
    return out


@route("POST", "/package-templates")
def create_template(ctx):
    auth.require_coach(ctx.user)

    def fn(d):
        t = {"id": new_id("t"), "coach_id": _target_coach(d, ctx.user, ctx.b("coach_id")),
             **_template_fields(ctx.body, False), "created_at": stamp()}
        d["package_templates"][t["id"]] = t
        return t
    return {"template": store.mutate(fn)}


@route("PATCH", "/package-templates/{tid}")
def update_template(ctx):
    def fn(d):
        t = _owned(d, "package_templates", ctx.params["tid"], ctx.user, "template")
        t.update(_template_fields(ctx.body, True))
        return t
    return {"template": store.mutate(fn)}


@route("DELETE", "/package-templates/{tid}")
def delete_template(ctx):
    def fn(d):
        _owned(d, "package_templates", ctx.params["tid"], ctx.user, "template")
        del d["package_templates"][ctx.params["tid"]]
    store.mutate(fn)
    return {"ok": True}


@route("POST", "/packages")
def add_package(ctx):
    auth.require_coach(ctx.user)

    def fn(d):
        sw = _managed_swimmer(d, ctx.user, ctx.b("swimmer_id"))
        name, valid_days = "", None
        if ctx.b("template_id"):
            t = d["package_templates"].get(ctx.b("template_id"))
            scope = auth.coach_scope(ctx.user)
            if not t or (scope is not None and t["coach_id"] not in scope):
                raise ApiError(400, "invalid_field", "unknown template", field="template_id")
            visits, name, valid_days = t["visits"], t["name"], t.get("valid_days")
        else:
            visits = parse_int(ctx.b("visits"), "visits", 1, 500)
            if ctx.b("valid_days"):
                valid_days = parse_int(ctx.b("valid_days"), "valid_days", 1, 3650)
        start = parse_date(ctx.b("start_date"), "start_date") if ctx.b("start_date") else today()
        valid_until = None
        if ctx.b("valid_until"):
            valid_until = parse_date(ctx.b("valid_until"), "valid_until").isoformat()
        elif valid_days:
            valid_until = (start + timedelta(days=valid_days - 1)).isoformat()
        p = {"id": new_id("p"), "swimmer_id": sw["id"], "coach_id": sw["coach_id"], "visits": visits,
             "name": name or "%d" % visits, "template_id": ctx.b("template_id"), "valid_until": valid_until,
             "note": clean_str(ctx.b("note"), "note", 300), "created_by": ctx.user["id"], "created_at": stamp()}
        d["packages"][p["id"]] = p
        store.audit(d, ctx.user["id"], "package_added", p["id"], coach_id=sw["coach_id"], swimmer=sw["id"],
                    visits=visits, valid_until=valid_until)
        return {"package": p, "balance": credits.balance(d, sw["id"])}
    return store.mutate(fn)


@route("DELETE", "/packages/{pid}")
def delete_package(ctx):
    auth.require_coach(ctx.user)

    def fn(d):
        p = d["packages"].get(ctx.params["pid"])
        if not p or not auth.can_manage_swimmer(ctx.user, d["users"].get(p["swimmer_id"])):
            raise ApiError(404, "not_found", "package not found")
        del d["packages"][p["id"]]
        store.audit(d, ctx.user["id"], "package_removed", p["id"], coach_id=p["coach_id"],
                    swimmer=p["swimmer_id"], visits=p["visits"])
        return {"balance": credits.balance(d, p["swimmer_id"])}
    return store.mutate(fn)


# --------------------------------------------------------------------------- statistics & export

def _scoped_bookings(d, viewer, start, end, swimmer_id=None, coach_id=None):
    scope = _scope_for_query(viewer, coach_id)
    for b in d["bookings"].values():
        if not (start.isoformat() <= b["date"] <= end.isoformat()):
            continue
        if swimmer_id and b["swimmer_id"] != swimmer_id:
            continue
        if auth.is_coach(viewer):
            if scope is not None and b["coach_id"] not in scope:
                continue
        elif b["swimmer_id"] != viewer["id"]:
            continue
        yield b


@route("GET", "/stats")
def stats(ctx):
    start, end = _date_range(ctx, default_days=30)
    n = now()
    with store.LOCK:
        d = store.current()
        per_swimmer, per_day, per_weekday, per_time = {}, {}, [0] * 7, {}
        attended_keys = set()
        totals = {st: 0 for st in credits.STATUSES}
        for b in _scoped_bookings(d, ctx.user, start, end, ctx.q("swimmer_id"), ctx.q("coach_id")):
            st = credits.effective_status(b, n)
            totals[st] += 1
            u = d["users"].get(b["swimmer_id"], {})
            row = per_swimmer.setdefault(b["swimmer_id"], {"swimmer_id": b["swimmer_id"], "name": u.get("name", "?"),
                                                           **{s: 0 for s in credits.STATUSES}})
            row[st] += 1
            if st == "attended":
                attended_keys.add(b["session_key"])
                per_day[b["date"]] = per_day.get(b["date"], 0) + 1
                per_weekday[date.fromisoformat(b["date"]).weekday()] += 1
                per_time[b["start"]] = per_time.get(b["start"], 0) + 1
        sessions = schedule.occurrences(d, start, min(end, today()), _scope_for_query(ctx.user, ctx.q("coach_id"))) \
            if auth.is_coach(ctx.user) else []
    # empty 45-min slots are offers, not trainings: "held" = someone actually came
    offered = [s for s in sessions if not s["cancelled"] and schedule.session_start(s) <= n]
    held = [s for s in offered if s["key"] in attended_keys]
    places = sum(s["capacity"] for s in offered)
    came = totals["attended"] + totals["no_show"]
    return {
        "from": start.isoformat(), "to": end.isoformat(),
        "totals": totals,
        "attendance_rate": round(totals["attended"] / came, 3) if came else None,
        "sessions_held": len(held),
        "slots_offered": len(offered),
        "occupancy": round(totals["attended"] / places, 3) if places else None,
        "avg_per_session": round(totals["attended"] / len(held), 2) if held else None,
        "per_swimmer": sorted(per_swimmer.values(), key=lambda r: (-r["attended"], r["name"])),
        "per_day": dict(sorted(per_day.items())),
        "per_weekday": per_weekday,
        "per_time": dict(sorted(per_time.items())),
    }


@route("GET", "/export.csv")
def export_csv(ctx):
    start, end = _date_range(ctx, default_days=31)
    n = now()
    with store.LOCK:
        d = store.current()
        rows = []
        for b in _scoped_bookings(d, ctx.user, start, end, ctx.q("swimmer_id"), ctx.q("coach_id")):
            u = d["users"].get(b["swimmer_id"], {})
            rows.append([b["date"], b["start"], b["end"], b.get("location", ""), u.get("name", "?"),
                         u.get("username", "?"), credits.effective_status(b, n), b["created_at"]])
    rows.sort()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "start", "end", "location", "swimmer", "username", "status", "booked_at"])
    w.writerows(rows)
    return Raw("﻿" + buf.getvalue(), "text/csv; charset=utf-8",
               "visits_%s_%s.csv" % (start.isoformat(), end.isoformat()))


# --------------------------------------------------------------------------- reports & audit

@route("GET", "/reports")
def list_reports(ctx):
    auth.require_coach(ctx.user)
    return {"reports": reports.list_reports(None if auth.is_admin(ctx.user) else ctx.user)}


REPORT_TYPES = {"json": ".json", "txt": ".txt", "visits": "_visits.csv", "balances": "_balances.csv"}


@route("GET", "/reports/{day}/{kind}")
def get_report(ctx):
    auth.require_coach(ctx.user)
    day = parse_date(ctx.params["day"], "day")
    suffix = REPORT_TYPES.get(ctx.params["kind"])
    if not suffix:
        raise ApiError(404, "not_found", "unknown report type")
    with store.LOCK:
        d = store.current()
        coach = d["users"].get(ctx.q("coach_id") or ctx.user["id"])
        if not coach or coach["role"] != "coach" or (coach["id"] != ctx.user["id"] and not auth.is_admin(ctx.user)):
            raise ApiError(404, "not_found", "report not found")
    path = reports.report_path(day, coach, suffix)
    if not os.path.exists(path):
        raise ApiError(404, "not_found", "report not found")
    with open(path, encoding="utf-8") as f:
        body = f.read()
    ctype = {"json": "application/json", "txt": "text/plain"}.get(ctx.params["kind"], "text/csv")
    return Raw(body, ctype + "; charset=utf-8", os.path.basename(path))


@route("POST", "/reports/run")
def run_report(ctx):
    auth.require_coach(ctx.user)
    day = parse_date(ctx.b("date"), "date") if ctx.b("date") else today()
    if day > today():
        raise ApiError(400, "invalid_field", "cannot report the future", field="date")
    paths = reports.run_daily(day)
    return {"ok": True, "files": len(paths)}


@route("GET", "/audit")
def audit_log(ctx):
    auth.require_coach(ctx.user)
    limit = parse_int(ctx.q("limit", 200), "limit", 1, 2000)
    scope = auth.coach_scope(ctx.user)
    with store.LOCK:
        d = store.current()
        entries = [e for e in reversed(d["audit"]) if scope is None or e.get("coach_id") in scope][:limit]
        names = {u["id"]: u["name"] for u in d["users"].values()}
    return {"entries": [dict(e, actor_name=names.get(e["actor"], e["actor"]),
                             target_name=names.get(e["target"])) for e in entries]}


# --------------------------------------------------------------------------- live sync

@route("GET", "/changes", allow_pw_change=True)
def changes(ctx):
    since = parse_int(ctx.q("since", 0), "since", 0)
    wait = parse_int(ctx.q("wait", 0), "wait", 0, 30)
    with store.LOCK:
        rev = store.current()["rev"]
    if rev <= since and wait:
        rev = store.wait_for_change(since, wait)
    return {"rev": rev}


@route("GET", "", public=True)
def describe(ctx):
    return {"version": "v1", "endpoints": sorted("%s /api/v1%s" % (r[0], r[5]) for r in ROUTES)}
