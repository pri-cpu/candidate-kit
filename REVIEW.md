# REVIEW.md
#

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.

# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Compares in whatever zone the datetime carries and replaces hour/minute on the punch-in's own date, so IST shift times are never applied. Overnight shifts use the wrong day. | Punch-in at 10:05 IST on a UTC server gives a wrong late value. | Shift start is built as an IST datetime on the record's attendance date. |
| 2 | `compute_late_minutes` | `int(seconds/60) > 10` instead of "strictly more than 10:00". 09:40:30 floors to 10 minutes, so it counts as not late. | 09:40:01 must give 10; the starter gives 0. | Compare in seconds: `seconds > 600`, then floor to minutes since shift start. |
| 3 | `compute_work_hours` | Python `round()` on a float: banker's rounding plus float error, not half-up. Instants are not truncated to whole seconds. | A 0.005 h gap returns 0.0 instead of 0.01. | Decimal division, `ROUND_HALF_UP`; instants truncated at the API boundary. |
| 4 | `compute_overtime` | No 30-minute minimum. Overnight shift end is not moved to the next day. Not floored explicitly. | Punch-out 18:35 on a 18:30 shift returns 5 instead of 0; overnight 06:40 gives a negative or zero value. | Floor minutes, count only if >= 30, add one day to the end of an overnight shift. |
| 5 | Missing helper | `half_day` (R5) is hard-coded to False and never computed. | Punch out after 3.5 h: `half_day` should be true. | `compute_derived` sets `half_day = work_hours < 4.5` using the rounded value. |
| 6 | `create_employee` | Check-then-insert, no unique index: two parallel creates both succeed. | Fire two identical POSTs at once and get two 201s. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 7 | `create_employee` / `EmployeeIn` | No validation (emp_code format, email, lengths, `shift_start == shift_end`, impossible `joined_on`). `created_at` is naive local time and returned as a datetime, not epoch ms. | `joined_on: "2026-02-30"` is accepted. | Pydantic patterns and validators (422); `created_at` is UTC, returned in ms. |
| 8 | `list_employees` | `skip = page * page_size` (page 1 skips the first page). `total` counts the whole collection, not the filter. No sort. No bounds on page or page_size. | Page 1 returns the second page; `total` ignores `department`. | `(page-1)*page_size`, `count_documents(filter)`, sort by `emp_code`, `Query(ge/le)` bounds. |
| 9 | `punch_in` | Unknown employee gives `emp is None`, then a TypeError and a 500. | POST with an unknown `emp_code`. | Return 404. |
| 10 | `punch_in` / `PunchInIn` | `status` accepts any string (ABSENT, garbage). `punched_at` is unvalidated: seconds, floats, strings and 0 (falsy, treated as "now") get through. | Send `punched_at: 1783312500` (seconds). | `Literal` status; strict int in the ms range, otherwise 422; `is not None` check. |
| 11 | `punch_in` | `datetime.fromtimestamp` uses the server's local zone. The date is the server's date, not the IST date (R1). Overnight shifts are ignored. Microseconds are stored. | Server in UTC: 23:00 UTC punch gets the wrong date. | `attendance_date()` converts to IST and applies the overnight rule; instants truncated to seconds, stored as UTC. |
| 12 | `punch_in` | Check-then-insert race: parallel identical requests can both return 201. | Two simultaneous requests. | Unique index on (`emp_code`, `date`); loser gets 409. |
| 13 | `punch_in` response | Returns an `id` (the ObjectId string) and raw datetimes. `_id` must never be exposed and instants must be ms. | Response contains `id`. | `record_out()` returns only contract fields, instants in ms, defaults for missing fields. |
| 14 | `list_attendance` | Loads the whole collection into memory and sorts in Python. | Slow at 100k records. | Query-side sort, skip and limit with supporting indexes; `count_documents` for `total`. |
| 15 | `list_attendance` | Secondary sort missing (`emp_code` asc). Dates and status are not validated. `date_from > date_to` is not rejected. `page=0` gives negative slicing. | `?date_from=garbage`, `?page=0`, `?status=BOGUS`. | Validation returns 422; sort `(date desc, emp_code asc)`. |
| 16 | `health` | Always returns ok, even when MongoDB is down. | Stop MongoDB and call `/health`. | Pings MongoDB; 503 on failure. |
| 17 | App startup | No indexes at all. | `explain` shows COLLSCAN. | `ensure_indexes()` in the lifespan hook, idempotent. |
| 18 | MongoClient | Default naive datetimes come back, mixing naive and aware values. | Comparison and arithmetic errors, or silent wrong zones. | `tz_aware=True`; everything normalised to UTC. |

## Looked at and decided it was fine
- `load_dotenv()` does not override existing environment variables by default, so real env vars already win.
- Plain `def` endpoints with PyMongo run in FastAPI's threadpool; the client is thread-safe, so no change needed.
- Popping `_id` after `insert_one` works, but it is replaced by explicit response builders.