"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Layout of this file:
  1. Config and database
  2. Time helpers            (epoch ms <-> datetime, IST, attendance date)
  3. Business rule helpers   (R2 - R5, half-up rounding)
  4. Request models
  5. Serializers             (MongoDB document -> API shape)
  6. Query / pipeline builders (shared by the endpoints and /admin/explain)
  7. Endpoints
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config and database
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False by default, so real environment variables win

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS = 100_000_000_000  # anything smaller looks like seconds
MAX_MS = 4_102_444_800_000  # 2100-01-01
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
MAX_SHIFT_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    # Uniqueness of emp_code; also every lookup of one employee.
    db.employees.create_index([("emp_code", 1)], unique=True)
    # GET /employees?department= sorted by emp_code; department lookups for the trend.
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    # Headcount (joined_on <= end of period) when no department is given.
    db.employees.create_index([("joined_on", 1)])
    # One record per employee per day (punch-in race); per-employee reads.
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # Default list order (date desc, emp_code asc) and month ranges.
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # List filtered by status, in list order.
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    # Punch-out: the latest record with punch_in <= punched_at.
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        pass  # database not reachable yet; /health reports 503
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
def database_error(_: Request, __: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_date(value: str, name: str) -> date:
    """Strict YYYY-MM-DD; anything else is a 422."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid date")


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:])
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def at_ist(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def shift_times(emp: dict) -> tuple[str, str]:
    return emp.get("shift_start") or "09:30", emp.get("shift_end") or "18:30"


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: the IST calendar date of the punch-in. For an overnight shift, a punch-in
    earlier than shift_end belongs to the shift that started the previous day."""
    start, end = shift_times(emp)
    local = punch_in.astimezone(IST)
    day = local.date()
    if end <= start and local.strftime("%H:%M:%S") < end + ":00":
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end as instants for one attendance date."""
    start, end = shift_times(emp)
    day = date.fromisoformat(date_str)
    end_day = day + timedelta(days=1) if end <= start else day
    return at_ist(day, start), at_ist(end_day, end)


# --------------------------------------------------------------------------- #
# 3. Business rule helpers
# --------------------------------------------------------------------------- #
def half_up(value, places: int = 2) -> float:
    """Round half-up (R8). round(value, 9) first removes binary float noise."""
    if isinstance(value, float):
        value = round(value, 9)
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only when strictly more than 10:00 after shift start."""
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, counted only from 30 minutes."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(emp: dict, date_str: str, status: str, punch_in, punch_out) -> dict:
    """All four derived fields for a record, from its final state (R2 - R5)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    start, end = shift_bounds(date_str, emp)
    out["late_minutes"] = compute_late_minutes(punch_in, start)
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, end)
        out["half_day"] = out["work_hours"] < 4.5
    return out


def working_days(first: date, last: date) -> int:
    """R7: Mon-Fri days in [first, last]."""
    count, day = 0, first
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
# strict=True rejects floats, strings and booleans instead of coercing them.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"

Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=MONTH)]
Limit = Annotated[int, Query(ge=1, le=50)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(value)  # ValueError for e.g. 2026-02-30
        return value

    @model_validator(mode="after")
    def shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 5. Serializers
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    start, end = shift_times(doc)
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": start,
        "shift_end": end,
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _instant_out(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def record_out(doc: dict) -> dict:
    """Legacy records may omit history / half_day / minutes: default them."""
    history = [
        {
            "at": dt_to_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": {
                field: {"from": _instant_out(change.get("from")), "to": _instant_out(change.get("to"))}
                for field, change in (entry.get("changes") or {}).items()
            },
        }
        for entry in doc.get("history") or []
    ]
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": history,
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Query / pipeline builders
#    Each endpoint and its /admin/explain twin call the same builder, so the
#    explained operation is exactly the one that runs.
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = {"date": -1, "emp_code": 1}

# Aggregation expressions over one attendance record. They read the STORED
# derived values; nothing is recomputed from punch times.
_LATE = {"$ifNull": ["$late_minutes", 0]}
_OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
_IS_LATE = {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
# R5/R6: a presence record is worth 1 day, or 0.5 when it is a half day.
_PRESENT_VALUE = {"$cond": [_IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}
_DAY = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}
_IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": _DAY}, 5]}  # ISO: Mon=1 .. Sun=7
# R7: present_days only counts Mon-Fri records.
_PRESENT_WEEKDAY_VALUE = {"$cond": [_IS_WEEKDAY, _PRESENT_VALUE, 0]}


def _is_status(status: str) -> dict:
    return {"$cond": [{"$eq": ["$status", status]}, 1, 0]}


def _month_range(month: str) -> dict:
    first, last = month_bounds(month)
    return {"$gte": first.isoformat(), "$lte": last.isoformat()}


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    start = parse_date(date_from, "date_from") if date_from is not None else None
    end = parse_date(date_to, "date_to") if date_to is not None else None
    if start and end and start > end:
        raise HTTPException(422, "date_from must not be after date_to")
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = start.isoformat()
        if end:
            query["date"]["$lte"] = end.isoformat()
    if status is not None:
        query["status"] = status
    return query


def monthly_pipeline(emp_code: str, month: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": _month_range(month)}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _PRESENT_WEEKDAY_VALUE},
                "leave_days": {"$sum": _is_status("LEAVE")},
                "late_count": {"$sum": _IS_LATE},
                "total_late_minutes": {"$sum": _LATE},
                "total_overtime_minutes": {"$sum": _OVERTIME},
            }
        },
    ]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees so that people with zero logs still count (R9)."""
    _, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [_IS_PRESENT, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": _month_range(month)}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": _PRESENT_WEEKDAY_VALUE},
                            "hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
                            "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
                            "late_count": {"$sum": _IS_LATE},
                            "total_late_minutes": {"$sum": _LATE},
                            "leave_count": {"$sum": _is_status("LEAVE")},
                            "on_duty_count": {"$sum": _is_status("ON_DUTY")},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        # Keep employees without logs: they add 1 to headcount and 0 elsewhere.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$logs.present_days"},
                # Sum and count are carried separately so the average is over
                # records, not an average of per-employee averages.
                "hours_sum": {"$sum": "$logs.hours_sum"},
                "hours_count": {"$sum": "$logs.hours_count"},
                "late_count": {"$sum": "$logs.late_count"},
                "total_late_minutes": {"$sum": "$logs.total_late_minutes"},
                "leave_count": {"$sum": "$logs.leave_count"},
                "on_duty_count": {"$sum": "$logs.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": _month_range(month), "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank is standard competition ranking: ties share a rank, next is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    start, end = parse_date(date_from, "from"), parse_date(date_to, "to")
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")
    members = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not members:
        raise HTTPException(404, "department not found")
    codes = [m["emp_code"] for m in members]
    joined = [m.get("joined_on") or "" for m in members]

    first_day = datetime(start.year, start.month, start.day, tzinfo=UTC)
    after_last_day = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    # Half-up to 4 decimals inside the database ($round is half-even).
    rate = {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": date_from, "$lte": date_to}}},
        # One empty anchor row so $densify has something to extend from even
        # when the range holds no records at all.
        {"$unionWith": {"pipeline": [{"$documents": [{"date": date_from}]}]}},
        {"$group": {"_id": "$date", "present_count": {"$sum": _PRESENT_VALUE}, "late_count": {"$sum": _IS_LATE}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}, "present_count": 1, "late_count": 1}},
        # Exactly one row per calendar day in [from, to].
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [first_day, after_last_day]}}},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            }
        },
        # R9: department employees who have joined on or before that day.
        {"$set": {"headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "cond": {"$lte": ["$$this", "$date"]}}}}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, rate, None]}}},
        # Current row + up to 6 preceding rows; $avg skips null rates.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "day": 0}},
    ]


# --------------------------------------------------------------------------- #
# 7. Endpoints
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
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides the winner
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, day, body.status, ts, None),
        "history": [],
    }
    try:
        # No check-then-insert: the unique (emp_code, date) index lets exactly
        # one of several simultaneous requests through.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Searching by punch_in instant (not by date) is what makes overnight shifts work.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = record["punch_in"]
    if ts <= started or ts - started > MAX_SHIFT_SPAN:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    derived = derive(emp, record["date"], record["status"], started, ts)
    # The filter repeats "still open": of two simultaneous punch-outs only one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_in": started, "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query) if query else db.attendance_logs.estimated_document_count()
    cursor = db.attendance_logs.find(query).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, day: Annotated[str, Path(alias="date")], body: RegularizeIn):
    parse_date(day, "date")
    emp = get_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    old = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day")),
    }
    status = body.status or old["status"]
    if status in PRESENCE:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, f"status {status} requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp) != day:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT_SPAN):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours of it")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"status {status} cannot have punch times")
        new_in = new_out = None

    new = {"status": status, "punch_in": new_in, "punch_out": new_out}
    if all(new[field] == old[field] for field in new):
        raise HTTPException(422, "the request changes nothing")
    new.update(derive(emp, day, status, new_in, new_out))
    changes = {f: {"from": old[f], "to": new[f]} for f in new if new[f] != old[f]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record is still
    # exactly as we read it (same values, same number of history entries).
    # A concurrent edit makes the filter miss, and the loser gets a 409
    # instead of overwriting the winner's history.
    seen = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            f"history.{seen}": {"$exists": False},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return record_out(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    stats = rows[0] if rows else {}

    first, last = month_bounds(month)
    joined = emp.get("joined_on")
    if joined:
        first = max(first, date.fromisoformat(joined))  # R7: mid-month joiner
    days = working_days(first, last)
    present = stats.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(Decimal(str(present)) * 100 / days, 2) if days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    items = []
    for row in db.employees.aggregate(summary_pipeline(month, department)):
        count = row["hours_count"]
        items.append(
            {
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": row["present_days"],
                # MongoDB's $round is half-even, so the half-up rounding happens here.
                "avg_work_hours": half_up(Decimal(str(round(row["hours_sum"], 6))) / count, 2) if count else None,
                "late_count": row["late_count"],
                "total_late_minutes": row["total_late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[str, Query(alias="from")],
    date_to: Annotated[str, Query(alias="to")],
):
    items = list(db.attendance_logs.aggregate(trend_pipeline(department, date_from, date_to)))
    for row in items:
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = half_up(row["moving_avg_7d"], 4)
    return {"department": department, "items": items}


ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(**params) -> None:
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(422, f"missing required query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH)] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    range_from: Annotated[Optional[str], Query(alias="from")] = None,
    range_to: Annotated[Optional[str], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    collection = "attendance_logs"
    if endpoint == "attendance_list":
        operation = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": ATTENDANCE_SORT,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            pipeline = monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            collection = "employees"
            pipeline = summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            pipeline = leaderboard_pipeline(month, limit, department)
        else:
            _require(department=department, **{"from": range_from, "to": range_to})
            pipeline = trend_pipeline(department, range_from, range_to)
        operation = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    plan = db.command({"explain": operation, "verbosity": "executionStats"})
    # The raw plan contains BSON types (timestamps, dates); make it plain JSON.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(plan))}
