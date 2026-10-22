"""Protection of past visits (so the customer can trust the record of what they paid for).

After a training has ended the swimmer has `ack_window_days` to confirm or dispute the visit.
Once confirmed, disputed, or when the window has passed, the visit is *protected*:

  - a coach change that favours the swimmer (gives a visit back, no-show -> attended)
    is applied right away;
  - a change that costs the swimmer (uses a visit, or attended -> no-show) is stored as a
    request the swimmer approves or rejects.

Every change to a visit after its training ended is written to the visit's own history and
flagged `unseen` until the swimmer has looked at it.
"""

from datetime import date, timedelta

from . import config
from .credits import CONSUMING
from .util import local_dt, now as clock_now, stamp


def ended(b, n=None):
    return local_dt(date.fromisoformat(b["date"]), b["end"]) <= (n or clock_now())


def ack_deadline(b):
    end = local_dt(date.fromisoformat(b["date"]), b["end"])
    return end + timedelta(days=config.CONFIG["ack_window_days"])


def is_protected(b, n=None):
    n = n or clock_now()
    if not ended(b, n):
        return False
    return bool(b.get("ack")) or n >= ack_deadline(b)


def can_ack(b, n=None):
    """The swimmer may still confirm/dispute: training over, window open, not yet confirmed."""
    n = n or clock_now()
    ack = (b.get("ack") or {}).get("status")
    return ended(b, n) and n < ack_deadline(b) and ack != "confirmed" and b["status"] != "cancelled"


def costs_swimmer(old, new):
    """Would changing a visit from `old` to `new` count against the swimmer?"""
    if new in CONSUMING and old not in CONSUMING:
        return True                       # uses up a visit
    return old == "attended" and new == "no_show"


def log(b, actor, kind, **details):
    b.setdefault("history", []).append({"at": stamp(), "by": actor["id"], "kind": kind,
                                        **{k: v for k, v in details.items() if v is not None}})


def out_fields(d, b, n=None):
    """Extra fields for API output."""
    n = n or clock_now()
    names = {u["id"]: u["name"] for u in d["users"].values()}
    return {
        "ended": ended(b, n),
        "protected": is_protected(b, n),
        "can_ack": can_ack(b, n),
        "ack_until": ack_deadline(b).replace(microsecond=0).isoformat(),
        "history": [dict(h, by_name=names.get(h["by"], "?")) for h in b.get("history", [])],
    }

