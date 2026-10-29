# API v1

Base path: `/api/v1`. Requests and responses are JSON. Request bodies must be sent with
`Content-Type: application/json`, which also acts as a CSRF guard. Authentication uses the
`swim_session` cookie, which `POST /login` sets.

Errors look like `{"error": "<code>", "message": "<english text>", ...}`. The UI translates
`code` using `errors.<code>` in `static/i18n/*.json`. Typical codes include `unauthorized`
(401), `forbidden` (403), `not_found` (404), `full`, `no_credits`, `already_booked`,
`too_late`, `too_far`, `cancel_too_late`, `session_cancelled`, `session_has_attendees`,
`not_started` and `self_booking_disabled` (409/403), and `invalid_field` (400, with a
`field` key).

A user with `must_change_password` gets `403 password_change_required` from everything
except `/me`, `/me/password`, `/logout` and `/changes`. `GET /api/v1` lists every endpoint.

## Auth, profile, coach rules

| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/login` | anyone | `{username, password}` → `{user}` + cookie. Limited to 5 failures per 15 min per user and IP |
| POST | `/logout` | anyone | |
| GET | `/me` | user | `{user, settings, today, now}`. `settings` are the effective rules of the user's coach |
| PATCH | `/me` | user | `{lang?, phone?, email?}`; coach also `{name?}` |
| POST | `/me/password` | user | `{current_password, new_password}` (min 8 characters) |
| GET / PUT | `/me/settings` | coach | Per-coach rules: `cancel_cutoff_hours`, `booking_horizon_days`, `low_credit_threshold`, `default_slot_minutes`, `default_capacity`, `swimmers_can_book`, `skip_holidays` |

## Users

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/swimmers?include_inactive=1&coach_id=` | coach | Each swimmer with `balance`, `last_visit`, `next_booking` |
| POST | `/swimmers` | coach | `{username, name, phone?, email?, notes?, members?, coach_id? (admin)}` → `{user, password}`. The password is shown once |
| GET / POST | `/coaches` | coach / admin | Admins list and create coaches |
| GET | `/users/{id}` | self, or the swimmer's coach | Swimmers also get `balance` and `bookings`; coaches also see `comments` |
| PATCH | `/users/{id}` | coach (own swimmers), admin | `{name, username, phone, email, notes, members, lang, active, coach_id (admin), is_admin (admin)}` |
| POST | `/users/{id}/reset-password` | coach, admin | → `{user, password}` |
| GET / POST / DELETE | `/users/{id}/avatar` | self, or coach | POST `{image: "data:image/jpeg;base64,…"}`, at most 200 KB (the UI resizes to 320 px). GET serves the picture |
| GET / POST | `/users/{id}/comments` | coach | Private coach comments `{text, session_key?}` |
| PATCH / DELETE | `/comments/{id}` | author, admin | |

`members` is the list of people on a **joint account** (1…`max_members` names, e.g. a parent's account with their kids). Such an
account books one place per member; with a single member it is picked automatically.

## Schedule

A session key is `"<window_id>@YYYY-MM-DDTHH:MM"` for a slot of an availability window, or
`"x:<id>"` for a one-off slot.

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/sessions?from=&to=&coach_id=&detail=1` | user | Max 62 days. Each session has `booked`, `free`, `started`, `cancelled`. Coaches also get `attendees` (with `member`, `note`, `swimmer_note`, and with `detail=1` also `credits`, `phone` and comments) plus `called_off`. Swimmers get `my_bookings`, `free_members` and `can_book`. The response also includes `holidays` |
| GET / POST | `/availability` | coach | POST `{weekdays: [0=Mon…6], start, end, slot_minutes?, capacity?, location?, title?, valid_from?, valid_to?}` creates one window per weekday. Without `valid_to` it ends `window_months` (3) after `valid_from` |
| PATCH / DELETE | `/availability/{id}` | coach | Changing the timing starts a new version from today, keeping past sessions. `valid_to: null` (or editing an older open-ended window) sets the end to `window_months` from today. Upcoming bookings move along or are cancelled |
| POST | `/extra-sessions` | coach | `{date, start, end?, duration_min, capacity, …}`. With `end`, the range is split into slots |
| PUT | `/overrides/{key}` | coach | `{cancelled?, capacity?, note?}` for one date. Cancelling releases the bookings (the swimmers see why), and restoring re-books them |
| DELETE | `/overrides/{key}` | coach | Puts that date back on the normal schedule |
| DELETE | `/sessions/{key}` | coach | Deletes one training, but only if nobody is booked or was called off |
| GET | `/holidays?from=&to=` | user | Bulgarian public holidays |

## Bookings

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/bookings?swimmer_id=&from=&to=&status=` | user | Swimmers only ever get their own |
| POST | `/bookings` | user | `{session_key, members?}`. A coach also passes `swimmer_id`, and `force: true` to ignore capacity and visit limits. → `{booking, bookings}` |
| DELETE | `/bookings/{id}` | owner (before the cutoff), coach | Sets the status to `cancelled` |
| PATCH | `/bookings/{id}` | coach | `{status}`. `attended`, `no_show` and `excused` are allowed only once the session has started |
| POST | `/bookings/{id}/note` | owner, coach | `{text}`. The coach writes `note`, the swimmer writes `swimmer_note`, and both see both |
| POST | `/bookings/{id}/ack` | owner (swimmer) | `{action: confirm \| dispute \| approve \| reject \| seen, comment?}`. Confirms or disputes a past visit, or answers a coach request |
| POST | `/bookings/{id}/resolve` | coach | Marks a disputed visit as resolved |
| GET | `/bookings?attention=1` | user | Swimmers get coach requests and unseen changes. Coaches get disputes and requests that are still open |

Past visits: each booking has `ended`, `protected`, `can_ack`, `ack_until`, `ack`
(`confirmed`/`disputed`/`resolved`/`changed`), `pending` (a coach request awaiting approval)
and `history`. On a protected visit, `PATCH` with a change that costs the swimmer returns
`{"requested": true}` and is stored as `pending` instead of being applied.

Possible statuses are `booked`, `attended`, `no_show`, `excused` and `cancelled`.
`reason` explains coach-side cancellations. A `booked` booking whose session has ended
counts as `attended`.

## Visits

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/credits/{swimmer_id}` | self, coach | `{balance: {total, used, reserved, available, unpaid, expired_unused, low, expiry: [{date, remaining}], packages[]}}` |
| GET / POST | `/package-templates` | coach | `{name, visits, valid_days?}` |
| PATCH / DELETE | `/package-templates/{id}` | coach | |
| POST | `/packages` | coach | `{swimmer_id, template_id}` or `{swimmer_id, visits, valid_days?, valid_until?, note?}` |
| DELETE | `/packages/{id}` | coach | Only for fixing a mistake |

## Statistics, reports, sync

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/stats?from=&to=&swimmer_id=` | user | Totals, attendance rate, and breakdowns per swimmer, weekday and time |
| GET | `/export.csv?from=&to=` | user | CSV, UTF-8 with BOM |
| GET | `/reports` | coach | |
| GET | `/reports/{date}/{txt\|json\|visits\|balances}` | coach | |
| POST | `/reports/run` | coach | `{date?}` |
| GET | `/audit?limit=` | coach | Change log |
| GET | `/changes?since=<rev>&wait=<s>` | user | Long-poll. Returns `{rev}` when the data changes |
