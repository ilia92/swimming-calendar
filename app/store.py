"""JSON-file "database".

Everything lives in one data.json held in memory. Writes go through mutate(), which runs
the change under a lock, persists atomically (tmp + fsync + os.replace) and bumps `rev`
so long-polling clients know to refresh. If data.json is edited by hand or restored from
a backup while the server runs, it is reloaded on the next access (mtime/size check).
"""

import copy
import json
import os
import threading

from . import config

LOCK = threading.RLock()
CHANGED = threading.Condition(LOCK)
AUDIT_MAX = 5000

_cache = {"data": None, "stamp": None}


def data_file():
    return os.path.join(config.path("data_dir"), "data.json")


def empty_data():
    return {
        "version": 2,
        "rev": 0,
        "users": {},
        "availability": {},
        "overrides": {},
        "extra_sessions": {},
        "package_templates": {},
        "packages": {},
        "bookings": {},
        "comments": {},
        "audit": [],
        "meta": {"last_report_date": None},
    }


def _file_stamp():
    try:
        st = os.stat(data_file())
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def _load():
    path = data_file()
    if not os.path.exists(path):
        return empty_data()
    with open(path, encoding="utf-8") as f:
        try:
            d = json.load(f)
        except json.JSONDecodeError as e:
            # Refuse to run on a corrupt file rather than silently start empty.
            raise SystemExit("data.json is corrupt (%s) - restore it from a backup" % e)
    base = empty_data()
    migrate(d)
    for k, v in base.items():
        d.setdefault(k, v)
    return d


def migrate(d):
    """v1 had one session per weekly "slot"; v2 has availability windows split into slots."""
    if d.get("version", 1) >= 2:
        return
    from .util import add_minutes
    avail = d.setdefault("availability", {})
    renamed = {}
    for sid, slot in (d.pop("slots", None) or {}).items():
        avail[sid] = {
            "id": sid, "coach_id": slot["coach_id"], "weekday": slot["weekday"],
            "start": slot["start"], "end": add_minutes(slot["start"], slot["duration_min"]),
            "slot_minutes": slot["duration_min"], "capacity": slot["capacity"],
            "location": slot.get("location", ""), "title": slot.get("title", ""),
            "valid_from": slot.get("valid_from"), "valid_to": slot.get("valid_to"),
            "active": slot.get("active", True), "created_at": slot.get("created_at"),
        }
        renamed[sid] = slot["start"]

    def new_key(key):
        sid, _, rest = key.partition("@")
        return "%s@%sT%s" % (sid, rest, renamed[sid]) if sid in renamed and "T" not in rest else key
    for b in d.get("bookings", {}).values():
        b["session_key"] = new_key(b["session_key"])
    d["overrides"] = {new_key(k): v for k, v in d.get("overrides", {}).items()}
    d["version"] = 2


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    binary = isinstance(text, bytes)
    with open(tmp, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"})) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write(d):
    write_atomic(data_file(), json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True))


def current():
    """The live data dict. Callers must hold LOCK while reading it."""
    with LOCK:
        stamp = _file_stamp()
        if _cache["data"] is None or stamp != _cache["stamp"]:
            _cache["data"] = _load()
            _cache["stamp"] = stamp
        return _cache["data"]


def reset_cache():
    with LOCK:
        _cache.update(data=None, stamp=None)


def mutate(fn):
    """Apply fn(data) atomically. If fn raises, nothing is written and memory is restored."""
    with LOCK:
        d = current()
        backup = copy.deepcopy(d)
        try:
            result = fn(d)
            d["rev"] = backup["rev"] + 1
            if len(d["audit"]) > AUDIT_MAX:
                del d["audit"][:len(d["audit"]) - AUDIT_MAX]
            _write(d)
        except BaseException:
            _cache["data"] = backup
            raise
        _cache["stamp"] = _file_stamp()
        CHANGED.notify_all()
        return result


def wait_for_change(since, timeout):
    """Block until rev > since or timeout; returns the current rev."""
    with LOCK:
        CHANGED.wait_for(lambda: current()["rev"] > since, timeout=timeout)
        return current()["rev"]


def audit(d, actor, action, target=None, coach_id=None, **details):
    from .util import stamp
    d["audit"].append({
        "ts": stamp(), "actor": actor, "action": action, "target": target,
        "coach_id": coach_id, "details": details or None,
    })
