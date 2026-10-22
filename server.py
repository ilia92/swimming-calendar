#!/usr/bin/env python3
"""Swimming Calendar - server entry point and admin CLI.

    python3 server.py                          run the web server (port from config.json, default 8870)
    python3 server.py --add-coach USER "Name" [--admin]
    python3 server.py --reset-password USER    prints a new one-time password
    python3 server.py --list-users
    python3 server.py --daily-report [YYYY-MM-DD]   write the daily report now (default: today)
"""

import argparse
import logging
import sys

from app import api, auth, config, reports, store, web
from app.util import parse_date, today


def cli(argv):
    p = argparse.ArgumentParser(description="Swimming Calendar")
    p.add_argument("--config", help="path to config.json")
    p.add_argument("--port", type=int)
    p.add_argument("--add-coach", nargs=2, metavar=("USERNAME", "NAME"))
    p.add_argument("--admin", action="store_true", help="with --add-coach: may manage other coaches")
    p.add_argument("--reset-password", metavar="USERNAME")
    p.add_argument("--list-users", action="store_true")
    p.add_argument("--daily-report", nargs="?", const="", metavar="DATE")
    return p.parse_args(argv)


def main(argv=None):
    args = cli(sys.argv[1:] if argv is None else argv)
    config.load(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.add_coach:
        username, name = args.add_coach
        try:
            with store.LOCK:
                first = not any(u["role"] == "coach" for u in store.current()["users"].values())
            u, password = api.create_user_cli("coach", username, name, is_admin=args.admin or first)
        except Exception as e:
            sys.exit("error: %s" % getattr(e, "message", e))
        print("created coach %s (%s)%s" % (u["username"], u["name"], " [admin]" if u["is_admin"] else ""))
        print("one-time password: %s   (must be changed at first login)" % password)
        return

    if args.reset_password:
        password = auth.generate_password()

        def fn(d):
            u = auth.find_user(d, args.reset_password.lower())
            if not u:
                sys.exit("no such user: %s" % args.reset_password)
            u["pw_hash"] = auth.hash_password(password)
            u["must_change_password"] = True
            u["active"] = True
            store.audit(d, "cli", "password_reset", u["id"])
            return u
        u = store.mutate(fn)
        auth.destroy_user_sessions(u["id"])
        print("one-time password for %s: %s" % (u["username"], password))
        return

    if args.list_users:
        with store.LOCK:
            d = store.current()
            users = sorted(d["users"].values(), key=lambda u: (u["role"], u["name"].lower()))
            for u in users:
                coach = d["users"].get(u.get("coach_id"), {}).get("username", "")
                flags = [f for f, on in (("admin", u.get("is_admin")), ("inactive", not u.get("active", True)),
                                         ("must-change-pw", u.get("must_change_password"))) if on]
                print("%-8s %-20s %-28s %-14s %s" % (u["role"], u["username"], u["name"], coach, ",".join(flags)))
        return

    if args.daily_report is not None:
        day = parse_date(args.daily_report) if args.daily_report else today()
        for path in reports.run_daily(day):
            print(path)
        return

    port = args.port or int(config.CONFIG["port"])
    httpd = web.make_server(config.CONFIG["host"], port)
    sched = reports.Scheduler()
    sched.start()
    logging.info("Swimming Calendar on http://%s:%d", config.CONFIG["host"], port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop_event.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
