"""Visit credits.

A swimmer buys packages ("10 visits", optionally with an expiry date). Each booking that
counts as a visit consumes one credit:

  consuming:      attended, no_show, and `booked` whose session has already ended
                  (auto-consumption - derived, so it is correct even if no job ever ran)
  reserved:       `booked` and not yet ended
  not consuming:  cancelled, excused

Visits are allocated to packages in date order, oldest-expiring package first, and a
visit can only use a package still valid on the visit date. Visits no package covers
are "unpaid" (shown to the coach as debt); adding a package later covers them.
"""

from datetime import date

from . import config, prefs
from .util import local_dt, now as clock_now

CONSUMING = {"attended", "no_show"}
INACTIVE = {"cancelled", "excused"}
STATUSES = ("booked", "attended", "no_show", "excused", "cancelled")


def booking_ended(b, now=None):
    return local_dt(date.fromisoformat(b["date"]), b["end"]) <= (now or clock_now())


def effective_status(b, now=None):
    """`booked` turns into `attended` once the session is over."""
    if b["status"] == "booked" and booking_ended(b, now):
        return "attended"
    return b["status"]


def swimmer_bookings(d, swimmer_id):
    return [b for b in d["bookings"].values() if b["swimmer_id"] == swimmer_id]


def _allocate(packages, visit_dates):
    """Assign visits to packages. Returns (remaining per package id, unpaid count)."""
    remaining = {p["id"]: p["visits"] for p in packages}
    order = sorted(packages, key=lambda p: (p.get("valid_until") or "9999-12-31", p["created_at"], p["id"]))
    unpaid = 0
    for iso in sorted(visit_dates):
        for p in order:
            if remaining[p["id"]] > 0 and (not p.get("valid_until") or p["valid_until"] >= iso):
                remaining[p["id"]] -= 1
                break
        else:
            unpaid += 1
    return remaining, unpaid


def balance(d, swimmer_id, now=None, extra_visit=None):
    """Credit summary for one swimmer. extra_visit=ISO date simulates one more reservation."""
    now = now or clock_now()
    today_iso = now.date().isoformat()
    packages = [p for p in d["packages"].values() if p["swimmer_id"] == swimmer_id]
    used_dates, reserved_dates = [], []
    for b in swimmer_bookings(d, swimmer_id):
        st = effective_status(b, now)
        if st in CONSUMING:
            used_dates.append(b["date"])
        elif st == "booked":
            reserved_dates.append(b["date"])
    if extra_visit:
        reserved_dates.append(extra_visit)

    remaining, unpaid = _allocate(packages, used_dates + reserved_dates)
    active = [p for p in packages if not p.get("valid_until") or p["valid_until"] >= today_iso]
    available = sum(remaining[p["id"]] for p in active)
    expired_unused = sum(remaining[p["id"]] for p in packages if p not in active)
    pk_out = sorted(({
        **p,
        "remaining": remaining[p["id"]],
        "expired": p not in active,
    } for p in packages), key=lambda p: p["created_at"], reverse=True)
    # when the visits still left run out: [{date, remaining}], earliest first, null date = no expiry
    by_date = {}
    for p in active:
        if remaining[p["id"]] > 0:
            by_date[p.get("valid_until")] = by_date.get(p.get("valid_until"), 0) + remaining[p["id"]]
    expiry = [{"date": k, "remaining": v} for k, v in sorted(by_date.items(), key=lambda kv: kv[0] or "9999-12-31")]
    return {
        "expiry": expiry,
        "total": sum(p["visits"] for p in packages),
        "used": len(used_dates),
        "reserved": len(reserved_dates) - (1 if extra_visit else 0),
        "available": available,
        "unpaid": unpaid,
        "expired_unused": expired_unused,
        "low": available <= _low_threshold(d, swimmer_id),
        "packages": pk_out,
    }


def _low_threshold(d, swimmer_id):
    u = d["users"].get(swimmer_id) or {}
    return prefs.for_coach(d, u.get("coach_id"))["low_credit_threshold"]


def can_afford(d, swimmer_id, visit_date_iso, now=None):
    """Would one more visit on this date still be covered by a package?"""
    before = balance(d, swimmer_id, now)["unpaid"]
    after = balance(d, swimmer_id, now, extra_visit=visit_date_iso)["unpaid"]
    return after == before


def finalize_past(d, now=None):
    """Write derived auto-consumption back into the data. Returns how many changed."""
    now = now or clock_now()
    n = 0
    for b in d["bookings"].values():
        if b["status"] == "booked" and booking_ended(b, now):
            b["status"] = "attended"
            b["updated_at"] = now.replace(microsecond=0).isoformat()
            b["auto"] = True
            n += 1
    return n


def pending_finalize(d, now=None):
    now = now or clock_now()
    return any(b["status"] == "booked" and booking_ended(b, now) for b in d["bookings"].values())


def last_visit(d, swimmer_id, now=None):
    dates = [b["date"] for b in swimmer_bookings(d, swimmer_id) if effective_status(b, now) == "attended"]
    return max(dates) if dates else None


def next_booking(d, swimmer_id, now=None):
    upcoming = [b for b in swimmer_bookings(d, swimmer_id) if effective_status(b, now) == "booked"]
    return min(upcoming, key=lambda b: (b["date"], b["start"])) if upcoming else None

