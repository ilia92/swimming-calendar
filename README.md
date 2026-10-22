# Swimming Calendar / Плувен календар

A web app for a swimming coach and the coach's swimmers. The coach sets the hours they
are available, split into bookable slots (45 min, up to 8 swimmers each by default).
Swimmers book from their phone and pay with prepaid visit cards ("10 visits"). A daily
report is written to disk as a fallback that doesn't depend on the web app.

- **No dependencies.** It's Python 3.9+ standard library plus vanilla JS. There's no
  build step and no database: everything is stored in `data/data.json`.
- **API first.** The UI only talks to `/api/v1` (see [docs/API.md](docs/API.md)).
- **Mobile first.** Swimmers and the coach both work from a phone; desktop gets more detail.
- **Bulgarian by default, English available.** Each user picks a language with the flags
  in the header. The code, API and data are in English.

## Features

**Calendar**
- Month, week and day views. Weeks start on Monday, and weekends and Bulgarian public
  holidays are shown in red.
- Month cells list each slot's fill on desktop. The week view shows the attendees'
  avatars on each slot. The day view expands every slot in place, with no popups.
- Changes appear live for everyone, through long-polling. A notice appears if the server
  can't be reached, and the page reconnects on its own.
- On a phone, swipe left/right to change the day or month. On desktop, use the keyboard:
  ← → navigate, **T** today, **D / W / M** for day / week / month.

**Coach**
- **Availability:** "Mon–Fri 07:00–12:00" is split automatically into slots. "+ Слот"
  creates a one-off slot or a weekly repeating one. Changing the hours keeps past
  sessions in the history and moves upcoming bookings along.
- **A slot shows** the booked swimmers with photo, phone, visits left and notes. From
  there the coach can:
  - mark them visited / no-show / excused (only once the session has started)
  - add a swimmer through a searchable dropdown
  - change the places or the note
  - cancel the training
  - delete it, which is allowed only when nobody is booked
- **Cancelling a training** returns the visits, and the affected swimmers see the
  cancellation. Restoring it books them back in.
- **Swimmers:** accounts are created with a generated one-time password that must be
  changed at first login. A swimmer has a photo, private coach comments 🔒, and visit
  cards with optional expiry. Adding a card always asks for confirmation.
- **Joint accounts:** one card shared by up to 3 people (e.g. siblings). Each member
  books their own place in a slot and is marked separately.
- **Rules** (⚙ → Правила), set per coach:
  - the cancel deadline
  - how far ahead swimmers can book
  - the low-visits warning
  - default slot length and places
  - whether swimmers may book themselves
  - auto-cancel on public holidays
- The swimmers list has filters (few visits, unpaid, no card) and sorting, plus tap-to-call
  phone numbers.
- Statistics (attendance, trainings that had visitors, places taken), CSV export, daily
  reports and a change log.
- An admin coach can create other coaches.

**Swimmer**
- Books and cancels within the coach's rules, and sees visits left plus when they
  expire (with a warning when expiry is close).
- Sees trainings the coach cancelled, and their visit history.
- Can write a note on each visit ("10 min late"). The coach can write one back
  ("great pace today").
- Taps a free time in the month view to book it in one tap, with undo in the toast, and
  sees the next training and their coach's contact details.

## How visits are counted

- A booking reserves one visit per person. Once the session has ended it is **used
  automatically**. The coach can correct it afterwards: *excused* gives the visit back,
  *no-show* keeps it used.
- Visits are taken from the card that expires first. Visits not covered by any card show
  as **unpaid**, and a card added later covers them.

## Past visits are protected

This means the customer can trust the record of what they paid for.

- After a training ends, the swimmer has **3 days** (`ack_window_days`) to **confirm ✓** or
  **dispute** the visit, with a comment. After that the visit locks on its own.
  Confirming locks it sooner.
- On a locked visit, a coach change that **favours the swimmer** is applied right away:
  giving a visit back (excused/removed), or no-show → attended. A change that **costs** the
  swimmer becomes a **request** the swimmer approves or rejects. That covers marking them
  attended/no-show on a visit they didn't use, attended → no-show, and adding them to an
  old training.
- Every change to a past visit is kept in that visit's history, which the swimmer can see.
  A banner and a badge show them anything new.
- A disputed visit is flagged for the coach until they mark it **resolved**.

## Running

```bash
cp config.json.example config.json          # adjust if needed (port 8870)
python3 server.py --add-coach ivan "Иван Петров"    # prints a one-time password; the first coach is admin
python3 server.py                           # http://<host>:8870
```

As a service:

```bash
sudo cp swimming-calendar.service /etc/systemd/system/   # adjust paths/user inside first
sudo systemctl daemon-reload && sudo systemctl enable --now swimming-calendar
```

Hourly backups of `data/data.json`, plus a mirror of the profile photos:

```bash
crontab -e
0 * * * * /path/to/swimming-calendar/backup.sh >> /path/to/swimming-calendar/backup.log 2>&1
```

Other CLI commands:

```bash
python3 server.py --list-users
python3 server.py --reset-password USERNAME       # new one-time password, also reactivates
python3 server.py --daily-report [YYYY-MM-DD]     # write a report now
```

Restart the service after updating the Python code. Changes under `static/` only need a
page reload.

## Configuration (`config.json`)

These settings are server-wide. The ones under **Rules** above are defaults, and each
coach can override them in the app.

| Key | Default | |
|---|---|---|
| `port`, `host` | 8870, 0.0.0.0 | |
| `timezone` | Europe/Sofia | |
| `default_lang` | bg | |
| `report_time` | 21:30 | daily report time |
| `max_capacity` | 8 | hard maximum of swimmers per slot |
| `max_members` | 3 | people on a joint account |
| `cancel_cutoff_hours`, `booking_horizon_days`, `low_credit_threshold`, `default_slot_minutes` | 12, 28, 2, 45 | defaults for each coach's rules |
| `cookie_secure` | false | set `true` once served over HTTPS only |
| `backup_dir`, `backup_keep` | ../backup_swimming_calendar, 1000 | |
| `notifiers.email` | disabled | placeholder for e-mailed reports |

## Daily report

Every day at `report_time`, the server writes the following for each coach:

```
reports/YYYY/MM/YYYY-MM-DD_<coach>.txt            readable summary in the coach's language
reports/YYYY/MM/YYYY-MM-DD_<coach>_visits.csv     who came to which session, with status
reports/YYYY/MM/YYYY-MM-DD_<coach>_balances.csv   visits left for every swimmer
reports/YYYY/MM/YYYY-MM-DD_<coach>.json           full snapshot
```

If the server was down, the missing days are written when it starts again. Only the file
notifier exists today. E-mail delivery has a placeholder in `app/reports.py` and the config.

## Files

```
server.py            entry point + CLI
app/config.py        config.json + defaults
app/prefs.py         per-coach rules (fall back to config.json)
app/store.py         JSON storage: atomic writes, lock, revision counter, migrations, audit log
app/auth.py          PBKDF2 passwords, persisted sessions, login rate limit, permissions
app/schedule.py      availability -> slots, one-off sessions, overrides, BG holidays
app/credits.py       visit balance, expiry, auto-consumption
app/reports.py       daily report, notifiers, scheduler
app/api.py           /api/v1 endpoints
app/web.py           HTTP server, static files, security headers
static/              single-page UI (app.js, styles.css), i18n/bg.json + en.json, flags
tests/test_api.py    end-to-end API tests
```

Tests: `python3 -m unittest discover tests -v`

Data that is **not** in git: `config.json`, `data/` (`data.json`, `sessions.json`,
`avatars/`) and `reports/`.

## Security notes

- Passwords are stored as salted PBKDF2-SHA256. Sessions are random tokens, and only
  their SHA-256 is persisted.
- Cookies are `HttpOnly; SameSite=Strict`. Request bodies must be JSON, which also guards
  against CSRF. The page has a strict Content-Security-Policy.
- Login is limited to 5 failures per 15 minutes per username and IP. A restart clears
  the lock.
- Every permission check happens on the server. A swimmer never sees other swimmers'
  data, and private coach comments are never sent to swimmers.
- Serve it over HTTPS through a reverse proxy when it is exposed to the internet, and
  then set `"cookie_secure": true`.

## Possible next steps

- A "Today" view for the pool deck: check-in, walk-ins, an end-of-session prompt
- E-mail or Telegram delivery of the daily report
- A waiting list for full slots
- An ICS calendar feed per swimmer
- Reminders the evening before a session
