"""Employee Attendance & Analytics API (FastAPI + MongoDB).

Everything lives in this one file, in this order:
  1. configuration and database handles
  2. small pure helpers (time, rounding, business rules R1-R5)
  3. request models and validation helpers
  4. query / pipeline builders (shared by the real endpoints and /admin/explain)
  5. endpoints
"""
import calendar
import json
import os
import re
import time as time_mod
from contextlib import asynccontextmanager
from datetime import date as date_t, datetime, time as time_t, timedelta, timezone
from fractions import Fraction
from math import floor
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------------------
# 1. Configuration. load_dotenv() never overrides variables that are already set, so real
#    environment variables win over a local .env file.
# --------------------------------------------------------------------------------------
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back UTC-aware datetimes instead of naive ones, so we never
# have to guess what a datetime means.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
OFF_STATUSES = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HUNDREDTHS = 450  # R5: rounded work_hours < 4.50
MAX_SHIFT_SECONDS = 24 * 3600  # punch-out / correction limit
MAX_TREND_DAYS = 92

MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"

_indexes_ready = False


def ensure_indexes() -> None:
    """Create every index we rely on. Idempotent: same spec + same name is a no-op."""
    global _indexes_ready
    # employees
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    employees.create_index([("joined_on", ASCENDING)], name="joined")
    # attendance_logs
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punchin")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The database may still be starting; retry for a few seconds, never crash the app.
    deadline = time_mod.monotonic() + 8
    while True:
        try:
            ensure_indexes()
            break
        except PyMongoError:
            if time_mod.monotonic() > deadline:
                break  # /health keeps retrying and reports 503 until Mongo answers
            time_mod.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------------------
# 2. Pure helpers
# --------------------------------------------------------------------------------------
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(value: datetime) -> int:
    """datetime -> epoch ms using integer arithmetic only (no float rounding)."""
    if value.tzinfo is None:  # defensive; the client is tz_aware so this should not happen
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return ms_to_dt(dt_to_ms(datetime.now(UTC)))


def now_ms_precision() -> datetime:
    """Now, at the millisecond precision BSON can store."""
    return EPOCH + timedelta(milliseconds=dt_to_ms(datetime.now(UTC)))


def round_half_up(value: Fraction, places: int) -> float:
    """Exact half-up rounding (R4, R8). Python's round() and Mongo's $round are banker's."""
    scale = 10**places
    return floor(value * scale + Fraction(1, 2)) / scale


def hhmm_to_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in
    belongs to the day the shift started."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < hhmm_to_minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def shift_window(att_date: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that belongs to attendance date `att_date`."""
    day = date_t.fromisoformat(att_date)
    s, e = hhmm_to_minutes(shift_start), hhmm_to_minutes(shift_end)
    start = datetime.combine(day, time_t(s // 60, s % 60), tzinfo=IST)
    end_day = day + timedelta(days=1) if e <= s else day
    end = datetime.combine(end_day, time_t(e // 60, e % 60), tzinfo=IST)
    return start, end


def compute_late(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: strictly more than 10:00 after shift start -> whole minutes since shift start."""
    elapsed = int((punch_in - shift_start_dt).total_seconds())
    return elapsed // 60 if elapsed > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes after shift end, only if at least 30."""
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_derived(status: str, p_in: Optional[datetime], p_out: Optional[datetime], emp: dict, att_date: str) -> dict:
    """The four derived fields for a record, per the table in DATA_MODEL.md."""
    if status not in PRESENCE or p_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start_dt, end_dt = shift_window(att_date, emp["shift_start"], emp["shift_end"])
    late = compute_late(p_in, start_dt)
    if p_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    seconds = int((p_out - p_in).total_seconds())
    hundredths = floor(Fraction(seconds, 3600) * 100 + Fraction(1, 2))  # R4, half-up
    return {
        "work_hours": hundredths / 100,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(p_out, end_dt),
        "half_day": hundredths < HALF_DAY_HUNDREDTHS,  # R5, on the rounded value
    }


def history_value_out(key: str, value):
    return dt_to_ms(value) if key in ("punch_in", "punch_out") and isinstance(value, datetime) else value


def record_out(doc: dict) -> dict:
    """Mongo document -> API shape. Tolerates legacy documents (no history / half_day)."""
    history = []
    for h in doc.get("history") or []:
        changes = {
            k: {"from": history_value_out(k, v.get("from")), "to": history_value_out(k, v.get("to"))}
            for k, v in (h.get("changes") or {}).items()
        }
        history.append({"at": dt_to_ms(h["at"]), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    p_in, p_out = doc.get("punch_in"), doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(p_in) if p_in else None,
        "punch_out": dt_to_ms(p_out) if p_out else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
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
        "created_at": dt_to_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------------------
# 3. Models and validation helpers
# --------------------------------------------------------------------------------------
def fail_422(loc: tuple, msg: str):
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_ymd(value: str, loc: tuple) -> date_t:
    """Strict YYYY-MM-DD that must also be a real calendar date."""
    if not re.fullmatch(DATE_RE, value):
        fail_422(loc, "must be a date in YYYY-MM-DD format")
    try:
        return date_t.fromisoformat(value)
    except ValueError:
        fail_422(loc, "not a valid calendar date")


def month_bounds(month: str, loc: tuple = ("query", "month")) -> tuple[str, str]:
    if not re.fullmatch(MONTH_RE, month):
        fail_422(loc, "must be a month in YYYY-MM format")
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        fail_422(loc, "invalid year")
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def count_weekdays(first: str, last: str) -> int:
    """Mon-Fri days in [first, last], inclusive (R7). At most ~31 iterations of date maths."""
    d, end, n = date_t.fromisoformat(first), date_t.fromisoformat(last), 0
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=DATE_RE)

    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date_t.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on is not a valid calendar date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # Omitted means "unchanged"; an explicit null is not a valid value for any of them.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    # Strip `ctx` (it can hold exception objects that are not JSON serialisable).
    errors = [{k: v for k, v in e.items() if k != "ctx"} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def paginate_args(page: int, page_size: int) -> tuple[int, int]:
    skip = (page - 1) * page_size
    return (skip if skip < 2**31 else 2**31 - 1), page_size


# --------------------------------------------------------------------------------------
# 4. Query / pipeline builders (used by the endpoints AND by /admin/explain)
# --------------------------------------------------------------------------------------
LOG_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


# Aggregation expressions over one attendance document. Derived values are read as stored,
# never recomputed (DATA_MODEL.md, "Stored derived values are authoritative").
_late = {"$ifNull": ["$late_minutes", 0]}
_is_presence = {"$in": ["$status", list(PRESENCE)]}
_is_weekday = {  # $dayOfWeek: Sunday = 1 ... Saturday = 7, so Mon-Fri = 2..6
    "$in": [
        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}},
        [2, 3, 4, 5, 6],
    ]
}
_halves = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}  # present value x2: half day = 1
_has_hours = {"$and": [_is_presence, {"$isNumber": "$work_hours"}]}

# present days are kept as integer "halves" (x2) so every ratio can be rounded exactly in one place.
M_PRESENT_X2 = {"$sum": {"$cond": [{"$and": [_is_presence, _is_weekday]}, _halves, 0]}}
M_LEAVE = {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}
M_ON_DUTY = {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}
M_LATE_COUNT = {"$sum": {"$cond": [{"$gt": [_late, 0]}, 1, 0]}}
M_LATE_TOTAL = {"$sum": _late}
M_OT_TOTAL = {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
# work hours summed as integer hundredths: exact, no float drift in the later division
M_HOURS_X100 = {"$sum": {"$cond": [_has_hours, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}}
M_HOURS_CNT = {"$sum": {"$cond": [_has_hours, 1, 0]}}


def monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_x2": M_PRESENT_X2,
                "leave_days": M_LEAVE,
                "late_count": M_LATE_COUNT,
                "total_late_minutes": M_LATE_TOTAL,
                "total_overtime_minutes": M_OT_TOTAL,
            }
        },
    ]


def dept_summary_query(first: str, last: str, department: Optional[str]):
    """Starts from `employees` (so people with zero logs still count, R9), joins each one's
    month of logs through the (emp_code, date) index, then rolls up per department."""
    match: dict = {"joined_on": {"$lte": last}}
    if department:
        match["department"] = department
    hint = "dept_joined" if department else "joined"  # forces an index scan on employees
    pipeline = [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_x2": M_PRESENT_X2,
                            "hours_x100": M_HOURS_X100,
                            "hours_cnt": M_HOURS_CNT,
                            "late_count": M_LATE_COUNT,
                            "late_total": M_LATE_TOTAL,
                            "leave_count": M_LEAVE,
                            "on_duty_count": M_ON_DUTY,
                        }
                    },
                ],
                "as": "m",
            }
        },
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},  # keeps no-log employees
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_x2": {"$sum": "$m.present_x2"},
                "hours_x100": {"$sum": "$m.hours_x100"},
                "hours_cnt": {"$sum": "$m.hours_cnt"},
                "late_count": {"$sum": "$m.late_count"},
                "late_total": {"$sum": "$m.late_total"},
                "leave_count": {"$sum": "$m.leave_count"},
                "on_duty_count": {"$sum": "$m.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    return pipeline, hint


def leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": M_LATE_TOTAL, "late_count": M_LATE_COUNT}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_pipeline(department: str, start: date_t, days: int) -> list:
    """One row per calendar day, built entirely in the database."""
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joins": {"$push": "$joined_on"}}},
        # Gap filling: generate every calendar day of the range ($range + $dateAdd) ...
        {"$project": {"_id": 0, "codes": 1, "joins": 1,
                      "d": {"$map": {"input": {"$range": [0, days]}, "as": "i",
                                     "in": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$d"},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}, "dow": {"$dayOfWeek": "$d"}}},
        {"$set": {
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        # ... then attach that day's records for the department's employees (index on date).
        {"$lookup": {
            "from": "attendance_logs", "localField": "date", "foreignField": "date", "let": {"codes": "$codes"},
            "pipeline": [
                {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                {"$group": {
                    "_id": None,
                    "present_x2": {"$sum": {"$cond": [_is_presence, _halves, 0]}},  # any weekday or weekend
                    "late_count": M_LATE_COUNT,
                }},
            ],
            "as": "agg",
        }},
        {"$set": {
            "present_x2": {"$ifNull": [{"$arrayElemAt": ["$agg.present_x2", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$agg.late_count", 0]}, 0]},
        }},
        # attendance_rate x 10000, half-up, in exact integer arithmetic:
        # floor(5000 * n / h + 1/2) = floor((10000 * n + h) / (2 * h)),  n = halves present, h = headcount
        {"$set": {"rate_i": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_x2", 10000]}, "$headcount"]},
                                    {"$multiply": ["$headcount", 2]}]}},
            None,
        ]}}},
        # trailing 7-row window over the requested range only (first rows get a shorter window)
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "ma_sum": {"$sum": "$rate_i", "window": {"documents": [-6, 0]}},
            "ma_cnt": {"$sum": {"$cond": [{"$isNumber": "$rate_i"}, 1, 0]}, "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$divide": ["$present_x2", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_i"}, {"$divide": ["$rate_i", 10000]}, None]},
            # mean of the (rounded) rates, half-up to 4 decimals: floor((2S + k) / 2k)
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$ma_cnt", 0]},
                {"$divide": [{"$floor": {"$divide": [{"$add": [{"$multiply": ["$ma_sum", 2]}, "$ma_cnt"]},
                                                     {"$multiply": ["$ma_cnt", 2]}]}}, 10000]},
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------------------
# 5. Endpoints
# --------------------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_ms_precision()
    try:
        employees.insert_one(doc)  # the unique index on emp_code decides, not a prior lookup
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    skip, limit = paginate_args(page, page_size)
    total = employees.count_documents(q)
    items = [employee_out(d) for d in employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(limit)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee(body.emp_code)
    punched = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    att_date = attendance_date(punched, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **compute_derived(body.status, punched, None, emp, att_date),
        "history": [],
    }
    try:
        # (emp_code, date) is unique: of two simultaneous requests exactly one insert succeeds.
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee(body.emp_code)
    punched = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    # Most recent record whose punch_in is at or before punched_at (works across midnight).
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = rec["punch_in"]
    if punched <= p_in:
        fail_422(("body", "punched_at"), "punched_at must be after punch_in")
    if (punched - p_in).total_seconds() > MAX_SHIFT_SECONDS:
        fail_422(("body", "punched_at"), "punched_at is more than 24 hours after punch_in")
    derived = compute_derived(rec["status"], p_in, punched, emp, rec["date"])
    # Compare-and-set: only succeeds while the record is still open and unchanged. Of two
    # simultaneous punch-outs exactly one matches.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": p_in},
        {"$set": {"punch_out": punched, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out or changed concurrently")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    d_from = parse_ymd(date_from, ("query", "date_from")) if date_from else None
    d_to = parse_ymd(date_to, ("query", "date_to")) if date_to else None
    if d_from and d_to and d_from > d_to:
        fail_422(("query", "date_from"), "date_from must not be after date_to")
    q = attendance_filter(emp_code, date_from, date_to, status)
    skip, limit = paginate_args(page, page_size)
    total = logs.count_documents(q) if q else logs.estimated_document_count()
    items = [record_out(d) for d in logs.find(q).sort(LOG_SORT).skip(skip).limit(limit)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def build_regularized(rec: dict, emp: dict, body: RegularizeRequest) -> tuple[dict, dict]:
    """Apply a correction to a copy of the record. Returns (new field values, changes dict)."""
    given = body.model_fields_set
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    status = body.status if "status" in given else old["status"]
    if status in OFF_STATUSES:
        if "punch_in" in given or "punch_out" in given:
            fail_422(("body",), "ABSENT/LEAVE clears punch times; punch_in/punch_out cannot be supplied")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if "punch_in" in given else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if "punch_out" in given else old["punch_out"]
        if new_in is None:
            fail_422(("body", "punch_in"), "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            fail_422(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                fail_422(("body", "punch_out"), "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > MAX_SHIFT_SECONDS:
                fail_422(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")
    new = {"status": status, "punch_in": new_in, "punch_out": new_out}
    new.update(compute_derived(status, new_in, new_out, emp, rec["date"]))
    if all(new[k] == old[k] for k in ("status", "punch_in", "punch_out")):
        fail_422(("body",), "the request changes nothing")
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    emp_code: str,
    body: RegularizeRequest,
    date: str = Path(pattern=DATE_RE),
):
    parse_ymd(date, ("path", "date"))
    emp = get_employee(emp_code)
    for _ in range(5):
        rec = logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = build_regularized(rec, emp, body)
        n_history = len(rec.get("history") or [])
        entry = {"at": now_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # Optimistic concurrency: the write only applies if nobody appended history (or
        # changed the punch fields) since we read the record. The loser re-reads and retries.
        updated = logs.find_one_and_update(
            {
                "_id": rec["_id"],
                "status": rec["status"],
                "punch_in": rec.get("punch_in"),
                "punch_out": rec.get("punch_out"),
                f"history.{n_history}": {"$exists": False},
            },
            {"$set": new, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record is being modified concurrently; retry")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)
    row = next(iter(logs.aggregate(monthly_pipeline(emp_code, first, last))), {})
    present_x2 = int(row.get("present_x2", 0))
    working_days = count_weekdays(max(first, emp["joined_on"]), last) if emp["joined_on"] <= last else 0
    pct = round_half_up(Fraction(present_x2, 2) / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_x2 / 2,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_bounds(month)
    pipeline, hint = dept_summary_query(first, last, department)
    items = []
    for r in employees.aggregate(pipeline, hint=hint):
        cnt = int(r["hours_cnt"])
        avg = round_half_up(Fraction(int(r["hours_x100"]), 100 * cnt), 2) if cnt else None
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": int(r["present_x2"]) / 2,
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["late_total"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = list(logs.aggregate(leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


def trend_args(from_: str, to: str) -> tuple[date_t, int]:
    d_from = parse_ymd(from_, ("query", "from"))
    d_to = parse_ymd(to, ("query", "to"))
    if d_to < d_from:
        fail_422(("query", "to"), "to must not be before from")
    days = (d_to - d_from).days + 1
    if days > MAX_TREND_DAYS:
        fail_422(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return d_from, days


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    start, days = trend_args(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(employees.aggregate(trend_pipeline(department, start, days), hint="dept_joined"))
    return {"department": department, "items": items}


EXPLAINABLE = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def require(value, name: str):
    if value is None or value == "":
        fail_422(("query", name), "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: EXPLAINABLE,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """executionStats explain of the endpoint's main query, built by the same builders."""
    if endpoint == "attendance_list":
        d_from = parse_ymd(date_from, ("query", "date_from")) if date_from else None
        d_to = parse_ymd(date_to, ("query", "date_to")) if date_to else None
        if d_from and d_to and d_from > d_to:
            fail_422(("query", "date_from"), "date_from must not be after date_to")
        skip, lim = paginate_args(page, page_size)
        find = {"find": logs.name, "filter": attendance_filter(emp_code, date_from, date_to, status),
                "sort": dict(LOG_SORT), "limit": lim}
        if skip:
            find["skip"] = skip
        coll, command = logs.name, find
    else:
        if endpoint == "department_trend":
            start, days = trend_args(require(from_, "from"), require(to, "to"))
            coll = employees.name
            command = {"aggregate": coll, "pipeline": trend_pipeline(require(department, "department"), start, days),
                       "cursor": {}, "hint": "dept_joined"}
        else:
            first, last = month_bounds(require(month, "month"))
            if endpoint == "employee_monthly":
                coll = logs.name
                command = {"aggregate": coll, "pipeline": monthly_pipeline(require(emp_code, "emp_code"), first, last), "cursor": {}}
            elif endpoint == "department_summary":
                pipeline, hint = dept_summary_query(first, last, department)
                coll = employees.name
                command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
            else:  # late_leaderboard
                coll = logs.name
                command = {"aggregate": coll, "pipeline": leaderboard_pipeline(first, last, limit, department), "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    # Explain output holds ObjectIds, Timestamps, Int64...: round-trip through extended JSON.
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(result))}
