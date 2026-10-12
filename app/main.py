"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

File layout (everything lives in this one file, as the assignment requires):
  1. Configuration & database handle
  2. Time / rule helpers   - IST handling and rules R1-R5 (pure functions, no I/O)
  3. Validation types & request models
  4. Response shaping      - BSON datetimes <-> epoch milliseconds
  5. Index creation        - run at startup, idempotent
  6. Employees, attendance, punch-in / punch-out, regularize (PATCH)
  7. Analytics             - aggregation pipelines (builders are shared with the explain endpoint)
  8. Admin explain endpoint
"""
import calendar
import datetime as dt
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration & database handle
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False (default): real environment variables take priority over .env
log = logging.getLogger("attendance")


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required (see README.md)")
    return value


# tz_aware=True: PyMongo hands back timezone-aware UTC datetimes instead of naive ones, so we never
# have to guess what a datetime means.
client = MongoClient(_required_env("MONGO_URI"), tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[_required_env("MONGO_DB")]

UTC = dt.timezone.utc
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600  # punch-out / regularize window
MAX_TREND_DAYS = 92

_indexes_ready = False


# --------------------------------------------------------------------------- #
# 2. Time and business-rule helpers (pure functions)
# --------------------------------------------------------------------------- #
def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def ms_to_dt(ms: int) -> dt.datetime:
    """Epoch milliseconds -> aware UTC datetime (exact integer arithmetic, no float)."""
    return EPOCH + dt.timedelta(milliseconds=ms)


def dt_to_ms(value: dt.datetime) -> int:
    """Datetime -> epoch milliseconds. A naive datetime is interpreted as UTC (how BSON stores it)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // dt.timedelta(milliseconds=1)


def trunc_sec(value: dt.datetime) -> dt.datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return value.replace(microsecond=0)


def hm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm_to_minutes(shift_end) <= hm_to_minutes(shift_start)


def attendance_date(punch_in: dt.datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; for an overnight shift a punch-in earlier than
    shift_end (IST time of day) belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < hm_to_minutes(shift_end) * 60:
            day -= dt.timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[dt.datetime, dt.datetime]:
    """Aware IST datetimes for the start and end of the shift that belongs to attendance date
    `date_str`. The end of an overnight shift falls on the next calendar day."""
    day = dt.date.fromisoformat(date_str)
    s_min, e_min = hm_to_minutes(shift_start), hm_to_minutes(shift_end)
    start = dt.datetime.combine(day, dt.time(s_min // 60, s_min % 60), tzinfo=IST)
    end = dt.datetime.combine(day, dt.time(e_min // 60, e_min % 60), tzinfo=IST)
    if e_min <= s_min:
        end += dt.timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: dt.datetime, shift_start_dt: dt.datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor-minutes since start."""
    seconds = int((trunc_sec(punch_in) - shift_start_dt).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: dt.datetime, shift_end_dt: dt.datetime) -> int:
    """R3: floor-minutes after shift end, counted only when >= 30."""
    seconds = int((trunc_sec(punch_out) - shift_end_dt).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: dt.datetime, punch_out: dt.datetime) -> Decimal:
    """R4: seconds / 3600 rounded to 2 decimals HALF-UP. Decimal avoids float and banker's rounding."""
    seconds = int((trunc_sec(punch_out) - trunc_sec(punch_in)).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive_fields(status: str, punch_in: Optional[dt.datetime], punch_out: Optional[dt.datetime],
                  date_str: str, shift_start: str, shift_end: str) -> dict:
    """All four derived fields for a record in one place (used by punch-in, punch-out and PATCH)."""
    result = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return result
    start_dt, end_dt = shift_bounds(date_str, shift_start, shift_end)
    result["late_minutes"] = compute_late_minutes(punch_in, start_dt)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        result["work_hours"] = float(hours)
        result["overtime_minutes"] = compute_overtime(punch_out, end_dt)
        result["half_day"] = hours < HALF_DAY_LIMIT  # compares the ROUNDED value (R5)
    return result


def round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_weekdays(first: dt.date, last: dt.date) -> int:
    """Number of Mon-Fri days in [first, last] (R7); 0 when the range is empty."""
    if last < first:
        return 0
    full_weeks, extra = divmod((last - first).days + 1, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (first + dt.timedelta(days=i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# 3. Validation types and request models
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def _check_date(value: str) -> str:
    if not _DATE_RE.match(value):
        raise ValueError("must be a calendar date in YYYY-MM-DD format")
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a valid calendar date") from None
    return value


def _check_month(value: str) -> str:
    if not _MONTH_RE.match(value):
        raise ValueError("must be a month in YYYY-MM format")
    try:
        dt.date.fromisoformat(value + "-01")
    except ValueError:
        raise ValueError("not a valid month") from None
    return value


DateStr = Annotated[str, AfterValidator(_check_date)]
MonthStr = Annotated[str, AfterValidator(_check_month)]
# Strict int: floats (1.5e12 / 1783312500000.0), strings and booleans are rejected with 422.
# The lower bound rejects "seconds-looking" values (they would be 1970 in milliseconds).
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
LimitQ = Annotated[int, Query(ge=1, le=50)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_date(cls, v: str) -> str:
        return _check_date(v)

    @field_validator("name", "department")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    # Derived fields (work_hours, ...) are not declared, so a client-sent value is silently ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def _validation_error(loc: tuple, message: str, kind: str = "value_error") -> RequestValidationError:
    """Build a 422 with FastAPI's standard body for checks that cannot be expressed as a type."""
    return RequestValidationError([{"type": kind, "loc": loc, "msg": message, "input": None}])


# --------------------------------------------------------------------------- #
# 4. Response shaping
# --------------------------------------------------------------------------- #
def _ms(value: Optional[dt.datetime]) -> Optional[int]:
    return None if value is None else dt_to_ms(value)


def _convert_value(value: Any) -> Any:
    """Datetimes inside history.changes become epoch ms; everything else is passed through."""
    return dt_to_ms(value) if isinstance(value, dt.datetime) else value


def history_out(entry: dict) -> dict:
    return {
        "at": _ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _convert_value(c.get("from")), "to": _convert_value(c.get("to"))}
            for field, c in (entry.get("changes") or {}).items()
        },
    }


def attendance_out(doc: dict) -> dict:
    """Stored document -> API record. Missing legacy fields are treated as 0 / false / empty."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms(doc.get("punch_in")),
        "punch_out": _ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# 5. Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    global _indexes_ready
    emp, att = db.employees, db.attendance_logs
    # employees
    emp.create_index([("emp_code", 1)], unique=True, name="uq_emp_code")  # identity + 409 on duplicates
    emp.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")  # list by department, sorted
    emp.create_index([("department", 1), ("joined_on", 1)], name="dept_joined_on")  # headcount per department
    emp.create_index([("joined_on", 1)], name="joined_on")  # headcount without a department filter
    # attendance_logs
    att.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_emp_date")  # one record per day
    att.create_index([("date", -1), ("emp_code", 1)], name="date_emp")  # list sort + month range scans
    att.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")  # status filter
    att.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")  # punch-out: latest punch-in
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # the app must still come up; /health reports 503 until MongoDB answers
        log.exception("Could not create indexes at startup; will retry on /health")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def _mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 6a. Health & employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except Exception:
        raise HTTPException(503, "MongoDB is not reachable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        # No find-then-insert: the unique index is the single source of truth, so concurrent
        # creates of the same emp_code produce exactly one 201 and the rest 409.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    flt: dict = {}
    if department is not None:
        flt["department"] = department
    total = db.employees.count_documents(flt)
    cursor = (db.employees.find(flt, {"_id": 0}).sort("emp_code", 1)
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 6b. Attendance: list, punch-in, punch-out, regularize
# --------------------------------------------------------------------------- #
def build_attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Shared by GET /attendance and the explain endpoint so both run the very same query."""
    if date_from and date_to and date_from > date_to:  # ISO dates compare correctly as strings
        raise _validation_error(("query", "date_from"), "date_from must not be after date_to")
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        flt["date"] = {}
        if date_from:
            flt["date"]["$gte"] = date_from
        if date_to:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    return {"filter": flt, "sort": [("date", -1), ("emp_code", 1)],
            "skip": (page - 1) * page_size, "limit": page_size, "projection": {"_id": 0}}


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[DateStr] = None,
                    date_to: Optional[DateStr] = None, status: Optional[Status] = None,
                    page: PageQ = 1, page_size: PageSizeQ = 20):
    q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    flt = q["filter"]
    total = db.attendance_logs.count_documents(flt)  # exact; uses the same indexes as the query itself
    cursor = (db.attendance_logs.find(flt, q["projection"]).sort(q["sort"]).skip(q["skip"]).limit(q["limit"]))
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    moment = trunc_sec(ms_to_dt(body.punched_at)) if body.punched_at is not None else trunc_sec(utc_now())
    date_str = attendance_date(moment, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": moment,
        "punch_out": None,
        **derive_fields(body.status, moment, None, date_str, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index decides the winner atomically.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    moment = trunc_sec(ms_to_dt(body.punched_at)) if body.punched_at is not None else trunc_sec(utc_now())

    for _ in range(3):
        # Most recent record whose punch_in is at or before the punch-out instant. Works across
        # midnight because we search by instant, not by calendar date.
        rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": moment}}, sort=[("punch_in", -1)])
        if rec is None:
            raise HTTPException(404, "no punch-in found for this employee")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        punch_in_dt = rec["punch_in"]
        elapsed = (moment - trunc_sec(punch_in_dt)).total_seconds()
        if elapsed <= 0:
            raise HTTPException(422, "punched_at must be after the punch-in")
        if elapsed > MAX_SHIFT_SECONDS:
            raise HTTPException(422, "punched_at is more than 24 hours after the punch-in")
        derived = derive_fields(rec["status"], punch_in_dt, moment, rec["date"], emp["shift_start"], emp["shift_end"])
        # Compare-and-set: only succeeds while the record is still open AND its punch_in is unchanged
        # (a concurrent regularization would otherwise leave us with stale derived values).
        updated = db.attendance_logs.find_one_and_update(
            {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None, "punch_in": punch_in_dt},
            {"$set": {"punch_out": moment, **derived}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return attendance_out(updated)
        # Lost a race: loop re-reads; if someone else already closed it the next pass returns 409.
    raise HTTPException(409, "record was modified concurrently, please retry")


def _same_instant(a: Optional[dt.datetime], b: Optional[dt.datetime]) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and dt_to_ms(a) == dt_to_ms(b))


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    for _ in range(5):  # optimistic concurrency: retry when another writer got in between
        old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if old is None:
            raise HTTPException(404, "no attendance record for that date")
        new_fields, changes = _plan_regularization(old, emp, body)
        entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        n = len(old.get("history") or [])
        guard = {
            "emp_code": emp_code, "date": date,
            "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"),
            # history must still have exactly n entries (index n absent, index n-1 present)
            f"history.{n}": {"$exists": False},
        }
        if n:
            guard[f"history.{n - 1}"] = {"$exists": True}
        updated = db.attendance_logs.find_one_and_update(
            guard, {"$set": new_fields, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return attendance_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


def _plan_regularization(old: dict, emp: dict, body: RegularizeRequest) -> tuple[dict, dict]:
    """Validate the correction and compute (fields to $set, history 'changes'). Raises 422 on rule violations."""
    status = body.status or old["status"]
    old_in, old_out = old.get("punch_in"), old.get("punch_out")
    times_supplied = body.punch_in is not None or body.punch_out is not None

    if status in NON_PRESENCE:
        if times_supplied:
            raise HTTPException(422, f"punch times cannot be supplied for status {status}")
        new_in = new_out = None
    else:
        new_in = trunc_sec(ms_to_dt(body.punch_in)) if body.punch_in is not None else old_in
        new_out = trunc_sec(ms_to_dt(body.punch_out)) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, f"status {status} requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != old["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and times_supplied:
            gap = (trunc_sec(new_out) - trunc_sec(new_in)).total_seconds()
            if gap <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if gap > MAX_SHIFT_SECONDS:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if status == old["status"] and _same_instant(new_in, old_in) and _same_instant(new_out, old_out):
        raise HTTPException(422, "the request does not change anything")

    derived = derive_fields(status, new_in, new_out, old["date"], emp["shift_start"], emp["shift_end"])
    new_fields = {"status": status, "punch_in": new_in, "punch_out": new_out, **derived}

    old_values = {
        "status": old["status"], "punch_in": old_in, "punch_out": old_out,
        "work_hours": old.get("work_hours"), "late_minutes": int(old.get("late_minutes") or 0),
        "overtime_minutes": int(old.get("overtime_minutes") or 0), "half_day": bool(old.get("half_day") or False),
    }
    changes = {}
    for field, new_value in new_fields.items():
        old_value = old_values[field]
        if field in ("punch_in", "punch_out"):
            differs = not _same_instant(old_value, new_value)
        else:
            differs = old_value != new_value
        if differs:
            changes[field] = {"from": old_value, "to": new_value}  # datetimes stay BSON dates in storage
    return new_fields, changes


# --------------------------------------------------------------------------- #
# 7. Analytics (aggregation pipelines)
# --------------------------------------------------------------------------- #
# Pipelines work in "half-day units" (full day = 2, half day = 1) and in integer hundredths /
# ten-thousandths so that all rounding is exact integer arithmetic. MongoDB's $round is
# banker's rounding, but R8 wants HALF-UP; floor((2n + d) / (2d)) is half-up for n/d.
def _present_weight(status_ref: str, half_ref: str) -> dict:
    """2 for a full present day, 1 for a half day, 0 when not present (R5/R6)."""
    return {"$cond": [
        {"$in": [status_ref, list(PRESENCE)]},
        {"$cond": [{"$eq": [{"$ifNull": [half_ref, False]}, True]}, 1, 2]},
        0,
    ]}


def _weekday_only(weight: dict, date_ref: str) -> dict:
    """Keep the weight only if the date string falls on Mon-Fri (R7)."""
    day_of_week = {"$isoDayOfWeek": {"$dateFromString": {"dateString": date_ref}}}  # 1 = Mon ... 7 = Sun
    return {"$cond": [{"$lte": [day_of_week, 5]}, weight, 0]}


def _late(ref: str) -> dict:
    return {"$ifNull": [ref, 0]}


def _half_up_div(numerator: Any, denominator: Any) -> dict:
    """floor(numerator/denominator + 1/2) as an expression: exact half-up rounding to an integer."""
    return {"$floor": {"$divide": [
        {"$add": [{"$multiply": [numerator, 2]}, denominator]},
        {"$multiply": [denominator, 2]},
    ]}}


# ---- employee monthly ------------------------------------------------------ #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "p2": {"$sum": _weekday_only(_present_weight("$status", "$half_day"), "$date")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late("$late_minutes"), 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _late("$late_minutes")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    start, end = month_bounds(month)
    first_day = max(dt.date.fromisoformat(start), dt.date.fromisoformat(emp["joined_on"]))  # R7
    working_days = count_weekdays(first_day, dt.date.fromisoformat(end))

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    present_days = (r.get("p2") or 0) / 2
    pct = None
    if working_days > 0:
        pct = float(round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 2))
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present_days, "leave_days": int(r.get("leave_days") or 0),
        "late_count": int(r.get("late_count") or 0),
        "total_late_minutes": int(r.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(r.get("total_overtime_minutes") or 0),
        "attendance_pct": pct,
    }


# ---- department summary ------------------------------------------------------ #
def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end}}  # R9: joined on/before month end, logs or not
    if department is not None:
        emp_match["department"] = department
    has_hours = {"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$isNumber": "$$l.work_hours"}]}

    def per_log(expr: dict) -> dict:
        return {"$map": {"input": "$logs", "as": "l", "in": expr}}

    def count_where(cond: dict) -> dict:
        return {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": cond}}}

    return [
        {"$match": emp_match},
        # Start from employees so people with zero logs still produce a row (headcount); the logs are
        # attached per employee through the (emp_code, date) index.
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "_id": 0, "department": 1,
            "p2": {"$sum": per_log(_weekday_only(_present_weight("$$l.status", "$$l.half_day"), "$$l.date"))},
            "late_count": count_where({"$gt": [_late("$$l.late_minutes"), 0]}),
            "late_total": {"$sum": per_log(_late("$$l.late_minutes"))},
            "leave_count": count_where({"$eq": ["$$l.status", "LEAVE"]}),
            "on_duty_count": count_where({"$eq": ["$$l.status", "ON_DUTY"]}),
            # work hours as integer hundredths so the later average is exact
            "w_sum": {"$sum": per_log({"$cond": [has_hours, {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}, 0]})},
            "w_cnt": {"$sum": per_log({"$cond": [has_hours, 1, 0]})},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "p2": {"$sum": "$p2"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$late_total"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "w_sum": {"$sum": "$w_sum"},
            "w_cnt": {"$sum": "$w_cnt"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": {"$divide": ["$p2", 2]},
            # mean over RECORDS (sum / count), half-up to 2 decimals
            "avg_work_hours": {"$cond": [{"$gt": ["$w_cnt", 0]},
                                         {"$divide": [_half_up_div("$w_sum", "$w_cnt"), 100]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        items.append({
            "department": r["department"], "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": None if r.get("avg_work_hours") is None else float(r["avg_work_hours"]),
            "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


# ---- late leaderboard -------------------------------------------------------- #
def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$emp_code",
            "total": {"$sum": _late("$late_minutes")},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late("$late_minutes"), 0]}, 1, 0]}},
        }},
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # inner join: logs of unknown employees disappear
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4).
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff stay
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": "$total", "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: LimitQ = 10, department: Optional[str] = None):
    items = [{
        "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
        "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
    } for r in db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department))]
    return {"month": month, "items": items}


# ---- department trend ---------------------------------------------------------- #
def department_trend_pipeline(department: str, from_day: dt.date, to_day: dt.date) -> list:
    lo = dt.datetime(from_day.year, from_day.month, from_day.day, tzinfo=UTC)
    hi = dt.datetime(to_day.year, to_day.month, to_day.day, tzinfo=UTC)
    hi_exclusive = hi + dt.timedelta(days=1)
    start, end = from_day.isoformat(), to_day.isoformat()
    joined = {"$dateFromString": {"dateString": "$joined_on"}}

    # One "event" per employee (+1 headcount from their join day, clamped into the range) and one per log.
    join_event = {
        "day": {"$cond": [{"$or": [{"$lte": [joined, lo]}, {"$gt": [joined, hi]}]}, lo, joined]},
        "hc": {"$cond": [{"$lte": [joined, hi]}, 1, 0]},  # not joined yet -> contributes nothing
        "p2": 0, "late": 0,
    }
    log_event = {"$map": {"input": "$logs", "as": "l", "in": {
        "day": {"$dateFromString": {"dateString": "$$l.date"}},
        "hc": 0,
        "p2": _present_weight("$$l.status", "$$l.half_day"),  # weekends count here (unlike present_days)
        "late": {"$cond": [{"$gt": [_late("$$l.late_minutes"), 0]}, 1, 0]},
    }}}
    sum_window = {"documents": ["unbounded", "current"]}
    last7 = {"documents": [-6, "current"]}

    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {"_id": 0, "events": {"$concatArrays": [[join_event], log_event]}}},
        {"$unwind": "$events"},
        {"$replaceRoot": {"newRoot": "$events"}},
        {"$group": {"_id": "$day", "hc": {"$sum": "$hc"}, "p2": {"$sum": "$p2"}, "late": {"$sum": "$late"}}},
        {"$project": {"_id": 0, "day": "$_id", "hc": 1, "p2": 1, "late": 1}},
        {"$sort": {"day": 1}},
        # Gap filling in the database: one document per calendar day in [from, to].
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi_exclusive]}}},
        {"$match": {"day": {"$gte": lo, "$lte": hi}}},
        {"$addFields": {"hc": {"$ifNull": ["$hc", 0]}, "p2": {"$ifNull": ["$p2", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        # headcount on a day = running total of "joined on or before that day"
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"headcount": {"$sum": "$hc", "window": sum_window}}}},
        {"$addFields": {"is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            # rate = (p2/2)/headcount, in ten-thousandths, half-up:  floor(p2*10000/(2h) + 1/2)
            _half_up_div({"$multiply": ["$p2", 10000]}, {"$multiply": ["$headcount", 2]}),
            None,
        ]}}},
        # moving average over the current row and up to 6 before it, inside the range only
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "rate_sum": {"$sum": "$rate_int", "window": last7},
            "rate_cnt": {"$sum": {"$cond": [{"$isNumber": "$rate_int"}, 1, 0]}, "window": last7},
        }}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]},
            "late_count": "$late",
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_int"}, {"$divide": ["$rate_int", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$rate_cnt", 0]},
                                        {"$divide": [_half_up_div("$rate_sum", "$rate_cnt"), 10000]}, None]},
        }},
    ]


def _check_trend_range(from_day: str, to_day: str) -> tuple[dt.date, dt.date]:
    start, end = dt.date.fromisoformat(from_day), dt.date.fromisoformat(to_day)
    if end < start:
        raise _validation_error(("query", "to"), "to must not be before from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise _validation_error(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return start, end


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: Annotated[DateStr, Query(alias="from")],
                     to: DateStr):
    start, end = _check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(department_trend_pipeline(department, start, end)):
        items.append({
            "date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
            "attendance_rate": None if r.get("attendance_rate") is None else float(r["attendance_rate"]),
            "moving_avg_7d": None if r.get("moving_avg_7d") is None else float(r["moving_avg_7d"]),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def _need(**params: Any) -> None:
    for name, value in params.items():
        if value is None:
            raise _validation_error(("query", name.rstrip("_")), "Field required", "missing")


def _run_explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: ExplainTarget,
            emp_code: Optional[str] = None, month: Optional[MonthStr] = None, department: Optional[str] = None,
            limit: LimitQ = 10, date_from: Optional[DateStr] = None, date_to: Optional[DateStr] = None,
            status: Optional[Status] = None,
            from_: Annotated[Optional[DateStr], Query(alias="from")] = None, to: Optional[DateStr] = None,
            page: PageQ = 1, page_size: PageSizeQ = 20):
    """Runs explain('executionStats') on the main query/pipeline of the named endpoint. The query is
    produced by the same builder function the real endpoint calls, so the plan shown is the real plan."""
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q["filter"], "sort": dict(q["sort"]),
                   "skip": q["skip"], "limit": q["limit"], "projection": q["projection"]}
    elif endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        _need(month=month)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        _need(month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(month, limit, department),
                   "cursor": {}}
    else:  # department_trend
        _need(department=department, from_=from_, to=to)
        start, end = _check_trend_range(from_, to)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(department, start, end),
                   "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": _run_explain(command)}
