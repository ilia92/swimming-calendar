"""Per-coach settings. A coach can change these from the UI; anything not set falls back
to config.json. Swimmers get the settings of their own coach."""

from . import config

# key: (min, max) for integers, bool for switches
COACH_SETTINGS = {
    "cancel_cutoff_hours": (0, 168),
    "booking_horizon_days": (1, 180),
    "low_credit_threshold": (0, 20),
    "default_slot_minutes": (15, 240),
    "default_capacity": (1, None),      # upper bound is config max_capacity
    "swimmers_can_book": bool,
    "skip_holidays": bool,
}


def defaults():
    c = config.CONFIG
    return {
        "cancel_cutoff_hours": c["cancel_cutoff_hours"],
        "booking_horizon_days": c["booking_horizon_days"],
        "low_credit_threshold": c["low_credit_threshold"],
        "default_slot_minutes": c["default_slot_minutes"],
        "default_capacity": c["max_capacity"],
        "swimmers_can_book": True,
        "skip_holidays": True,
    }


def for_coach(d, coach_id):
    coach = d["users"].get(coach_id) or {}
    out = defaults()
    out.update(coach.get("settings") or {})
    if "skip_holidays" in coach:          # stored on the user record since v1
        out["skip_holidays"] = coach["skip_holidays"]
    out["default_capacity"] = min(out["default_capacity"], config.CONFIG["max_capacity"])
    return out


def for_user(d, user):
    return for_coach(d, user["id"] if user["role"] == "coach" else user.get("coach_id"))
