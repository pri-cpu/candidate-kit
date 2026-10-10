#!/usr/bin/env python3
"""
One-by-one API checker for the Attendance API.   (no extra packages needed)

    1. start MongoDB + the API:   uvicorn app.main:app --port 8000
    2. run:                        python check_api.py
       step by step (Enter between checks):   python check_api.py --pause
       other server:                           python check_api.py --base http://host:8000

It creates its own uniquely-named employees (random EMP codes, unique departments, month 2030-03),
so it can be re-run any time without clashing with the sample data. Every check prints PASS or
FAIL (with what was expected vs. what came back); the script never stops at the first failure.
"""
import argparse
import json
import random
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "http://localhost:8000"
PAUSE = False
counts = {"pass": 0, "fail": 0}
step = 0


# ------------------------------------------------------------------ plumbing
def http(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    try:
        return code, json.loads(raw)
    except Exception:
        return code, raw.decode(errors="replace")


def check(name, ok, detail=""):
    global step
    step += 1
    counts["pass" if ok else "fail"] += 1
    print(f"[{step:02d}] {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n        -> {detail}"))
    if PAUSE:
        input("        press Enter for the next check ... ")
    return ok


def expect(name, resp, want, **fields):
    """HTTP status must match, and every given response field must equal the expected value."""
    code, body = resp
    ok, detail = code == want, f"expected HTTP {want}, got {code}: {str(body)[:300]}"
    if ok:
        for k, v in fields.items():
            got = body.get(k) if isinstance(body, dict) else None
            if got != v:
                ok, detail = False, f"field '{k}': expected {v!r}, got {got!r}"
                break
    check(name, ok, detail)
    return body if isinstance(body, dict) else {}


def section(title):
    print(f"\n=== {title} ===")


def ms(y, mo, d, h=0, mi=0, s=0):
    """IST wall-clock -> epoch milliseconds."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


def mk(code, dept, joined="2030-01-01", start="09:30", end="18:30"):
    return http("POST", "/employees", {
        "emp_code": code, "name": f"User {code}", "email": f"{code.lower()}@example.com",
        "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined})


def punch_in(code, y, mo, d, h, mi, status="PRESENT"):
    return http("POST", "/attendance/punch-in", {"emp_code": code, "punched_at": ms(y, mo, d, h, mi), "status": status})


def punch_out(code, y, mo, d, h, mi):
    return http("POST", "/attendance/punch-out", {"emp_code": code, "punched_at": ms(y, mo, d, h, mi)})


def parallel(fn, n):
    out, barrier = [None] * n, threading.Barrier(n)

    def run(i):
        barrier.wait()
        out[i] = fn(i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return out


def half_up(x, places):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------ checks
def main():
    base = random.randint(100000, 899000)
    tag = str(base)
    code = lambda i: f"EMP{base + i}"
    D, NIGHT, SOLO_DEPT, Q, FUT = f"Dept{tag}", f"Night{tag}", f"Solo{tag}", f"Race{tag}", f"Future{tag}"
    P = code(11)  # the solo employee used for the punch-in/out/PATCH checks

    section("1. system")
    expect("GET /health -> 200 ok", http("GET", "/health"), 200, status="ok")

    section("2. POST /employees")
    A, B, C, D0, E, F = (code(i) for i in range(6))
    r = expect("create employee -> 201", mk(A, D), 201, emp_code=A, department=D, shift_start="09:30", shift_end="18:30")
    check("response has created_at as epoch ms and no _id", isinstance(r.get("created_at"), int) and "_id" not in r and "id" not in r, str(r))
    expect("duplicate emp_code -> 409", mk(A, D), 409)
    expect("emp_code 'X1' -> 422", mk("X1", D), 422)
    expect("shift_start == shift_end -> 422", mk(code(90), D, start="09:30", end="09:30"), 422)
    expect("impossible joined_on 2030-02-30 -> 422", mk(code(91), D, joined="2030-02-30"), 422)
    expect("bad email -> 422", http("POST", "/employees", {"emp_code": code(92), "name": "x", "email": "nope", "department": D, "joined_on": "2030-01-01"}), 422)
    expect("bad shift time 25:00 -> 422", mk(code(93), D, start="25:00"), 422)
    for c, joined in ((B, "2030-01-01"), (C, "2030-01-01"), (D0, "2030-01-01"), (E, "2030-03-15"), (F, "2030-04-01")):
        mk(c, D, joined=joined)
    mk(code(10), NIGHT, start="22:00", end="06:00")
    mk(P, SOLO_DEPT)
    mk(code(12), FUT, joined="2031-01-01")

    section("3. GET /employees")
    r = expect(f"filter department={D} -> total 6", http("GET", f"/employees?department={D}&page_size=100"), 200, total=6)
    check("sorted by emp_code ascending", [i["emp_code"] for i in r["items"]] == [A, B, C, D0, E, F], str([i["emp_code"] for i in r["items"]]))
    r = expect("page_size=2 page=1 -> 2 items, total still 6", http("GET", f"/employees?department={D}&page=1&page_size=2"), 200, total=6, page=1, page_size=2)
    check("page 1 holds the FIRST two employees", [i["emp_code"] for i in r["items"]] == [A, B], str([i["emp_code"] for i in r["items"]]))
    r = expect("page 3 -> last two", http("GET", f"/employees?department={D}&page=3&page_size=2"), 200)
    check("page 3 holds the last two employees", [i["emp_code"] for i in r["items"]] == [E, F], str([i["emp_code"] for i in r["items"]]))
    r = expect("page 4 -> empty items", http("GET", f"/employees?department={D}&page=4&page_size=2"), 200, total=6)
    check("page 4 items == []", r["items"] == [], str(r["items"]))
    expect("page=0 -> 422", http("GET", "/employees?page=0"), 422)
    expect("page_size=101 -> 422", http("GET", "/employees?page_size=101"), 422)

    section("4. POST /attendance/punch-in and punch-out (solo employee, Wed 2030-03-20)")
    expect("punch-in unknown employee -> 404", punch_in("EMP000000", 2030, 3, 20, 10, 5), 404)
    ok_ms = ms(2030, 3, 20, 10, 5)
    for label, bad in (("seconds instead of ms", ok_ms // 1000), ("float", float(ok_ms)), ("string", str(ok_ms)), ("bool", True), ("too large", 5_000_000_000_000)):
        expect(f"punched_at as {label} -> 422", http("POST", "/attendance/punch-in", {"emp_code": P, "punched_at": bad}), 422)
    expect("status ABSENT on punch-in -> 422", http("POST", "/attendance/punch-in", {"emp_code": P, "punched_at": ok_ms, "status": "ABSENT"}), 422)
    expect("status garbage -> 422", http("POST", "/attendance/punch-in", {"emp_code": P, "punched_at": ok_ms, "status": "x"}), 422)
    r = expect("punch-in 10:05 IST -> 201, late 35, date 2030-03-20", punch_in(P, 2030, 3, 20, 10, 5), 201,
               date="2030-03-20", late_minutes=35, status="PRESENT", punch_out=None, work_hours=None, overtime_minutes=0, half_day=False, history=[])
    check("punch-in response has no id/_id", "id" not in r and "_id" not in r, str(r))
    expect("second punch-in same day -> 409", punch_in(P, 2030, 3, 20, 10, 5), 409)
    r = expect("punch-in with sub-second ms is truncated to whole seconds",
               http("POST", "/attendance/punch-in", {"emp_code": code(10), "punched_at": ms(2030, 3, 20, 21, 55) + 900}), 201)
    check("stored punch_in has 000 ms", r.get("punch_in", 1) % 1000 == 0, str(r.get("punch_in")))
    expect("punch-out BEFORE any punch-in -> 404", punch_out(P, 2030, 3, 20, 10, 0), 404)
    expect("punch-out == punch-in time -> 422", punch_out(P, 2030, 3, 20, 10, 5), 422)
    expect("punch-out > 24h after punch-in -> 422", http("POST", "/attendance/punch-out", {"emp_code": P, "punched_at": ok_ms + 25 * 3600 * 1000}), 422)
    expect("punch-out unknown employee -> 404", punch_out("EMP000000", 2030, 3, 20, 19, 10), 404)
    expect("punch-out 19:10 -> 200, work_hours 9.08, overtime 40, not half day", punch_out(P, 2030, 3, 20, 19, 10), 200,
           work_hours=9.08, overtime_minutes=40, half_day=False, late_minutes=35)
    expect("second punch-out -> 409", punch_out(P, 2030, 3, 20, 19, 20), 409)
    expect("overnight shift: out 06:40 next day -> 200, overtime 40, hours 8.75, date = start day",
           punch_out(code(10), 2030, 3, 21, 6, 40), 200, date="2030-03-20", overtime_minutes=40, work_hours=8.75, late_minutes=0)
    expect("overnight: punch-in 03:00 IST belongs to the previous day's shift", punch_in(code(10), 2030, 3, 22, 3, 0), 201, date="2030-03-21")

    section("5. GET /attendance")
    r = expect("filter by emp_code -> 1 record", http("GET", f"/attendance?emp_code={P}"), 200, total=1)
    first = (r.get("items") or [{}])[0]
    check("record shape is correct", first.get("date") == "2030-03-20" and first.get("history") == [], str(first))
    expect("date range containing it -> total 1", http("GET", f"/attendance?emp_code={P}&date_from=2030-03-20&date_to=2030-03-20"), 200, total=1)
    expect("date range excluding it -> total 0", http("GET", f"/attendance?emp_code={P}&date_from=2030-03-21&date_to=2030-03-22"), 200, total=0)
    expect("status filter WFH -> total 0", http("GET", f"/attendance?emp_code={P}&status=WFH"), 200, total=0)
    expect("date_from > date_to -> 422", http("GET", "/attendance?date_from=2030-03-22&date_to=2030-03-20"), 422)
    expect("invalid date -> 422", http("GET", "/attendance?date_from=garbage"), 422)
    expect("invalid status -> 422", http("GET", "/attendance?status=BOGUS"), 422)
    expect("page=0 -> 422", http("GET", "/attendance?page=0"), 422)
    expect("page_size=101 -> 422", http("GET", "/attendance?page_size=101"), 422)
    r = expect("huge page number does not crash (-> empty list)", http("GET", "/attendance?page=999999999999"), 200)

    section("6. PATCH /attendance/{emp_code}/{date}")
    path = f"/attendance/{P}/2030-03-20"
    who = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    expect("bad date in path -> 422", http("PATCH", f"/attendance/{P}/2030-13-40", who), 422)
    expect("unknown employee -> 404", http("PATCH", "/attendance/EMP000000/2030-03-20", who), 404)
    expect("no record on that date -> 404", http("PATCH", f"/attendance/{P}/2030-03-19", {**who, "status": "LEAVE"}), 404)
    expect("request that changes nothing -> 422", http("PATCH", path, who), 422)
    expect("reason shorter than 5 chars -> 422", http("PATCH", path, {**who, "reason": "abc", "status": "WFH"}), 422)
    expect("LEAVE together with punch_in -> 422", http("PATCH", path, {**who, "status": "LEAVE", "punch_in": ms(2030, 3, 20, 9, 0)}), 422)
    expect("punch_in moved to another date -> 422", http("PATCH", path, {**who, "punch_in": ms(2030, 3, 21, 9, 0)}), 422)
    expect("punch_out before punch_in -> 422", http("PATCH", path, {**who, "punch_out": ms(2030, 3, 20, 9, 0)}), 422)
    r = expect("fix punch_in to 09:28 (client-sent derived fields ignored) -> 200", http("PATCH", path, {
        **who, "punch_in": ms(2030, 3, 20, 9, 28), "late_minutes": 999, "work_hours": 1, "half_day": True}), 200,
        late_minutes=0, work_hours=9.7, overtime_minutes=40, half_day=False)
    h = r.get("history", [])
    check("history has exactly 1 entry with by/reason", len(h) == 1 and h[0]["by"] == "hr.admin" and h[0]["reason"] == "biometric glitch", str(h))
    ch = h[0]["changes"] if h else {}
    check("history.changes holds ONLY the fields that changed", set(ch) == {"punch_in", "late_minutes", "work_hours"}, str(sorted(ch)))
    check("history punch_in from/to are epoch ms", ch.get("punch_in", {}).get("from") == ms(2030, 3, 20, 10, 5) and ch["punch_in"]["to"] == ms(2030, 3, 20, 9, 28), str(ch.get("punch_in")))
    check("history late_minutes 35 -> 0", ch.get("late_minutes") == {"from": 35, "to": 0}, str(ch.get("late_minutes")))
    r = expect("status ABSENT clears punches -> 200", http("PATCH", path, {**who, "status": "ABSENT"}), 200,
               status="ABSENT", punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    check("history now has 2 entries (appended, not overwritten)", len(r.get("history", [])) == 2, str(r.get("history")))
    expect("presence status without any punch_in -> 422", http("PATCH", path, {**who, "status": "WFH"}), 422)
    r = expect("back to WFH with 09:30-12:30 -> half day, 3.0 h", http("PATCH", path, {
        **who, "status": "WFH", "punch_in": ms(2030, 3, 20, 9, 30), "punch_out": ms(2030, 3, 20, 12, 30)}), 200, half_day=True, work_hours=3.0, late_minutes=0)
    check("history has 3 entries", len(r.get("history", [])) == 3, str(r.get("history")))

    section("7. loading analytics data for March 2030 (through the API)")
    loads = {}
    # A: normal, late+overtime, half day, Saturday, a LEAVE (via PATCH), an ON_DUTY day
    loads["A"] = [punch_in(A, 2030, 3, 4, 9, 30), punch_out(A, 2030, 3, 4, 18, 30),
                  punch_in(A, 2030, 3, 5, 10, 0), punch_out(A, 2030, 3, 5, 19, 30),
                  punch_in(A, 2030, 3, 6, 9, 30), punch_out(A, 2030, 3, 6, 13, 0),
                  punch_in(A, 2030, 3, 9, 10, 0), punch_out(A, 2030, 3, 9, 14, 0),
                  punch_in(A, 2030, 3, 12, 9, 30, "ON_DUTY"), punch_out(A, 2030, 3, 12, 18, 30),
                  punch_in(A, 2030, 3, 11, 9, 30)]
    loads["A"].append(http("PATCH", f"/attendance/{A}/2030-03-11", {**who, "status": "LEAVE"}))
    for c in (B, C):  # identical -> tie on 80 late minutes
        loads[c] = [punch_in(c, 2030, 3, 4, 10, 0), punch_out(c, 2030, 3, 4, 18, 30), punch_in(c, 2030, 3, 5, 10, 20), punch_out(c, 2030, 3, 5, 18, 30)]
    loads["E"] = [punch_in(E, 2030, 3, 18, 11, 10), punch_out(E, 2030, 3, 18, 18, 30)]
    for who_, rs in loads.items():
        bad = [(c, str(b)[:120]) for c, b in rs if c not in (200, 201)]
        check(f"loaded records for {who_} without errors", not bad, str(bad))

    section("8. GET /analytics/employees/{emp_code}/monthly   (month=2030-03)")
    expect("employee A", http("GET", f"/analytics/employees/{A}/monthly?month=2030-03"), 200,
           working_days=21, present_days=3.5, leave_days=1, late_count=2, total_late_minutes=60, total_overtime_minutes=60, attendance_pct=16.67)
    expect("mid-month joiner E: working days counted from joined_on (11)", http("GET", f"/analytics/employees/{E}/monthly?month=2030-03"), 200,
           working_days=11, present_days=1, late_count=1, total_late_minutes=100, attendance_pct=9.09)
    expect("employee with no logs D0: zeros, pct 0.0", http("GET", f"/analytics/employees/{D0}/monthly?month=2030-03"), 200,
           working_days=21, present_days=0, leave_days=0, late_count=0, total_late_minutes=0, total_overtime_minutes=0, attendance_pct=0.0)
    expect("employee joined after the month: working_days 0, pct null", http("GET", f"/analytics/employees/{F}/monthly?month=2030-03"), 200,
           working_days=0, present_days=0, attendance_pct=None)
    expect("unknown employee -> 404", http("GET", "/analytics/employees/EMP000000/monthly?month=2030-03"), 404)
    expect("month=2030-13 -> 422", http("GET", f"/analytics/employees/{A}/monthly?month=2030-13"), 422)
    expect("missing month -> 422", http("GET", f"/analytics/employees/{A}/monthly"), 422)

    section("9. GET /analytics/departments/summary")
    r = expect(f"department={D} -> one item", http("GET", f"/analytics/departments/summary?month=2030-03&department={D}"), 200, month="2030-03")
    items = r.get("items", [])
    want = {"department": D, "headcount": 5, "present_days": 8.5, "avg_work_hours": 7.57, "late_count": 7,
            "total_late_minutes": 320, "leave_count": 1, "on_duty_count": 1}
    check("headcount 5 (incl. D0 with zero logs, excl. F who joins in April); numbers match", len(items) == 1 and items[0] == want, f"expected {want}\n        got      {items}")
    r = expect("no department filter -> 200", http("GET", "/analytics/departments/summary?month=2030-03"), 200)
    names = [i["department"] for i in r.get("items", [])]
    check("items sorted by department ascending", names == sorted(names), str(names))
    check("department that has not started yet is omitted", FUT not in names, str(names))
    check(f"{D} appears with the same numbers", want in r.get("items", []), "")
    expect("month=2030-13 -> 422", http("GET", "/analytics/departments/summary?month=2030-13"), 422)

    section("10. GET /analytics/leaderboard/late   (ties 1,2,2,4)")
    row = lambda c, rank, tot, n_: {"rank": rank, "emp_code": c, "name": f"User {c}", "department": D, "total_late_minutes": tot, "late_count": n_}
    full = [row(E, 1, 100, 1), row(B, 2, 80, 2), row(C, 2, 80, 2), row(A, 4, 60, 2)]
    for lim, rows in ((10, full), (4, full), (3, full[:3]), (2, full[:3]), (1, full[:1])):
        r = expect(f"department={D} limit={lim}", http("GET", f"/analytics/leaderboard/late?month=2030-03&department={D}&limit={lim}"), 200)
        check(f"  -> {len(rows)} rows" + (" (tie at the cutoff is kept)" if lim == 2 else ""), r.get("items") == rows, f"expected {rows}\n        got      {r.get('items')}")
    check("employee with 0 late minutes (D0) is not listed", all(i["emp_code"] != D0 for i in r.get("items", [])), "")
    expect("limit=0 -> 422", http("GET", "/analytics/leaderboard/late?month=2030-03&limit=0"), 422)
    expect("limit=51 -> 422", http("GET", "/analytics/leaderboard/late?month=2030-03&limit=51"), 422)
    expect("no department filter -> 200", http("GET", "/analytics/leaderboard/late?month=2030-03"), 200)

    section("11. GET /analytics/departments/{department}/trend")
    daily = {  # date -> (present_count, late_count) that the loaded data should produce
        date(2030, 3, 4): (3, 2), date(2030, 3, 5): (3, 3), date(2030, 3, 6): (0.5, 0), date(2030, 3, 9): (0.5, 1),
        date(2030, 3, 11): (0, 0), date(2030, 3, 12): (1, 0)}
    joins = [date(2030, 1, 1)] * 4 + [date(2030, 3, 15), date(2030, 4, 1)]
    start, end = date(2030, 3, 1), date(2030, 3, 16)

    def model(start, end, daily, joins):
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        rows, rates = [], []
        for d in days:
            hc = sum(1 for j in joins if j <= d)
            pres, late = daily.get(d, (0, 0))
            wd = d.weekday() < 5
            rate = half_up(Decimal(str(pres)) / Decimal(hc), 4) if wd and hc > 0 else None
            rates.append(rate)
            win = [x for x in rates[-7:] if x is not None]
            mov = half_up(sum(Decimal(str(x)) for x in win) / Decimal(len(win)), 4) if win else None
            rows.append({"date": d.isoformat(), "is_working_day": wd, "headcount": hc, "present_count": float(pres),
                         "late_count": late, "attendance_rate": rate, "moving_avg_7d": mov})
        return rows

    def compare(name, actual, expected):
        if len(actual) != len(expected):
            return check(name, False, f"expected {len(expected)} rows, got {len(actual)}")
        bad = [(e["date"], k, e[k], a.get(k)) for a, e in zip(actual, expected) for k in e
               if (abs(e[k] - a[k]) > 1e-9 if isinstance(e[k], float) and isinstance(a.get(k), (int, float)) else e[k] != a.get(k))]
        return check(name, not bad, "first mismatches (date, field, expected, got): " + str(bad[:4]))

    r = expect(f"{D} 2030-03-01..16 -> 200", http("GET", f"/analytics/departments/{D}/trend?from=2030-03-01&to=2030-03-16"), 200, department=D)
    compare("all 16 rows correct vs. independent model (gap days, weekends null rate, headcount, 7-day window, half-up)", r.get("items", []), model(start, end, daily, joins))
    byd = {i["date"]: i for i in r.get("items", [])}
    check("2030-03-06 moving_avg is 0.4063 (0.40625 rounded HALF-UP, not 0.4062)", byd.get("2030-03-06", {}).get("moving_avg_7d") == 0.4063, str(byd.get("2030-03-06")))
    check("weekend with a record (03-09): present_count 0.5, late_count 1, attendance_rate null", byd.get("2030-03-09", {}).get("present_count") == 0.5 and byd["2030-03-09"]["late_count"] == 1 and byd["2030-03-09"]["attendance_rate"] is None, str(byd.get("2030-03-09")))
    check("headcount becomes 5 on 03-15 (E joins)", byd.get("2030-03-14", {}).get("headcount") == 4 and byd.get("2030-03-15", {}).get("headcount") == 5, str((byd.get("2030-03-14"), byd.get("2030-03-15"))))
    r = expect("range with NO logs at all still returns every day", http("GET", f"/analytics/departments/{D}/trend?from=2030-02-01&to=2030-02-07"), 200)
    compare("7 rows: weekdays rate 0.0, weekend null, correct moving avg", r.get("items", []), model(date(2030, 2, 1), date(2030, 2, 7), {}, joins))
    expect("unknown department -> 404", http("GET", "/analytics/departments/NoSuchDept/trend?from=2030-03-01&to=2030-03-05"), 404)
    expect("to before from -> 422", http("GET", f"/analytics/departments/{D}/trend?from=2030-03-05&to=2030-03-01"), 422)
    expect("range of 93 days -> 422", http("GET", f"/analytics/departments/{D}/trend?from=2030-01-01&to=2030-04-03"), 422)
    expect("range of exactly 92 days -> 200", http("GET", f"/analytics/departments/{D}/trend?from=2030-01-01&to=2030-04-02"), 200)
    expect("missing 'from' -> 422", http("GET", f"/analytics/departments/{D}/trend?to=2030-03-05"), 422)

    section("12. GET /admin/explain/{endpoint}   (must use IXSCAN, never COLLSCAN)")
    cases = {"attendance_list": "", "employee_monthly": f"?emp_code={A}&month=2030-03", "department_summary": "?month=2030-03",
             "late_leaderboard": "?month=2030-03", "department_trend": f"?department={D}&from=2030-03-01&to=2030-03-16"}
    cases["attendance_list"] = f"?date_from=2030-03-01&date_to=2030-03-31&page=1&page_size=20"
    for name, qs in cases.items():
        code_, body = http("GET", f"/admin/explain/{name}{qs}")
        txt = json.dumps(body)
        ok = code_ == 200 and isinstance(body, dict) and body.get("endpoint") == name and "explain" in body and "collection" in body
        check(f"{name}: 200 with endpoint/collection/explain", ok, f"HTTP {code_}: {txt[:300]}")
        check(f"{name}: plan contains IXSCAN", "IXSCAN" in txt, "no IXSCAN in plan")
        check(f"{name}: plan has NO COLLSCAN", "COLLSCAN" not in txt, "COLLSCAN found -> an index is missing/unused")
    check("attendance_list with status + emp_code filters also uses an index", "COLLSCAN" not in json.dumps(http("GET", f"/admin/explain/attendance_list?emp_code={A}&status=PRESENT")[1]), "")
    expect("employee_monthly without month -> 422", http("GET", f"/admin/explain/employee_monthly?emp_code={A}"), 422)
    expect("unknown endpoint name -> 422", http("GET", "/admin/explain/nope"), 422)

    section("13. concurrency")
    res = parallel(lambda i: mk(code(50), Q)[0], 8)
    check("8 parallel creates of the same emp_code -> exactly one 201, rest 409", sorted(res) == [201] + [409] * 7, str(sorted(res)))
    res = parallel(lambda i: punch_in(code(50), 2030, 3, 25, 9, 30)[0], 8)
    check("8 parallel punch-ins, same employee+date -> exactly one 201, rest 409", sorted(res) == [201] + [409] * 7, str(sorted(res)))
    res = parallel(lambda i: punch_out(code(50), 2030, 3, 25, 18, 30)[0], 8)
    check("8 parallel punch-outs -> exactly one 200, rest 409", sorted(res) == [200] + [409] * 7, str(sorted(res)))
    res = parallel(lambda i: http("PATCH", f"/attendance/{code(50)}/2030-03-25", {**who, "punch_out": ms(2030, 3, 25, 18, 31 + i)})[0], 6)
    wins = res.count(200)
    check("6 parallel PATCHes -> only 200/409 answers, at least one 200", set(res) <= {200, 409} and wins >= 1, str(res))
    r = http("GET", f"/attendance?emp_code={code(50)}")[1]
    hist = r["items"][0]["history"] if isinstance(r, dict) and r.get("items") else []
    check("history entries == number of successful PATCHes (no lost update)", len(hist) == wins, f"{wins} PATCHes returned 200 but history has {len(hist)} entries")

    print(f"\n{'=' * 60}\nRESULT: {counts['pass']} passed, {counts['fail']} failed (out of {step})")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--pause", action="store_true", help="wait for Enter after every check")
    a = ap.parse_args()
    BASE, PAUSE = a.base.rstrip("/"), a.pause
    try:
        http("GET", "/health")
    except Exception as e:
        sys.exit(f"Cannot reach {BASE}: {e}\nStart the API first: uvicorn app.main:app --port 8000")
    sys.exit(main())