"""Daily report: who came when, plus every swimmer's balance.

Written once a day per coach as a safety net independent of the web app:
    reports/YYYY/MM/YYYY-MM-DD_<coach>.json          full snapshot
    reports/YYYY/MM/YYYY-MM-DD_<coach>.txt           human readable (coach's language)
    reports/YYYY/MM/YYYY-MM-DD_<coach>_visits.csv    one row per booking that day
    reports/YYYY/MM/YYYY-MM-DD_<coach>_balances.csv  credits of every swimmer

Delivery goes through notifiers. Only FileNotifier exists today; an e-mail notifier can be
added later by implementing deliver() and enabling it in config.json -> notifiers.email.
"""

import csv
import io
import json
import logging
import os
import re
import threading
from datetime import date, timedelta

from . import config, credits, schedule, store
from .i18n import strings
from .util import local_dt, now as clock_now

log = logging.getLogger("swim.reports")


# --------------------------------------------------------------------------- building

def _swimmers_of(d, coach_id):
    return sorted((u for u in d["users"].values() if u["role"] == "swimmer" and u.get("coach_id") == coach_id),
                  key=lambda u: u["name"].lower())


def build(d, coach, day, now=None):
    now = now or clock_now()
    users = d["users"]
    by_session = {}
    for b in d["bookings"].values():
        if b["date"] == day.isoformat():
            by_session.setdefault(b["session_key"], []).append(b)

    sessions = []
    keys_seen = set()
    for s in schedule.occurrences(d, day, day, {coach["id"]}):
        keys_seen.add(s["key"])
        sessions.append((s, by_session.get(s["key"], [])))
    # bookings whose session no longer exists (slot removed) still belong in the record
    for key, bs in by_session.items():
        if key not in keys_seen and users.get(bs[0]["swimmer_id"], {}).get("coach_id") == coach["id"]:
            b0 = bs[0]
            sessions.append(({"key": key, "date": b0["date"], "start": b0["start"], "end": b0["end"],
                              "location": "", "title": "", "capacity": None, "cancelled": True,
                              "cancel_reason": "removed"}, bs))
    sessions.sort(key=lambda p: (p[0]["start"], p[0]["key"]))

    summary = {st: 0 for st in credits.STATUSES}
    out_sessions = []
    for s, bs in sessions:
        attendees = []
        for b in sorted(bs, key=lambda b: users.get(b["swimmer_id"], {}).get("name", "")):
            st = credits.effective_status(b, now)
            summary[st] += 1
            u = users.get(b["swimmer_id"], {})
            name = u.get("name", "?") + (" (%s)" % b["member"] if b.get("member") else "")
            attendees.append({"booking_id": b["id"], "swimmer_id": b["swimmer_id"], "name": name,
                              "username": u.get("username", "?"), "status": st})
        out_sessions.append({k: s.get(k) for k in ("key", "start", "end", "location", "title", "capacity",
                                                   "cancelled", "cancel_reason")} | {"attendees": attendees})

    balances = []
    for u in _swimmers_of(d, coach["id"]):
        bal = credits.balance(d, u["id"], now)
        balances.append({"swimmer_id": u["id"], "name": u["name"], "username": u["username"],
                         "active": u.get("active", True),
                         **{k: bal[k] for k in ("total", "used", "reserved", "available", "unpaid", "low")}})

    tomorrow = (day + timedelta(days=1)).isoformat()
    tomorrow_list = []
    for b in d["bookings"].values():
        u = users.get(b["swimmer_id"], {})
        if b["date"] == tomorrow and u.get("coach_id") == coach["id"] and b["status"] == "booked":
            tomorrow_list.append({"start": b["start"], "name": u.get("name", "?")})
    tomorrow_list.sort(key=lambda x: (x["start"], x["name"]))

    return {
        "date": day.isoformat(),
        "coach": {"id": coach["id"], "name": coach["name"], "username": coach["username"]},
        "generated_at": now.replace(microsecond=0).isoformat(),
        "summary": summary,
        "sessions": out_sessions,
        "balances": balances,
        "tomorrow": tomorrow_list,
    }


# --------------------------------------------------------------------------- rendering

def _csv(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return "﻿" + buf.getvalue()   # BOM so Excel opens Cyrillic correctly


def render(report, lang):
    t = strings(lang)
    st = t["status"]
    visits = [[t["col_date"], t["col_start"], t["col_end"], t["col_location"], t["col_swimmer"],
               t["col_username"], t["col_status"]]]
    for s in report["sessions"]:
        for a in s["attendees"]:
            visits.append([report["date"], s["start"], s["end"], s["location"], a["name"], a["username"],
                           st.get(a["status"], a["status"])])
    bal = [[t["col_swimmer"], t["col_username"], "total", "used", "reserved", t["col_left"], t["unpaid"]]]
    for b in report["balances"]:
        bal.append([b["name"], b["username"], b["total"], b["used"], b["reserved"], b["available"], b["unpaid"]])

    lines = ["%s %s - %s: %s" % (t["report_title"], report["date"], t["coach"], report["coach"]["name"]),
             "%s: %s" % (t["generated"], report["generated_at"]), "", "== %s ==" % t["sessions"]]
    if not report["sessions"]:
        lines.append(t["no_sessions"])
    for s in report["sessions"]:
        head = "%s-%s %s" % (s["start"], s["end"], " ".join(x for x in (s["title"], s["location"]) if x))
        if s["cancelled"]:
            head += "  [%s]" % t["cancelled"]
        lines.append(head.rstrip())
        if not s["attendees"]:
            lines.append("    (%s)" % t["nobody"])
        for a in s["attendees"]:
            lines.append("    - %s (%s): %s" % (a["name"], a["username"], st.get(a["status"], a["status"])))
    lines += ["", "== %s ==" % t["summary"]]
    lines += ["%s: %d" % (st[k], v) for k, v in report["summary"].items() if v] or [t["none"]]
    lines += ["", "== %s ==" % t["balances"]]
    for b in report["balances"]:
        mark = " !" if b["low"] else ""
        extra = ", %s: %d" % (t["unpaid"], b["unpaid"]) if b["unpaid"] else ""
        lines.append("%s (%s): %d %s %d%s%s" % (b["name"], b["username"], b["available"], t["of"],
                                               b["total"], extra, mark))
    low = [b["name"] for b in report["balances"] if b["low"] and b["active"]]
    lines += ["", "== %s ==" % t["low_credit"], ", ".join(low) if low else t["none"]]
    lines += ["", "== %s ==" % t["tomorrow"]]
    lines += ["%s %s" % (x["start"], x["name"]) for x in report["tomorrow"]] or [t["none"]]

    return {
        ".json": json.dumps(report, ensure_ascii=False, indent=1),
        ".txt": "\n".join(lines) + "\n",
        "_visits.csv": _csv(visits),
        "_balances.csv": _csv(bal),
    }


# --------------------------------------------------------------------------- delivery

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def report_dir_for(day):
    return os.path.join(config.path("report_dir"), "%04d" % day.year, "%02d" % day.month)


def file_base(day, coach):
    return "%s_%s" % (day.isoformat(), SAFE_NAME.sub("_", coach["username"]))


class FileNotifier:
    name = "file"

    def deliver(self, report, rendered, coach, day):
        folder = report_dir_for(day)
        paths = []
        for suffix, content in rendered.items():
            path = os.path.join(folder, file_base(day, coach) + suffix)
            store.write_atomic(path, content)
            paths.append(path)
        return paths


class EmailNotifier:
    """Placeholder: enable in config once e-mail delivery is wanted."""
    name = "email"

    def __init__(self, settings):
        self.settings = settings

    def deliver(self, report, rendered, coach, day):
        log.warning("email notifier is enabled but not implemented yet - skipped")
        return []


def notifiers():
    cfg = config.CONFIG["notifiers"]
    out = []
    if cfg.get("file", {}).get("enabled", True):
        out.append(FileNotifier())
    if cfg.get("email", {}).get("enabled"):
        out.append(EmailNotifier(cfg["email"]))
    return out


def run_daily(day, now=None):
    """Finalize past bookings, then build and deliver the report of `day` for every coach."""
    now = now or clock_now()

    def finalize(d):
        credits.finalize_past(d, now)
    with store.LOCK:
        if credits.pending_finalize(store.current(), now):
            store.mutate(finalize)
        d = store.current()
        coaches = [u for u in d["users"].values() if u["role"] == "coach" and u.get("active", True)]
        jobs = [(c, build(d, c, day, now)) for c in coaches]

    paths = []
    for coach, report in jobs:
        rendered = render(report, coach.get("lang") or config.CONFIG["default_lang"])
        for n in notifiers():
            try:
                paths += n.deliver(report, rendered, coach, day)
            except Exception:
                log.exception("notifier %s failed for %s %s", n.name, coach["username"], day)

    # A manual run before report_time must not stop the scheduled run from writing the
    # final version of that day later on.
    if now >= local_dt(day, config.CONFIG["report_time"]):
        def mark(d):
            last = d["meta"].get("last_report_date")
            if not last or last < day.isoformat():
                d["meta"]["last_report_date"] = day.isoformat()
        store.mutate(mark)
    return paths


def due_dates(now=None):
    """Report dates that should exist by now but have not been produced yet."""
    now = now or clock_now()
    h, m = map(int, config.CONFIG["report_time"].split(":"))
    target = now.date() if (now.hour, now.minute) >= (h, m) else now.date() - timedelta(days=1)
    with store.LOCK:
        last = store.current()["meta"].get("last_report_date")
    if not last:
        return [target]
    start = max(date.fromisoformat(last) + timedelta(days=1),
                target - timedelta(days=config.CONFIG["report_catchup_days"] - 1))
    out = []
    while start <= target:
        out.append(start)
        start += timedelta(days=1)
    return out


def list_reports(coach=None):
    """[{date, coach, files}] newest first. coach=None lists everybody's."""
    root = config.path("report_dir")
    found = {}
    if not os.path.isdir(root):
        return []
    for dirpath, _, files in os.walk(root):
        for f in files:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+?)(\.json|\.txt|_visits\.csv|_balances\.csv)$", f)
            if not m or (coach and m.group(2) != SAFE_NAME.sub("_", coach["username"])):
                continue
            found.setdefault((m.group(1), m.group(2)), []).append(m.group(3))
    return [{"date": k[0], "coach": k[1], "files": sorted(v)} for k, v in sorted(found.items(), reverse=True)]


def report_path(day, coach, suffix):
    return os.path.join(report_dir_for(day), file_base(day, coach) + suffix)


# --------------------------------------------------------------------------- scheduler

class Scheduler(threading.Thread):
    """Once a minute: write auto-consumed visits back to data, run any due daily reports."""

    def __init__(self, interval=60):
        super().__init__(name="swim-scheduler", daemon=True)
        self.interval = interval
        self.stop_event = threading.Event()

    def tick(self):
        now = clock_now()
        with store.LOCK:
            if credits.pending_finalize(store.current(), now):
                store.mutate(lambda d: credits.finalize_past(d, now))
        for day in due_dates(now):
            paths = run_daily(day, now)
            log.info("daily report %s: %d file(s)", day, len(paths))

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self.stop_event.wait(self.interval)

