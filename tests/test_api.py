"""End-to-end tests: a real server on a random port against a temp data dir.

Run:  python3 -m unittest discover tests -v
"""

import http.cookiejar
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import api, auth, config, reports, store, util, web  # noqa: E402

# Wednesday 2026-10-21 10:00 Sofia time
T0 = datetime(2026, 10, 21, 10, 0, tzinfo=config.tz())


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + "/api/v1" + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req) as r:
                payload = r.read().decode("utf-8")
                return r.status, (payload if raw else json.loads(payload))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def ok(self, method, path, body=None, raw=False):
        status, out = self.call(method, path, body, raw)
        assert status == 200, (method, path, status, out)
        return out

    def login(self, username, password, new_password=None):
        self.ok("POST", "/login", {"username": username, "password": password})
        if new_password:
            self.ok("POST", "/me/password", {"current_password": password, "new_password": new_password})
        return self


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = web.make_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        config.load(os.path.join(self.tmp, "none.json"), {
            "data_dir": os.path.join(self.tmp, "data"),
            "report_dir": os.path.join(self.tmp, "reports"),
        })
        store.reset_cache()
        auth.reset_sessions()
        auth.LOGIN_LIMITER._fails.clear()
        util.set_now(T0)
        u, pw = api.create_user_cli("coach", "coach", "Coach One", is_admin=True)
        self.coach = Client(self.base).login("coach", pw, "coach-pass-1")
        self.coach_id = u["id"]

    def tearDown(self):
        util.set_now(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ---------------------------------------------------------------

    def make_swimmer(self, username="mark", name="Марк", visits=None, coach=None):
        out = (coach or self.coach).ok("POST", "/swimmers", {"username": username, "name": name, "phone": "0888"})
        sw = Client(self.base).login(username, out["password"], "swim-pass-1")
        sw.id = out["user"]["id"]
        if visits:
            (coach or self.coach).ok("POST", "/packages", {"swimmer_id": sw.id, "visits": visits})
        return sw

    def make_slot(self, weekday=3, start="18:00", capacity=2, coach=None):
        """A one-slot availability window (60 min). weekday 3 = Thursday -> 2026-10-22."""
        end = "%02d:%s" % (int(start[:2]) + 1, start[3:])
        return (coach or self.coach).ok("POST", "/availability", {
            "weekdays": [weekday], "start": start, "end": end, "slot_minutes": 60,
            "capacity": capacity, "location": "Басейн"})["availability"][0]

    # auth ------------------------------------------------------------------

    def test_password_hashing(self):
        h = auth.hash_password("secret123")
        self.assertTrue(auth.verify_password("secret123", h))
        self.assertFalse(auth.verify_password("secret124", h))
        self.assertNotEqual(h, auth.hash_password("secret123"))   # salted

    def test_login_wrong_password_and_lockout(self):
        c = Client(self.base)
        for _ in range(5):
            self.assertEqual(c.call("POST", "/login", {"username": "coach", "password": "nope"})[0], 401)
        status, out = c.call("POST", "/login", {"username": "coach", "password": "coach-pass-1"})
        self.assertEqual((status, out["error"]), (429, "too_many_attempts"))

    def test_unauthenticated(self):
        status, out = Client(self.base).call("GET", "/me")
        self.assertEqual((status, out["error"]), (401, "unauthorized"))

    def test_new_swimmer_must_change_password(self):
        out = self.coach.ok("POST", "/swimmers", {"username": "Mark", "name": "Марк"})
        self.assertEqual(out["user"]["username"], "mark")
        self.assertEqual(len(out["password"]), 10)
        sw = Client(self.base).login("mark", out["password"])
        self.assertEqual(sw.call("GET", "/sessions")[1]["error"], "password_change_required")
        self.assertTrue(sw.ok("GET", "/me")["user"]["must_change_password"])
        self.assertEqual(sw.call("POST", "/me/password", {"current_password": out["password"],
                                                          "new_password": "short"})[1]["error"], "password_too_short")
        sw.ok("POST", "/me/password", {"current_password": out["password"], "new_password": "long-enough"})
        sw.ok("GET", "/sessions")

    def test_only_coach_creates_accounts(self):
        sw = self.make_swimmer()
        self.assertEqual(sw.call("POST", "/swimmers", {"username": "x1x", "name": "X"})[0], 403)
        self.assertEqual(sw.call("POST", "/coaches", {"username": "x1x", "name": "X"})[0], 403)
        self.assertEqual(self.coach.call("POST", "/swimmers", {"username": "mark", "name": "Dup"})[1]["error"],
                         "username_taken")

    def test_sessions_survive_restart(self):
        auth.reset_sessions()          # simulate a restart: memory gone, file stays
        self.coach.ok("GET", "/me")

    def test_reset_password_logs_user_out(self):
        sw = self.make_swimmer()
        out = self.coach.ok("POST", "/users/%s/reset-password" % sw.id)
        self.assertEqual(sw.call("GET", "/me")[0], 401)
        Client(self.base).login("mark", out["password"], "another-pass")

    def test_deactivated_swimmer_cannot_log_in(self):
        sw = self.make_swimmer()
        self.coach.ok("PATCH", "/users/%s" % sw.id, {"active": False})
        self.assertEqual(sw.call("GET", "/me")[0], 401)
        self.assertEqual(Client(self.base).call("POST", "/login", {"username": "mark", "password": "swim-pass-1"})[0], 401)

    # isolation -------------------------------------------------------------

    def test_swimmer_isolation(self):
        a = self.make_swimmer("anna", "Анна", visits=5)
        b = self.make_swimmer("boris", "Борис", visits=5)
        self.assertEqual(a.call("GET", "/users/%s" % b.id)[0], 404)
        self.assertEqual(a.call("GET", "/credits/%s" % b.id)[0], 404)
        self.assertEqual(a.call("GET", "/swimmers")[0], 403)
        self.assertEqual(a.call("GET", "/stats")[0], 200)  # own stats only
        slot = self.make_slot()
        key = "%s@2026-10-22T18:00" % slot["id"]
        bk = b.ok("POST", "/bookings", {"session_key": key})["booking"]
        self.assertEqual(a.call("DELETE", "/bookings/%s" % bk["id"])[0], 404)
        sess = a.ok("GET", "/sessions?from=2026-10-22&to=2026-10-22")["sessions"][0]
        self.assertNotIn("attendees", sess)        # no names of other swimmers
        self.assertEqual(sess["booked"], 1)
        self.assertEqual([x["id"] for x in a.ok("GET", "/bookings")["bookings"]], [])

    def test_coaches_see_only_their_swimmers(self):
        out = self.coach.ok("POST", "/coaches", {"username": "coach2", "name": "Coach Two"})
        c2 = Client(self.base).login("coach2", out["password"], "coach-pass-2")
        self.make_swimmer("anna", "Анна")
        mine = self.make_swimmer("boris", "Борис", coach=c2)
        self.assertEqual([s["username"] for s in c2.ok("GET", "/swimmers")["swimmers"]], ["boris"])
        self.assertEqual(len(self.coach.ok("GET", "/swimmers")["swimmers"]), 2)   # admin sees all
        anna = [s for s in self.coach.ok("GET", "/swimmers")["swimmers"] if s["username"] == "anna"][0]
        self.assertEqual(c2.call("GET", "/users/%s" % anna["id"])[0], 404)
        self.assertEqual(c2.call("POST", "/coaches", {"username": "c3c", "name": "x"})[0], 403)
        self.assertEqual(c2.ok("GET", "/users/%s" % mine.id)["user"]["username"], "boris")

    # schedule & bookings ---------------------------------------------------

    def test_booking_capacity_and_credits(self):
        slot = self.make_slot(capacity=2)
        key = "%s@2026-10-22T18:00" % slot["id"]
        a = self.make_swimmer("anna", "Анна", visits=10)
        b = self.make_swimmer("boris", "Борис", visits=10)
        c = self.make_swimmer("cveta", "Цвета", visits=10)
        none = self.make_swimmer("dido", "Дидо")
        self.assertEqual(none.call("POST", "/bookings", {"session_key": key})[1]["error"], "no_credits")
        a.ok("POST", "/bookings", {"session_key": key})
        self.assertEqual(a.call("POST", "/bookings", {"session_key": key})[1]["error"], "already_booked")
        b.ok("POST", "/bookings", {"session_key": key})
        self.assertEqual(c.call("POST", "/bookings", {"session_key": key})[1]["error"], "full")
        bal = a.ok("GET", "/credits/%s" % a.id)["balance"]
        self.assertEqual((bal["total"], bal["used"], bal["reserved"], bal["available"]), (10, 0, 1, 9))
        # coach can overbook with force
        self.coach.ok("POST", "/bookings", {"session_key": key, "swimmer_id": c.id, "force": True})

    def test_auto_consumption_after_session(self):
        slot = self.make_slot()
        key = "%s@2026-10-22T18:00" % slot["id"]
        a = self.make_swimmer(visits=10)
        bk = a.ok("POST", "/bookings", {"session_key": key})["booking"]
        util.set_now(datetime(2026, 10, 22, 18, 59, tzinfo=config.tz()))
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["reserved"], 1)
        util.set_now(datetime(2026, 10, 22, 19, 0, tzinfo=config.tz()))
        bal = a.ok("GET", "/credits/%s" % a.id)["balance"]
        self.assertEqual((bal["used"], bal["reserved"], bal["available"]), (1, 0, 9))
        self.assertEqual(a.ok("GET", "/bookings")["bookings"][0]["status"], "attended")
        # coach excuses it -> credit back
        self.coach.ok("PATCH", "/bookings/%s" % bk["id"], {"status": "excused"})
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["available"], 10)

    def test_cancel_cutoff(self):
        slot = self.make_slot()
        key = "%s@2026-10-22T18:00" % slot["id"]        # starts 2026-10-22 18:00, cutoff 12h -> 06:00
        a = self.make_swimmer(visits=10)
        bk = a.ok("POST", "/bookings", {"session_key": key})["booking"]
        self.assertTrue(bk["can_cancel"])
        a.ok("DELETE", "/bookings/%s" % bk["id"])
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["available"], 10)
        bk = a.ok("POST", "/bookings", {"session_key": key})["booking"]      # rebook after cancel
        util.set_now(datetime(2026, 10, 22, 7, 0, tzinfo=config.tz()))
        self.assertEqual(a.call("DELETE", "/bookings/%s" % bk["id"])[1]["error"], "cancel_too_late")
        self.coach.ok("DELETE", "/bookings/%s" % bk["id"])                   # coach still can

    def test_cannot_book_started_or_far_sessions(self):
        slot = self.make_slot()
        a = self.make_swimmer(visits=10)
        util.set_now(datetime(2026, 10, 22, 18, 5, tzinfo=config.tz()))
        self.assertEqual(a.call("POST", "/bookings", {"session_key": "%s@2026-10-22T18:00" % slot["id"]})[1]["error"],
                         "too_late")
        self.assertEqual(a.call("POST", "/bookings", {"session_key": "%s@2026-12-31T18:00" % slot["id"]})[1]["error"],
                         "too_far")
        self.assertEqual(a.call("POST", "/bookings", {"session_key": "%s@2026-10-23T18:00" % slot["id"]})[1]["error"],
                         "session_not_found")   # wrong weekday

    def test_holiday_and_coach_cancellation(self):
        slot = self.make_slot(weekday=3)          # Thursday 2026-12-24 is Christmas Eve
        a = self.make_swimmer(visits=10)
        sess = self.coach.ok("GET", "/sessions?from=2026-12-24&to=2026-12-24")["sessions"][0]
        self.assertTrue(sess["cancelled"])
        self.assertEqual(sess["cancel_reason"], "holiday")
        key = "%s@2026-10-29T18:00" % slot["id"]
        a.ok("POST", "/bookings", {"session_key": key})
        out = self.coach.ok("PUT", "/overrides/%s" % key, {"cancelled": True, "note": "ремонт"})
        self.assertEqual(out["cancelled_bookings"], 1)
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["reserved"], 0)
        self.assertEqual(a.call("POST", "/bookings", {"session_key": key})[1]["error"], "session_cancelled")

    def test_package_expiry_and_templates(self):
        t = self.coach.ok("POST", "/package-templates", {"name": "10 посещения", "visits": 10, "valid_days": 30})
        a = self.make_swimmer()
        out = self.coach.ok("POST", "/packages", {"swimmer_id": a.id, "template_id": t["template"]["id"]})
        self.assertEqual(out["package"]["valid_until"], "2026-11-19")
        self.assertEqual(out["balance"]["available"], 10)
        self.coach.ok("POST", "/packages", {"swimmer_id": a.id, "visits": 2})            # no expiry
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["expiry"],
                         [{"date": "2026-11-19", "remaining": 10}, {"date": None, "remaining": 2}])
        util.set_now(datetime(2026, 11, 20, 10, 0, tzinfo=config.tz()))
        bal = a.ok("GET", "/credits/%s" % a.id)["balance"]
        self.assertEqual(bal["expiry"], [{"date": None, "remaining": 2}])
        bal["available"] -= 2   # the 2 open-ended visits; the dated card has expired
        self.assertEqual((bal["available"], bal["expired_unused"]), (0, 10))

    def test_unpaid_visit_covered_by_later_package(self):
        slot = self.make_slot()
        a = self.make_swimmer()
        self.coach.ok("POST", "/bookings", {"session_key": "%s@2026-10-22T18:00" % slot["id"], "swimmer_id": a.id,
                                            "force": True})
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["unpaid"], 1)
        self.coach.ok("POST", "/packages", {"swimmer_id": a.id, "visits": 10})
        bal = a.ok("GET", "/credits/%s" % a.id)["balance"]
        self.assertEqual((bal["unpaid"], bal["available"]), (0, 9))

    def test_delete_slot_cancels_future_bookings(self):
        slot = self.make_slot()
        a = self.make_swimmer(visits=10)
        a.ok("POST", "/bookings", {"session_key": "%s@2026-10-22T18:00" % slot["id"]})
        out = self.coach.ok("DELETE", "/availability/%s" % slot["id"])
        self.assertEqual(out["cancelled_bookings"], 1)
        self.assertEqual(self.coach.ok("GET", "/sessions?from=2026-10-22&to=2026-10-29")["sessions"], [])

    def test_availability_split_into_45_min_slots(self):
        out = self.coach.ok("POST", "/availability", {"weekdays": [3, 5], "start": "08:00", "end": "10:30"})
        self.assertEqual([w["slots"] for w in out["availability"]], [3, 3])       # 08:00 08:45 09:30
        self.assertEqual(out["availability"][0]["capacity"], 8)
        sess = self.coach.ok("GET", "/sessions?from=2026-10-22&to=2026-10-22")["sessions"]
        self.assertEqual([(s["start"], s["end"]) for s in sess], [("08:00", "08:45"), ("08:45", "09:30"),
                                                                     ("09:30", "10:15")])
        self.assertEqual(self.coach.call("POST", "/availability", {"weekdays": [1], "start": "08:00", "end": "09:00",
                                                                   "capacity": 9})[1]["error"], "invalid_field")
        self.assertEqual(self.coach.call("POST", "/availability", {"weekdays": [1], "start": "08:00",
                                                                   "end": "08:30"})[1]["error"], "window_too_short")

    def test_changing_hours_keeps_history_and_moves_bookings(self):
        w = self.coach.ok("POST", "/availability", {"weekdays": [2], "start": "08:00", "end": "09:30"})["availability"][0]
        a = self.make_swimmer(visits=10)
        # Wednesday 2026-10-21 08:00 already happened at T0 (10:00); book it as coach, and next week as swimmer
        self.coach.ok("POST", "/bookings", {"session_key": "%s@2026-10-21T08:00" % w["id"], "swimmer_id": a.id})
        a.ok("POST", "/bookings", {"session_key": "%s@2026-10-28T08:45" % w["id"]})
        a.ok("POST", "/bookings", {"session_key": "%s@2026-10-28T08:00" % w["id"]})
        out = self.coach.ok("PATCH", "/availability/%s" % w["id"], {"start": "08:45", "end": "10:15"})
        self.assertNotEqual(out["availability"]["id"], w["id"])                # new version from tomorrow
        self.assertEqual((out["moved_bookings"], out["cancelled_bookings"]), (1, 1))
        past = self.coach.ok("GET", "/sessions?from=2026-10-21&to=2026-10-21")["sessions"]
        self.assertEqual([s["start"] for s in past], ["08:00", "08:45"])       # history unchanged
        nxt = self.coach.ok("GET", "/sessions?from=2026-10-28&to=2026-10-28")["sessions"]
        self.assertEqual([(s["start"], s["booked"]) for s in nxt], [("08:45", 1), ("09:30", 0)])

    def test_comments_are_coach_only(self):
        a = self.make_swimmer()
        c = self.coach.ok("POST", "/users/%s/comments" % a.id, {"text": "Добра техника, да работи по дишането"})
        self.assertEqual(c["comment"]["author_name"], "Coach One")
        detail = self.coach.ok("GET", "/users/%s" % a.id)
        self.assertEqual(len(detail["comments"]), 1)
        self.assertNotIn("comments", a.ok("GET", "/users/%s" % a.id))
        self.assertEqual(a.call("GET", "/users/%s/comments" % a.id)[0], 403)
        self.coach.ok("PATCH", "/comments/%s" % c["comment"]["id"], {"text": "edited"})
        self.coach.ok("DELETE", "/comments/%s" % c["comment"]["id"])
        self.assertEqual(self.coach.ok("GET", "/users/%s/comments" % a.id)["comments"], [])

    def test_one_off_range_and_session_comments(self):
        out = self.coach.ok("POST", "/extra-sessions", {"date": "2026-10-24", "start": "09:00", "end": "10:30",
                                                        "duration_min": 45, "capacity": 8})
        self.assertEqual([x["start"] for x in out["sessions"]], ["09:00", "09:45"])
        key = out["sessions"][0]["key"]
        a = self.make_swimmer(visits=10)
        a.ok("POST", "/bookings", {"session_key": key})
        self.coach.ok("POST", "/users/%s/comments" % a.id, {"text": "общ коментар"})
        self.coach.ok("POST", "/users/%s/comments" % a.id, {"text": "за тази тренировка", "session_key": key})
        sess = self.coach.ok("GET", "/sessions?from=2026-10-24&to=2026-10-24&detail=1")["sessions"][0]
        att = sess["attendees"][0]
        self.assertEqual((att["credits"]["available"], att["phone"]), (9, "0888"))
        self.assertEqual([c["text"] for c in att["session_comments"]], ["за тази тренировка"])
        self.assertEqual(att["last_comment"]["text"], "общ коментар")
        self.assertNotIn("credits", a.ok("GET", "/sessions?from=2026-10-24&to=2026-10-24&detail=1")["sessions"][0])

    def test_avatars(self):
        import base64
        a = self.make_swimmer()
        b = self.make_swimmer("boris", "Борис")
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 50).decode()
        self.assertEqual(a.call("POST", "/users/%s/avatar" % a.id, {"image": "data:image/png;base64,AAAA"})[1]["error"],
                         "invalid_image")
        u = a.ok("POST", "/users/%s/avatar" % a.id, {"image": png})["user"]
        self.assertEqual(u["avatar"], 1)
        self.assertEqual(b.call("POST", "/users/%s/avatar" % a.id, {"image": png})[0], 404)   # not yours
        with self.coach.opener.open(self.base + "/api/v1/users/%s/avatar?v=1" % a.id) as r:
            self.assertEqual((r.headers["Content-Type"], r.read()[:4]), ("image/png", b"\x89PNG"))
        self.assertEqual(b.call("GET", "/users/%s/avatar" % a.id)[0], 404)                   # swimmers can't see each other
        self.coach.ok("POST", "/users/%s/avatar" % a.id, {"image": png})                    # coach may change it
        self.coach.ok("DELETE", "/users/%s/avatar" % a.id)
        self.assertEqual(self.coach.call("GET", "/users/%s/avatar" % a.id)[0], 404)
        self.assertIn("2026-12-24", a.ok("GET", "/holidays?from=2026-12-01&to=2026-12-31")["holidays"])
        self.assertEqual(a.ok("GET", "/me")["coach"]["name"], "Coach One")   # swimmers see whom to contact

    def test_delete_only_empty_and_cancel_is_visible(self):
        slot = self.make_slot(capacity=4)
        key = "%s@2026-10-29T18:00" % slot["id"]
        a = self.make_swimmer(visits=10)
        bk = a.ok("POST", "/bookings", {"session_key": key})["booking"]
        self.assertEqual(self.coach.call("DELETE", "/sessions/%s" % key)[1]["error"], "session_has_attendees")
        # future: no attended/no-show/excused
        self.assertEqual(self.coach.call("PATCH", "/bookings/%s" % bk["id"], {"status": "attended"})[1]["error"], "not_started")
        self.assertEqual(self.coach.call("PATCH", "/bookings/%s" % bk["id"], {"status": "excused"})[1]["error"], "not_started")
        # cancel: the swimmer still sees it, as cancelled by the coach
        self.coach.ok("PUT", "/overrides/%s" % key, {"note": "Друг басейн - Спартак"})
        self.assertEqual(a.ok("GET", "/sessions?from=2026-10-29&to=2026-10-29")["sessions"][0]["note"], "Друг басейн - Спартак")
        self.assertEqual(a.ok("GET", "/bookings")["bookings"][0]["session_note"], "Друг басейн - Спартак")
        self.coach.ok("PUT", "/overrides/%s" % key, {"cancelled": True})
        mine = a.ok("GET", "/sessions?from=2026-10-29&to=2026-10-29")["sessions"][0]["my_booking"]
        self.assertEqual((mine["status"], mine["by_coach"]), ("cancelled", True))
        coach_view = self.coach.ok("GET", "/sessions?from=2026-10-29&to=2026-10-29")["sessions"][0]
        self.assertEqual(coach_view["called_off"], ["Марк"])
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["reserved"], 0)
        self.assertEqual(self.coach.call("DELETE", "/sessions/%s" % key)[1]["error"], "session_has_attendees")
        # restore books them back
        out = self.coach.ok("PUT", "/overrides/%s" % key, {"cancelled": False})
        self.assertEqual(out["restored_bookings"], 1)
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["reserved"], 1)
        # empty date of a repeating slot can be deleted; it disappears
        empty = "%s@2026-11-05T18:00" % slot["id"]
        self.coach.ok("DELETE", "/sessions/%s" % empty)
        self.assertEqual(self.coach.ok("GET", "/sessions?from=2026-11-05&to=2026-11-05")["sessions"], [])
        self.assertEqual(a.call("POST", "/bookings", {"session_key": empty})[1]["error"], "session_not_found")

    def test_joint_account_books_one_place_per_member(self):
        slot = self.make_slot(capacity=3)
        key = "%s@2026-10-22T18:00" % slot["id"]
        fam = self.make_swimmer("family", "Семейство Иванови", visits=10)
        self.coach.ok("PATCH", "/users/%s" % fam.id, {"members": ["Иван", "Мария"]})
        self.assertEqual(fam.call("POST", "/bookings", {"session_key": key})[1]["error"], "pick_members")
        out = fam.ok("POST", "/bookings", {"session_key": key, "members": ["Иван", "Мария"]})
        self.assertEqual(sorted(b["member"] for b in out["bookings"]), ["Иван", "Мария"])
        self.assertEqual(fam.call("POST", "/bookings", {"session_key": key, "members": ["Иван"]})[1]["error"], "already_booked")
        bal = fam.ok("GET", "/credits/%s" % fam.id)["balance"]
        self.assertEqual((bal["reserved"], bal["available"]), (2, 8))
        sess = fam.ok("GET", "/sessions?from=2026-10-22&to=2026-10-22")["sessions"][0]
        self.assertEqual((len(sess["my_bookings"]), sess["free_members"], sess["can_book"]), (2, [], False))
        coach_sess = self.coach.ok("GET", "/sessions?from=2026-10-22&to=2026-10-22")["sessions"][0]
        self.assertEqual(sorted(a["member"] for a in coach_sess["attendees"]), ["Иван", "Мария"])
        # one of the two cancels; the other stays
        fam.ok("DELETE", "/bookings/%s" % out["bookings"][0]["id"])
        sess = fam.ok("GET", "/sessions?from=2026-10-22&to=2026-10-22")["sessions"][0]
        self.assertEqual((len(sess["my_bookings"]), sess["can_book"]), (1, True))
        # not enough visits for two -> nothing is booked
        poor = self.make_swimmer("poor", "Двама", visits=1)
        self.coach.ok("PATCH", "/users/%s" % poor.id, {"members": ["А", "Б"]})
        self.assertEqual(poor.call("POST", "/bookings", {"session_key": key, "members": ["А", "Б"]})[1]["error"], "no_credits")
        self.assertEqual(poor.ok("GET", "/credits/%s" % poor.id)["balance"]["reserved"], 0)
        self.assertEqual(self.coach.call("PATCH", "/users/%s" % poor.id, {"members": ["1", "2", "3", "4"]})[1]["error"],
                         "too_many_members")

    def test_coach_settings(self):
        slot = self.make_slot()
        key = "%s@2026-10-22T18:00" % slot["id"]
        a = self.make_swimmer(visits=10)
        self.assertEqual(a.ok("GET", "/me")["settings"]["cancel_cutoff_hours"], 12)
        self.assertEqual(a.call("PUT", "/me/settings", {"cancel_cutoff_hours": 1})[0], 403)
        out = self.coach.ok("PUT", "/me/settings", {"cancel_cutoff_hours": 2, "low_credit_threshold": 9,
                                                    "default_capacity": 6, "booking_horizon_days": 7})
        self.assertEqual(out["settings"]["default_capacity"], 6)
        self.assertEqual(self.coach.call("PUT", "/me/settings", {"default_capacity": 9})[1]["error"], "invalid_field")
        self.assertEqual(a.ok("GET", "/me")["settings"]["cancel_cutoff_hours"], 2)       # swimmers follow their coach
        bk = a.ok("POST", "/bookings", {"session_key": key})["booking"]
        util.set_now(datetime(2026, 10, 22, 15, 30, tzinfo=config.tz()))                 # 2.5h before 18:00
        self.assertTrue(a.ok("GET", "/bookings")["bookings"][0]["can_cancel"])          # default 12h would say no
        self.assertTrue(a.ok("GET", "/credits/%s" % a.id)["balance"]["low"])            # 9 left <= 9
        self.assertEqual(a.call("POST", "/bookings", {"session_key": "%s@2026-11-12T18:00" % slot["id"]})[1]["error"], "too_far")
        self.coach.ok("PUT", "/me/settings", {"swimmers_can_book": False})
        self.assertEqual(a.call("POST", "/bookings", {"session_key": "%s@2026-10-29T18:00" % slot["id"]})[1]["error"],
                         "self_booking_disabled")
        self.assertFalse(a.ok("GET", "/sessions?from=2026-10-29&to=2026-10-29")["sessions"][0]["can_book"])
        # visit notes: both sides write, both sides read
        self.coach.ok("POST", "/bookings/%s/note" % bk["id"], {"text": "Браво, добро темпо"})
        a.ok("POST", "/bookings/%s/note" % bk["id"], {"text": "Ще закъснея 10 мин"})
        mine = a.ok("GET", "/bookings")["bookings"][0]
        self.assertEqual((mine["note"], mine["swimmer_note"]), ("Браво, добро темпо", "Ще закъснея 10 мин"))
        att = self.coach.ok("GET", "/sessions?from=2026-10-22&to=2026-10-22")["sessions"][0]["attendees"][0]
        self.assertEqual(att["swimmer_note"], "Ще закъснея 10 мин")
        other = self.make_swimmer("boris", "Борис")
        self.assertEqual(other.call("POST", "/bookings/%s/note" % bk["id"], {"text": "x"})[0], 404)

    def test_past_visits_are_protected(self):
        slot = self.make_slot()
        key = "%s@2026-10-22T18:00" % slot["id"]
        a = self.make_swimmer(visits=10)
        bk = a.ok("POST", "/bookings", {"session_key": key})["booking"]
        self.assertEqual(a.call("POST", "/bookings/%s/ack" % bk["id"], {"action": "confirm"})[1]["error"], "ack_closed")
        util.set_now(datetime(2026, 10, 22, 20, 0, tzinfo=config.tz()))          # training over
        mine = a.ok("GET", "/bookings")["bookings"][0]
        self.assertEqual((mine["status"], mine["can_ack"], mine["protected"]), ("attended", True, False))
        # inside the window the coach can still correct freely - and the swimmer sees it
        out = self.coach.ok("PATCH", "/bookings/%s" % bk["id"], {"status": "no_show"})
        self.assertFalse(out["requested"])
        self.assertTrue(a.ok("GET", "/bookings?attention=1")["bookings"][0]["unseen"])
        self.coach.ok("PATCH", "/bookings/%s" % bk["id"], {"status": "attended"})
        a.ok("POST", "/bookings/%s/ack" % bk["id"], {"action": "confirm"})
        self.assertEqual(a.ok("GET", "/bookings?attention=1")["bookings"], [])
        # confirmed -> locked: costing change becomes a request, refund goes through
        out = self.coach.ok("PATCH", "/bookings/%s" % bk["id"], {"status": "no_show"})
        self.assertTrue(out["requested"])
        self.assertEqual(out["booking"]["status"], "attended")
        a.ok("POST", "/bookings/%s/ack" % bk["id"], {"action": "reject"})
        self.assertEqual(a.ok("GET", "/bookings")["bookings"][0]["status"], "attended")
        self.assertFalse(self.coach.ok("PATCH", "/bookings/%s" % bk["id"], {"status": "excused"})["requested"])
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["used"], 0)
        out = self.coach.ok("PATCH", "/bookings/%s" % bk["id"], {"status": "attended"})
        self.assertTrue(out["requested"])                                        # costs a visit again
        a.ok("POST", "/bookings/%s/ack" % bk["id"], {"action": "approve"})
        hist = [h["kind"] for h in a.ok("GET", "/bookings")["bookings"][0]["history"]]
        self.assertEqual(hist, ["status", "status", "confirm", "requested", "rejected", "status", "requested", "approved"])
        # dispute within the window, coach resolves
        old_key = self.coach.ok("POST", "/extra-sessions", {"date": "2026-10-15", "start": "18:00", "duration_min": 60,
                                                            "capacity": 4})["session"]["key"]
        bk2 = self.coach.ok("POST", "/bookings", {"session_key": old_key, "swimmer_id": a.id})
        util.set_now(datetime(2026, 10, 23, 9, 0, tzinfo=config.tz()))
        # 15.10 + 3 days is long gone: adding a visit there needs the swimmer's approval
        b2 = bk2["booking"]
        self.assertEqual((b2["status"], b2["pending"]["status"]), ("cancelled", "attended"))
        self.assertEqual(self.coach.ok("GET", "/bookings?attention=1")["bookings"][0]["id"], b2["id"])
        a.ok("POST", "/bookings/%s/ack" % b2["id"], {"action": "approve"})
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["used"], 2)
        bk3 = a.ok("POST", "/bookings", {"session_key": "%s@2026-10-29T18:00" % slot["id"]})["booking"]
        util.set_now(datetime(2026, 10, 30, 9, 0, tzinfo=config.tz()))
        self.assertEqual(a.call("POST", "/bookings/%s/ack" % bk3["id"], {"action": "dispute"})[1]["error"], "invalid_field")
        a.ok("POST", "/bookings/%s/ack" % bk3["id"], {"action": "dispute", "comment": "Не бях там - болен"})
        att = self.coach.ok("GET", "/sessions?from=2026-10-29&to=2026-10-29")["sessions"][0]["attendees"][0]
        self.assertEqual((att["ack"]["status"], att["protected"]), ("disputed", True))
        self.coach.ok("POST", "/bookings/%s/resolve" % bk3["id"])
        self.assertEqual(self.coach.call("POST", "/bookings/%s/ack" % bk3["id"], {"action": "confirm"})[0], 403)
        util.set_now(datetime(2026, 11, 3, 9, 0, tzinfo=config.tz()))            # window passed on its own
        self.assertFalse(a.ok("GET", "/bookings?from=2026-10-29&to=2026-10-29")["bookings"][0]["can_ack"])

    def test_waitlist(self):
        slot = self.make_slot(capacity=1)
        key = "%s@2026-10-22T18:00" % slot["id"]
        a = self.make_swimmer("anna", "Анна", visits=3)
        out = a.ok("POST", "/bookings", {"session_key": key})
        b = self.make_swimmer("boris", "Борис", visits=5)
        self.assertEqual(b.call("POST", "/bookings", {"session_key": key})[1]["error"], "full")
        b.ok("POST", "/waitlist", {"session_key": key})
        sess = b.ok("GET", "/sessions?from=2026-10-22&to=2026-10-22")["sessions"][0]
        self.assertEqual(sess["my_waitlist"][0]["position"], 1)
        self.assertEqual(b.call("POST", "/waitlist", {"session_key": key})[1]["error"], "already_waiting")
        a.ok("DELETE", "/bookings/%s" % out["bookings"][0]["id"])           # a place frees up
        mine = b.ok("GET", "/bookings?attention=1")["bookings"]
        self.assertEqual((len(mine), mine[0]["waitlist_notice"]), (1, True))
        self.assertEqual(b.ok("GET", "/sessions?from=2026-10-22&to=2026-10-22")["sessions"][0]["my_waitlist"], [])

    def test_absence_cancels_and_extends_cards(self):
        slot = self.make_slot(capacity=4)
        a = self.make_swimmer(visits=10)
        p = self.coach.ok("POST", "/packages", {"swimmer_id": a.id, "visits": 5, "valid_days": 30})["package"]
        a.ok("POST", "/bookings", {"session_key": "%s@2026-10-29T18:00" % slot["id"]})
        out = self.coach.ok("POST", "/absences", {"from": "2026-10-26", "to": "2026-11-01", "reason": "Състезание"})["absence"]
        self.assertEqual((out["days"], out["cancelled_bookings"], len(out["extended"])), (7, 1, 1))
        sess = a.ok("GET", "/sessions?from=2026-10-29&to=2026-10-29")["sessions"][0]
        self.assertTrue(sess["cancelled"]); self.assertIn("Състезание", sess["note"])
        pk = [x for x in a.ok("GET", "/credits/%s" % a.id)["balance"]["packages"] if x["id"] == p["id"]][0]
        self.assertEqual(pk["valid_until"], "2026-11-26")                   # 19.11 + 7 days
        self.coach.ok("DELETE", "/absences/%s" % out["id"])                  # undo
        self.assertFalse(a.ok("GET", "/sessions?from=2026-10-29&to=2026-10-29")["sessions"][0]["cancelled"])
        self.assertEqual(a.ok("GET", "/credits/%s" % a.id)["balance"]["reserved"], 1)
        pk = [x for x in a.ok("GET", "/credits/%s" % a.id)["balance"]["packages"] if x["id"] == p["id"]][0]
        self.assertEqual(pk["valid_until"], "2026-11-19")

    def test_messages_and_insights(self):
        slot = self.make_slot()
        a = self.make_swimmer("anna", "Анна", visits=2)
        b = self.make_swimmer("boris", "Борис", visits=10)
        a.ok("POST", "/bookings", {"session_key": "%s@2026-10-22T18:00" % slot["id"]})
        self.coach.ok("POST", "/messages", {"text": "Утре сме в басейн 2", "audience": "day", "date": "2026-10-22"})
        self.coach.ok("POST", "/messages", {"text": "Честит празник!"})
        self.assertEqual(sorted(m["text"] for m in a.ok("GET", "/messages")["messages"]), ["Утре сме в басейн 2", "Честит празник!"])
        self.assertEqual([m["text"] for m in b.ok("GET", "/messages")["messages"]], ["Честит празник!"])
        mid = [m for m in a.ok("GET", "/messages")["messages"] if m["text"] == "Честит празник!"][0]["id"]
        a.ok("POST", "/messages/%s/read" % mid)
        sent = [m for m in self.coach.ok("GET", "/messages")["messages"] if m["id"] == mid][0]
        self.assertEqual((sent["read_count"], sent["recipient_count"]), (1, 2))
        self.assertEqual(b.call("POST", "/messages", {"text": "x"})[0], 403)
        ins = self.coach.ok("GET", "/insights")
        self.assertEqual([r["name"] for r in ins["inactive"]], ["Борис"])           # Анна has a booking
        self.assertEqual([r["name"] for r in ins["low"]], ["Анна"])                 # 1 left

    def test_sessions_include_holidays(self):
        out = self.coach.ok("GET", "/sessions?from=2026-12-21&to=2026-12-27")
        self.assertEqual(sorted(out["holidays"]), ["2026-12-24", "2026-12-25", "2026-12-26"])

    def test_migrates_v1_slots(self):
        d = {"version": 1, "slots": {"s_1": {"id": "s_1", "coach_id": "u", "weekday": 3, "start": "18:00",
                                             "duration_min": 60, "capacity": 6}},
             "bookings": {"b": {"session_key": "s_1@2026-10-22"}}, "overrides": {"s_1@2026-10-29": {"cancelled": True}}}
        store.migrate(d)
        self.assertEqual(d["availability"]["s_1"]["end"], "19:00")
        self.assertEqual(d["bookings"]["b"]["session_key"], "s_1@2026-10-22T18:00")
        self.assertIn("s_1@2026-10-29T18:00", d["overrides"])

    # stats, export, reports -------------------------------------------------

    def test_stats_export_and_daily_report(self):
        slot = self.make_slot()
        key = "%s@2026-10-22T18:00" % slot["id"]
        a = self.make_swimmer("anna", "Анна", visits=10)
        b = self.make_swimmer("boris", "Борис", visits=2)
        a.ok("POST", "/bookings", {"session_key": key})
        bk = b.ok("POST", "/bookings", {"session_key": key})["booking"]
        util.set_now(datetime(2026, 10, 22, 21, 0, tzinfo=config.tz()))
        self.coach.ok("PATCH", "/bookings/%s" % bk["id"], {"status": "no_show"})
        st = self.coach.ok("GET", "/stats?from=2026-10-01&to=2026-10-31")
        self.assertEqual((st["totals"]["attended"], st["totals"]["no_show"]), (1, 1))
        self.assertEqual(st["attendance_rate"], 0.5)
        self.assertEqual((st["sessions_held"], st["avg_per_session"]), (1, 1.0))   # empty slots are not "held"
        csv_text = self.coach.ok("GET", "/export.csv?from=2026-10-01&to=2026-10-31", raw=True)
        self.assertIn("Анна", csv_text)

        paths = reports.run_daily(datetime(2026, 10, 22).date())
        self.assertEqual(len(paths), 4)
        rep = json.loads(self.coach.ok("GET", "/reports/2026-10-22/json", raw=True))
        statuses = {x["name"]: x["status"] for x in rep["sessions"][0]["attendees"]}
        self.assertEqual(statuses, {"Анна": "attended", "Борис": "no_show"})
        boris = [x for x in rep["balances"] if x["name"] == "Борис"][0]
        self.assertEqual((boris["available"], boris["low"]), (1, True))
        txt = self.coach.ok("GET", "/reports/2026-10-22/txt", raw=True)
        self.assertIn("Дневен отчет", txt)
        self.assertEqual(self.coach.ok("GET", "/reports")["reports"][0]["date"], "2026-10-22")
        self.assertEqual(a.call("GET", "/reports")[0], 403)
        # the report run also wrote the auto-consumed state into data.json
        with store.LOCK:
            statuses = {x["status"] for x in store.current()["bookings"].values()}
        self.assertEqual(statuses, {"attended", "no_show"})

    def test_report_catchup_dates(self):
        cid = self.coach_id
        self.coach.ok("PUT", "/me/settings", {"report_time": "21:30"})
        util.set_now(datetime(2026, 10, 21, 22, 0, tzinfo=config.tz()))
        self.assertEqual([d.isoformat() for d in reports.due_dates(cid)], ["2026-10-21"])
        reports.run_daily(datetime(2026, 10, 21).date())
        util.set_now(datetime(2026, 10, 22, 12, 0, tzinfo=config.tz()))   # manual run before report_time
        reports.run_daily(datetime(2026, 10, 22).date())
        util.set_now(datetime(2026, 10, 24, 9, 0, tzinfo=config.tz()))   # server was down for two days
        self.assertEqual([d.isoformat() for d in reports.due_dates(cid)], ["2026-10-22", "2026-10-23"])
        # each coach has their own time: at 22:00 a 23:00 coach is not due yet
        self.coach.ok("PUT", "/me/settings", {"report_time": "23:00"})
        util.set_now(datetime(2026, 10, 24, 22, 0, tzinfo=config.tz()))
        self.assertEqual([d.isoformat() for d in reports.due_dates(cid)][-1], "2026-10-23")

    def test_report_email_settings(self):
        self.assertEqual(self.coach.call("PUT", "/me/settings", {"report_to": "not-an-email"})[1]["error"], "invalid_email")
        self.assertEqual(self.coach.call("PUT", "/me/settings", {"report_attach": ["pdf"]})[1]["error"], "invalid_field")
        out = self.coach.ok("PUT", "/me/settings", {"report_email": True, "report_to": "a@b.bg, c@d.com",
                                                    "report_attach": ["visits", "txt"], "report_time": "23:15"})["settings"]
        self.assertEqual((out["report_attach"], out["report_time"]), (["txt", "visits"], "23:15"))
        rep = self.coach.ok("GET", "/reports")
        self.assertEqual((rep["recipients"], rep["mail_configured"]), (["a@b.bg", "c@d.com"], False))
        self.assertEqual(self.coach.call("POST", "/reports/run", {"email": True})[1]["error"], "mail_not_configured")
        # with a sender configured, the mail goes out through the notifier (mailer stubbed here)
        from app import mailer
        sent = []

        class FakeMailer:
            last_log = ["mx: accepted"]

            def __init__(self, *a, **k):
                pass

            def send(self, to, subject, text, html=None, attachments=()):
                sent.append((to, subject, [f[0] for f in attachments]))
                return True
        real = mailer.DirectMailer
        mailer.DirectMailer = FakeMailer
        config.CONFIG["mail_sender"] = "Test <noreply@example.com>"
        try:
            res = self.coach.ok("POST", "/reports/run", {"email": True})
        finally:
            mailer.DirectMailer = real
            config.CONFIG["mail_sender"] = ""
        self.assertTrue(res["mail_status"]["ok"])
        self.assertEqual([s[0] for s in sent], ["a@b.bg", "c@d.com"])
        self.assertEqual(len(sent[0][2]), 1)
        self.assertTrue(sent[0][2][0].endswith(".zip"))          # visits CSV inside one ZIP

    def test_json_content_type_required(self):
        req = urllib.request.Request(self.base + "/api/v1/login", data=b"username=coach&password=x", method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 415)

    def test_changes_long_poll(self):
        rev = self.coach.ok("GET", "/changes")["rev"]
        self.assertEqual(self.coach.ok("GET", "/changes?since=%d&wait=0" % rev)["rev"], rev)
        self.make_slot()
        self.assertGreater(self.coach.ok("GET", "/changes?since=%d&wait=1" % rev)["rev"], rev)

    def test_static_index(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"<html", r.read())


if __name__ == "__main__":
    unittest.main()
