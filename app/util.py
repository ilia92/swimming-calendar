"""Small shared helpers: errors, clock, ids, parsing."""

import re
import secrets
from datetime import date, datetime, time, timedelta

from . import config


class ApiError(Exception):
    """Raised anywhere in request handling; turned into a JSON error response.

    `code` is a stable machine-readable key the frontend translates (errors.<code>).
    """

    def __init__(self, status, code, message=None, **extra):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code
        self.extra = extra


# The clock is swappable so tests can move time forward.
_fixed_now = None


def set_now(dt):
    global _fixed_now
    _fixed_now = dt


def now():
    if _fixed_now is not None:
        return _fixed_now
    return datetime.now(config.tz())


def today():
    return now().date()


def stamp():
    """Timestamp for records, ISO with offset, second precision."""
    return now().replace(microsecond=0).isoformat()


def new_id(prefix):
    return "%s_%s" % (prefix, secrets.token_hex(5))


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_date(value, field="date"):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError(400, "invalid_field", "%s must be YYYY-MM-DD" % field, field=field)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "invalid_field", "%s is not a valid date" % field, field=field)


def add_months(day, n):
    """The same day n months later, clamped to the month's last day (Nov 30 + 3 -> Feb 28)."""
    y, m = divmod(day.month - 1 + n, 12)
    y, m = day.year + y, m + 1
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return min(day.replace(year=y, month=m, day=1) + timedelta(days=day.day - 1), nxt - timedelta(days=1))


def parse_time(value, field="start"):
    if not isinstance(value, str) or not TIME_RE.match(value):
        raise ApiError(400, "invalid_field", "%s must be HH:MM" % field, field=field)
    return value


def parse_int(value, field, lo=None, hi=None):
    if isinstance(value, bool):
        raise ApiError(400, "invalid_field", "%s must be an integer" % field, field=field)
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_field", "%s must be an integer" % field, field=field)
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise ApiError(400, "invalid_field", "%s out of range" % field, field=field)
    return n


def clean_str(value, field, max_len=200, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, "invalid_field", "%s must be a string" % field, field=field)
    value = value.strip()
    if required and not value:
        raise ApiError(400, "invalid_field", "%s is required" % field, field=field)
    if len(value) > max_len:
        raise ApiError(400, "invalid_field", "%s is too long" % field, field=field)
    return value


def local_dt(day, hhmm):
    """Aware datetime for a local date + 'HH:MM' in the configured timezone."""
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=config.tz())


def add_minutes(hhmm, minutes):
    h, m = map(int, hhmm.split(":"))
    total = h * 60 + m + minutes
    total = min(total, 23 * 60 + 59)   # sessions never cross midnight
    return "%02d:%02d" % divmod(total, 60)


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
