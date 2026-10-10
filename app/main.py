"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Layout of this file
    1. configuration, constants, indexes
    2. time / rule helpers (R1 - R5, R8)
    3. request models and response serialisers
    4. system + employee endpoints
    5. attendance endpoints (punch-in, punch-out, list, regularize)
    6. analytics pipelines + endpoints
    7. admin explain endpoint
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, constants, indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back UTC-aware datetimes, so we never mix naive
# and aware values (the starter's naive datetimes silently used the server's zone).
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MS_MIN = 100_000_000_000  # anything below this "looks like seconds"
MS_MAX = 4_102_444_800_000  # 2100-01-01
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
StatusT = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatusT = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HOURS = 4.5  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92
MAX_PATCH_RETRIES = 5

MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

EpochMs = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]  # strict: no floats/strings/bools


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the index already exists."""
    specs = [
        # employees
        (db.employees, [("emp_code", ASCENDING)], {"unique": True, "name": "uq_emp_code"}),
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "ix_dept_emp"}),
        (db.employees, [("joined_on", ASCENDING)], {"name": "ix_joined_on"}),
        # attendance_logs
        # one record per (employee, day): the guarantee behind race-safe punch-in; also serves
        # emp_code (+ date range) filters, monthly analytics and the trend's $in query.
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uq_emp_date"}),
        # list sort order (date desc, emp_code asc), date ranges, leaderboard month scan.
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_date_emp"}),
        # status filter + the same sort order.
        (db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_status_date_emp"}),
        # punch-out: "latest record whose punch_in <= punched_at" for one employee.
        (db.attendance_logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "ix_emp_punch_in"}),
    ]
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except PyMongoError:
            log.exception("could not create index %s", opts.get("name"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Helpers: time conversion and business rules
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def parse_date(value: str, label: str = "date") -> date_cls:
    """Strict YYYY-MM-DD; anything else (or an impossible date) is a 422."""
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{label} must be YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{label} is not a valid calendar date")


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded "HH:MM" strings compare correctly


def ist_at(day: date_cls, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; for overnight shifts, a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = ts.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (eh, em, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def half_up(value: Any, places: int) -> float:
    """Round half-up on the decimal value (not binary float rounding, not banker's)."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since
    shift_start (floor), not since the end of the grace period."""
    start = ist_at(date_cls.fromisoformat(date_str), shift_start)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (Decimal arithmetic, so no float surprises)."""
    delta = punch_out - punch_in
    seconds = delta.days * 86400 + delta.seconds
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes (floor) past shift_end, only when >= 30. The end of an overnight shift
    falls on the day after the attendance date."""
    day = date_cls.fromisoformat(date_str)
    end = ist_at(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_derived(emp: dict, date_str: str, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """All derived fields for a presence record (R2 - R5)."""
    out = {
        "late_minutes": compute_late_minutes(punch_in, emp["shift_start"], date_str),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str)
        out["half_day"] = out["work_hours"] < HALF_DAY_HOURS
    return out


# --------------------------------------------------------------------------- #
# 3. Models and serialisers
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatusT = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields are deliberately not declared: if a client sends them they are ignored.
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def _history_value(value: Any) -> Any:
    return to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _history_value(c.get("from")), "to": _history_value(c.get("to"))}
            for field, c in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    """Stored attendance document -> API shape (no _id, ms instants, defaults for legacy gaps)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. System and employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # BSON = ms precision
    try:
        db.employees.insert_one(doc)  # the unique index is the real guard against duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items: list = []
    if skip < total:  # also keeps absurd page numbers away from the driver
        cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 5. Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Latest record whose punch_in is at or before the punch-out (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_dt = as_utc(rec["punch_in"])
    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after the punch-in")
    if ts - punch_in_dt > MAX_SHIFT:
        raise HTTPException(422, "punch-out is more than 24 hours after the punch-in")

    derived = compute_derived(emp, rec["date"], punch_in_dt, ts)
    # The `punch_out: None` guard makes the close atomic: of two simultaneous requests only one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_attendance_find(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
    status: Optional[str], page: int, page_size: int,
) -> dict:
    """Single source of truth for the list query; the explain endpoint reuses it."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        lo = parse_date(date_from, "date_from") if date_from is not None else None
        hi = parse_date(date_to, "date_to") if date_to is not None else None
        if lo and hi and lo > hi:
            raise HTTPException(422, "date_from must not be after date_to")
        rng: dict = {}
        if date_from is not None:
            rng["$gte"] = date_from
        if date_to is not None:
            rng["$lte"] = date_to
        q["date"] = rng
    if status:
        q["status"] = status
    return {"filter": q, "sort": ATTENDANCE_SORT, "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    items: list = []
    if spec["skip"] < total:
        cursor = (
            db.attendance_logs.find(spec["filter"])
            .sort(spec["sort"]).skip(spec["skip"]).limit(spec["limit"])
        )
        items = [record_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def corrected_state(rec: dict, emp: dict, body: RegularizeIn) -> dict:
    """The record's new field values after applying a correction (422 on any rule violation)."""
    status = body.status or rec["status"]

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE clear the punch times; do not send punch_in/punch_out")
        return {
            "status": status, "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
        }

    p_in = ms_to_dt(body.punch_in) if body.punch_in is not None else as_utc(rec.get("punch_in"))
    p_out = ms_to_dt(body.punch_out) if body.punch_out is not None else as_utc(rec.get("punch_out"))
    if p_in is None:
        raise HTTPException(422, "a presence status requires a punch_in")
    if attendance_date(p_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
        raise HTTPException(422, "punch_in must stay on the record's attendance date")
    if p_out is not None:
        if p_out <= p_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if p_out - p_in > MAX_SHIFT:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    return {"status": status, "punch_in": p_in, "punch_out": p_out, **compute_derived(emp, rec["date"], p_in, p_out)}


CORRECTABLE = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee_or_404(emp_code)

    # Optimistic concurrency: the write only lands if the record is still exactly what we read
    # (including its history length). The loser re-reads and re-applies, so no history entry is lost.
    for _ in range(MAX_PATCH_RETRIES):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")

        new = corrected_state(rec, emp, body)
        old = {
            "status": rec["status"],
            "punch_in": as_utc(rec.get("punch_in")),
            "punch_out": as_utc(rec.get("punch_out")),
            "work_hours": rec.get("work_hours"),
            "late_minutes": rec.get("late_minutes") or 0,
            "overtime_minutes": rec.get("overtime_minutes") or 0,
            "half_day": bool(rec.get("half_day", False)),
        }
        changes = {f: {"from": old[f], "to": new[f]} for f in CORRECTABLE if old[f] != new[f]}
        if not changes:
            raise HTTPException(422, "the request changes nothing")

        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        n_history = len(rec.get("history") or [])
        guard = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            f"history.{n_history}": {"$exists": False},  # nobody appended since we read
        }
        updated = db.attendance_logs.find_one_and_update(
            guard,
            {"$set": new, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# 6. Analytics
# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> tuple[str, str]:
    """('YYYY-MM-01', 'YYYY-MM-<last day>') - ISO date strings compare chronologically."""
    if not re.match(MONTH_RE, month) or int(month[:4]) < 1:
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def count_working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on for mid-month joiners (0 if joined later)."""
    first, last = month_bounds(month)
    day = max(date_cls.fromisoformat(first), date_cls.fromisoformat(joined_on))
    end = date_cls.fromisoformat(last)
    n = 0
    while day <= end:
        if day.weekday() < 5:
            n += 1
        day += timedelta(days=1)
    return n


# ---- reusable aggregation expressions; `p` is the field prefix ("$" or "$lg.") --------------------
def _is_present(p: str) -> dict:
    return {"$in": [f"{p}status", PRESENCE]}


def _is_weekday(p: str) -> dict:
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": f"{p}date", "format": "%Y-%m-%d"}}}, 5]}


def _present_weight(p: str) -> dict:
    """1 per present weekday record, 0.5 for a half day, 0 otherwise (R5-R7)."""
    half = {"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}
    return {"$cond": [{"$and": [_is_present(p), _is_weekday(p)]}, {"$cond": [half, 0.5, 1]}, 0]}


def _late_minutes(p: str) -> dict:
    return {"$ifNull": [f"{p}late_minutes", 0]}


def _is_late(p: str) -> dict:
    return {"$cond": [{"$gt": [_late_minutes(p), 0]}, 1, 0]}


def _count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    p = "$"
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},  # uq_emp_date
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_weight(p)},
            "leave_days": _count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": {"$sum": _is_late(p)},
            "total_late_minutes": {"$sum": _late_minutes(p)},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from `employees` so people with zero logs are still counted (R9), then left-joins
    their month of logs; headcount = distinct employees per department."""
    first, last = month_bounds(month)
    p = "$lg."
    emp_filter: dict = {"joined_on": {"$lte": last}}
    if department:
        emp_filter["department"] = department
    has_hours = {"$and": [_is_present(p), {"$isNumber": f"{p}work_hours"}]}
    return [
        {"$match": emp_filter},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "status": 1, "date": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "lg",
        }},
        {"$unwind": {"path": "$lg", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": _present_weight(p)},
            "hours_sum": {"$sum": {"$cond": [has_hours, f"{p}work_hours", 0]}},
            "hours_n": _count_if(has_hours),
            "late_count": {"$sum": _is_late(p)},
            "total_late_minutes": {"$sum": _late_minutes(p)},
            "leave_count": _count_if({"$eq": [f"{p}status", "LEAVE"]}),
            "on_duty_count": _count_if({"$eq": [f"{p}status", "ON_DUTY"]}),
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$emps"},
            "present_days": 1, "hours_sum": 1, "hours_n": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},  # ix_date_emp
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": _late_minutes("$")}, "late_count": {"$sum": _is_late("$")}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # BEFORE ranking: rank within the department
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4); array position is never used as the rank.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
            "department": "$emp.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


def validate_trend_range(date_from: str, date_to: str) -> tuple[date_cls, date_cls]:
    start, end = parse_date(date_from, "from"), parse_date(date_to, "to")
    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")
    return start, end


def load_department_roster(department: str) -> tuple[list, list]:
    """(emp_codes, joined_on strings) for a department; 404 if it has no employees."""
    rows = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not rows:
        raise HTTPException(404, "unknown department")
    return [r["emp_code"] for r in rows], sorted(r["joined_on"] for r in rows)


def trend_pipeline(department: str, start: date_cls, end: date_cls, codes: list, joined: list) -> list:
    from_s, to_s = start.isoformat(), end.isoformat()
    lower = datetime(start.year, start.month, start.day, tzinfo=UTC)
    upper = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_s, "$lte": to_s}}},
        # Sentinel row (never present, never late) so the stream is non-empty even if the
        # department has no logs at all in the range; $densify then still fills every day.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "date": {"$literal": from_s}, "status": {"$literal": "SENTINEL"}}},
        ]}},
        {"$group": {"_id": "$date", "present_count": {"$sum": _present_weight_any_day()}, "late_count": {"$sum": _is_late("$")}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"day": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$addFields": {"headcount": {"$size": {"$filter": {
            "input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]},
        }}}}},
        # attendance_rate scaled by 10^4 and rounded HALF-UP with integer-exact arithmetic:
        # floor((p * 10000 * 2 + h) / (2h)) with p doubled -> no float/banker's rounding surprises.
        {"$addFields": {"rate_k": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": [{"$multiply": ["$present_count", 2]}, 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]},
            ]}},
            None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "win_sum_k": {"$sum": "$rate_k", "window": {"documents": [-6, 0]}},
            "win_cnt": {"$sum": {"$cond": [{"$eq": ["$rate_k", None]}, 0, 1]}, "window": {"documents": [-6, 0]}},
        }}},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "rate_k": 1, "win_sum_k": 1, "win_cnt": 1,
        }},
    ]


def _present_weight_any_day() -> dict:
    """Trend present_count: presence records on ANY day (weekends included), half day = 0.5."""
    half = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    return {"$cond": [_is_present("$"), {"$cond": [half, 0.5, 1]}, 0]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    month_bounds(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    working_days = count_working_days(month, emp["joined_on"])
    present = float(agg.get("present_days", 0))
    pct = None
    if working_days > 0:
        pct = half_up(Decimal(str(present)) / Decimal(working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    month_bounds(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(month, department)):
        avg = None
        if r["hours_n"]:
            # mean over RECORDS: exact decimal division of (sum of hours) / (record count)
            cents = round(r["hours_sum"] * 100)
            avg = half_up(Decimal(cents) / Decimal(int(r["hours_n"]) * 100), 2)
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_bounds(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department))
    items = [
        {
            "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
            "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    start, end = validate_trend_range(date_from, date_to)
    codes, joined = load_department_roster(department)
    items = []
    for r in db.attendance_logs.aggregate(trend_pipeline(department, start, end, codes, joined)):
        rate = None if r["rate_k"] is None else round(int(r["rate_k"]) / 10000, 4)
        moving = None
        if r["win_cnt"]:
            avg = Decimal(int(r["win_sum_k"])) / Decimal(int(r["win_cnt"])) / Decimal(10000)
            moving = float(avg.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Admin: explain
# --------------------------------------------------------------------------- #
def _require(**params: Any) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to_: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """executionStats explain of the SAME query/pipeline the endpoint builds (shared builders)."""
    if endpoint == "attendance_list":
        spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        # the find COMMAND wants sort as a document; the cursor API accepts the list-of-pairs form
        command: dict = {"find": collection, **{**spec, "sort": dict(spec["sort"])}}
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            collection, pipeline = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            _require(department=department, **{"from": from_, "to": to_})
            start, end = validate_trend_range(from_, to_)
            codes, joined = load_department_roster(department)
            collection, pipeline = "attendance_logs", trend_pipeline(department, start, end, codes, joined)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(raw)),
    }