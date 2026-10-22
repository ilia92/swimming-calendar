"""Training sessions.

Sessions are never stored. They are computed from:
  - availability   weekly windows when the coach is available (weekday + start-end), split
                   into slots of `slot_minutes` (default 45), each with `capacity` places
  - extra_sessions one-off sessions on a given date
  - overrides      per-occurrence changes, keyed by session key (cancel, capacity, note)
and Bulgarian public holidays, which cancel a slot occurrence automatically unless the
coach turned that off or explicitly re-enabled the occurrence with an override.

Session keys: "<window_id>@YYYY-MM-DDTHH:MM" for availability slots, "x:<extra_id>" for one-offs.
"""

from datetime import date, timedelta
from functools import lru_cache

from .util import add_minutes, daterange, local_dt


# --------------------------------------------------------------------------- BG holidays
# Same rules as custody-calendar/server.py (Labour Code art. 154(1)).

def orthodox_easter(year):
    """Easter Sunday of the Orthodox church, as a Gregorian date (valid 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    return date(year, month, day) + timedelta(days=13)  # Julian -> Gregorian


FIXED_BG = [
    ((1, 1), "Нова година", "New Year's Day"),
    ((3, 3), "Ден на Освобождението на България", "Liberation Day"),
    ((5, 1), "Ден на труда", "Labour Day"),
    ((5, 6), "Гергьовден", "St George's Day"),
    ((5, 24), "Ден на българската азбука, просвета и култура", "Education and Culture Day"),
    ((9, 6), "Ден на Съединението", "Unification Day"),
    ((9, 22), "Ден на Независимостта", "Independence Day"),
    ((12, 24), "Бъдни вечер", "Christmas Eve"),
    ((12, 25), "Рождество Христово", "Christmas Day"),
    ((12, 26), "Втори ден на Рождество Христово", "Second day of Christmas"),
]


@lru_cache(maxsize=32)
def bg_holidays(year):
    """{iso: {name_bg, name_en}} for one year, computed offline."""
    out = {}
    for (m, d), bg, en in FIXED_BG:
        out[date(year, m, d).isoformat()] = {"name_bg": bg, "name_en": en, "type": "public"}
    easter = orthodox_easter(year)
    for offset, bg, en in ((-2, "Разпети петък", "Good Friday"), (-1, "Велика събота", "Holy Saturday"),
                           (0, "Великден", "Easter Sunday"), (1, "Втори ден на Великден", "Easter Monday")):
        out[(easter + timedelta(days=offset)).isoformat()] = {"name_bg": bg, "name_en": en, "type": "easter"}
    # A public holiday (Easter excluded) falling on a weekend moves to the next working day.
    for iso in sorted(k for k, v in out.items() if v["type"] == "public"):
        day = date.fromisoformat(iso)
        if day.weekday() < 5:
            continue
        nxt = day + timedelta(days=1)
        while nxt.weekday() >= 5 or nxt.isoformat() in out:
            nxt += timedelta(days=1)
        out[nxt.isoformat()] = {"name_bg": "Почивен ден (%s)" % out[iso]["name_bg"],
                                "name_en": "Day off for %s" % out[iso]["name_en"], "type": "compensation"}
    return out


def holiday(day):
    return bg_holidays(day.year).get(day.isoformat())


# --------------------------------------------------------------------------- occurrences

def minutes(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def hhmm(total):
    return "%02d:%02d" % divmod(total, 60)


def runs_on(window, day):
    if day.weekday() != window["weekday"]:
        return False
    if window.get("valid_from") and day.isoformat() < window["valid_from"]:
        return False
    if window.get("valid_to") and day.isoformat() > window["valid_to"]:
        return False
    return True


def slot_starts(window):
    """Start times ("HH:MM") of the slots a window is split into."""
    step = window["slot_minutes"]
    t, end = minutes(window["start"]), minutes(window["end"])
    out = []
    while t + step <= end:
        out.append(hhmm(t))
        t += step
    return out


def _build(d, key, coach_id, day, start, duration, capacity, location, title, kind, source_id):
    ov = d["overrides"].get(key) or {}
    if ov.get("removed"):
        return None          # the coach deleted this single occurrence
    cancelled, reason, hol_name = False, None, None
    hol = holiday(day)
    coach = d["users"].get(coach_id) or {}
    if hol:
        hol_name = {"bg": hol["name_bg"], "en": hol["name_en"]}
        if kind == "slot" and coach.get("skip_holidays", True):
            cancelled, reason = True, "holiday"
    if "cancelled" in ov:
        cancelled = bool(ov["cancelled"])
        reason = "coach" if cancelled else None
    if ov.get("capacity") is not None:
        capacity = ov["capacity"]
    return {
        "key": key,
        "kind": kind,
        "source_id": source_id,
        "coach_id": coach_id,
        "coach_name": coach.get("name", ""),
        "date": day.isoformat(),
        "start": start,
        "end": add_minutes(start, duration),
        "duration_min": duration,
        "capacity": capacity,
        "location": location or "",
        "title": title or "",
        "note": ov.get("note") or "",
        "cancelled": cancelled,
        "cancel_reason": reason,
        "holiday": hol_name,
        "overridden": bool(ov),
    }


def slot_key(window, day, start):
    return "%s@%sT%s" % (window["id"], day.isoformat(), start)


def _from_window(d, w, day, start):
    return _build(d, slot_key(w, day, start), w["coach_id"], day, start, w["slot_minutes"], w["capacity"],
                  w.get("location"), w.get("title"), "slot", w["id"])


def _from_extra(d, x):
    return _build(d, "x:%s" % x["id"], x["coach_id"], date.fromisoformat(x["date"]), x["start"],
                  x["duration_min"], x["capacity"], x.get("location"), x.get("title"), "extra", x["id"])


def occurrences(d, start, end, coaches=None):
    """All sessions between start and end (inclusive), sorted. coaches=None means all."""
    out = []
    # ended windows keep valid_to, so their past sessions remain visible as history
    windows = [w for w in d["availability"].values() if coaches is None or w["coach_id"] in coaches]
    for day in daterange(start, end):
        for w in windows:
            if runs_on(w, day):
                out.extend(s for s in (_from_window(d, w, day, t) for t in slot_starts(w)) if s)
    for x in d["extra_sessions"].values():
        if (coaches is None or x["coach_id"] in coaches) and start.isoformat() <= x["date"] <= end.isoformat():
            s = _from_extra(d, x)
            if s:
                out.append(s)
    out.sort(key=lambda s: (s["date"], s["start"], s["coach_name"], s["key"]))
    return out


def get_session(d, key):
    """Resolve a session key to a session dict, or None if it does not (or no longer) exist."""
    if not isinstance(key, str):
        return None
    if key.startswith("x:"):
        x = d["extra_sessions"].get(key[2:])
        return _from_extra(d, x) if x else None
    wid, _, when = key.partition("@")
    iso, _, start = when.partition("T")
    w = d["availability"].get(wid)
    try:
        day = date.fromisoformat(iso)
    except ValueError:
        return None
    if not w or not runs_on(w, day) or start not in slot_starts(w):
        return None
    return _from_window(d, w, day, start)


def holidays_between(start, end):
    out = {}
    for day in daterange(start, end):
        h = holiday(day)
        if h:
            out[day.isoformat()] = {"bg": h["name_bg"], "en": h["name_en"]}
    return out


def session_start(sess):
    return local_dt(date.fromisoformat(sess["date"]), sess["start"])


def session_end(sess):
    return local_dt(date.fromisoformat(sess["date"]), sess["end"])
