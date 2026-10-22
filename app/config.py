"""Configuration: config.json merged over defaults. Paths are relative to the project dir."""

import copy
import json
import os
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8870,
    "timezone": "Europe/Sofia",
    "default_lang": "bg",
    "data_dir": "data",
    "report_dir": "reports",
    "report_time": "21:30",          # daily report, local time
    "report_catchup_days": 31,       # how far back missed reports are regenerated
    "cancel_cutoff_hours": 12,       # swimmers may cancel up to this many hours before start
    "booking_horizon_days": 28,      # swimmers may book this far ahead
    "default_slot_minutes": 45,      # length of one bookable slot in a new availability window
    "max_capacity": 8,               # swimmers per slot (coach can still overbook explicitly)
    "max_members": 3,                # people on a joint account (e.g. siblings sharing one card)
    "ack_window_days": 3,            # swimmers can confirm/dispute a past visit this long; then it locks
    "low_credit_threshold": 2,
    "session_ttl_days": 30,
    "cookie_secure": False,          # set true once served over HTTPS only
    "backup_dir": "../backup_swimming_calendar",
    "backup_keep": 1000,
    "notifiers": {
        "file": {"enabled": True},
        # Not implemented yet - placeholder so the config shape is stable.
        "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "username": "",
                  "password": "", "from": "", "starttls": True},
    },
}

CONFIG = copy.deepcopy(DEFAULTS)


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load(path=None, overrides=None):
    """(Re)load configuration in place so every module sees the same dict."""
    cfg = copy.deepcopy(DEFAULTS)
    path = path or CONFIG_PATH
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    if overrides:
        _merge(cfg, overrides)
    CONFIG.clear()
    CONFIG.update(cfg)
    return CONFIG


def path(key):
    p = CONFIG[key]
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(BASE_DIR, p))


def tz():
    return ZoneInfo(CONFIG["timezone"])
