"""Employee attendance and analytics API.

Run with ``uvicorn app.main:app --port 8000``. Configuration is read from
``MONGO_URI`` and ``MONGO_DB``; see ``openapi.yaml`` for the HTTP contract.
"""
import json
import os
from datetime import datetime, timedelta, timezone, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from bson import json_util
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_seconds(value: datetime) -> datetime:
    """Remove fractional seconds before storing or calculating."""
    return value.replace(microsecond=0)


def to_ist(value: datetime) -> datetime:
    """Convert a datetime to IST."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
) -> int:
    punch_in = to_ist(truncate_seconds(punch_in))
    h, m = map(int, shift_start.split(":"))

    start = punch_in.replace(
        hour=h, minute=m, second=0, microsecond=0
    )

    if shift_end is not None:
        end_h, end_m = map(int, shift_end.split(":"))
        if (end_h, end_m) <= (h, m):
            if punch_in.time() < time(end_h, end_m):
                start -= timedelta(days=1)
        elif punch_in.time() < time(h, m):
            start -= timedelta(days=1)
    elif punch_in.time() < time(h, m):
        start -= timedelta(days=1)

    seconds_late = (punch_in - start).total_seconds()

    # The grace period is 10 minutes, strictly greater than 10.
    if seconds_late <= 600:
        return 0

    return int(seconds_late // 60)


def attendance_date_for_punch_in(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    local_punch_in = to_ist(truncate_seconds(punch_in))
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    if (end_h, end_m) <= (start_h, start_m) and local_punch_in.time() < time(end_h, end_m):
        local_punch_in -= timedelta(days=1)
    return local_punch_in.date().isoformat()


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (
        truncate_seconds(punch_out) - truncate_seconds(punch_in)
    ).total_seconds()

    hours = Decimal(str(seconds)) / Decimal("3600")
    rounded = hours.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(rounded)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    punch_out = to_ist(truncate_seconds(punch_out))

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    attendance_date = datetime.fromisoformat(date_str).date()

    end_date = attendance_date
    if (end_h, end_m) <= (start_h, start_m):
        end_date += timedelta(days=1)

    shift_end_dt = datetime.combine(
        end_date,
        time(end_h, end_m),
        tzinfo=IST,
    )

    seconds = (punch_out - shift_end_dt).total_seconds()
    minutes = int(seconds // 60)

    # Overtime counts only when it reaches 30 whole minutes.
    return minutes if minutes >= 30 else 0


def epoch_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def attendance_response(doc: dict) -> dict:
    response = {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": epoch_millis(doc.get("punch_in")),
        "punch_out": epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    for entry in doc.get("history", []):
        serialized_entry = dict(entry)
        serialized_entry["at"] = epoch_millis(entry.get("at"))
        serialized_entry["changes"] = dict(entry.get("changes", {}))
        for field in ("punch_in", "punch_out"):
            if field in serialized_entry["changes"]:
                change = dict(serialized_entry["changes"][field])
                change["from"] = epoch_millis(change.get("from"))
                change["to"] = epoch_millis(change.get("to"))
                serialized_entry["changes"][field] = change
        response["history"].append(serialized_entry)
    return response


def month_bounds(month: str):
    try:
        start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    if start.strftime("%Y-%m") != month:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return start, next_month, next_month - timedelta(days=1)


def count_working_days(start, end) -> int:
    if start > end:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def round_half_up(value, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as error:
            raise ValueError("joined_on must be YYYY-MM-DD") from error
        if parsed.isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=100000000000, le=4102444800000
    )
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=100000000000, le=4102444800000
    )


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(
        default=None, ge=100000000000, le=4102444800000
    )
    punch_out: Optional[StrictInt] = Field(
        default=None, ge=100000000000, le=4102444800000
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = epoch_millis(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for employee in items:
        employee["created_at"] = epoch_millis(employee.get("created_at"))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="status must be PRESENT, WFH, or ON_DUTY",
        )

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)

    # Truncate to whole seconds and derive the attendance date in IST.
    ts = truncate_seconds(ts)
    local_ts = to_ist(ts)
    date_str = attendance_date_for_punch_in(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            local_ts,
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc.copy())
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    ts = truncate_seconds(ts)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        future_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": ts}},
            sort=[("punch_in", ASCENDING)],
        )
        if future_record:
            raise HTTPException(
                status_code=422,
                detail="punched_at must be after punch_in",
            )
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)
    if ts <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in",
        )
    if ts - punch_in_time > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punch_out must be within 24 hours of punch_in",
        )
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.5,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    parsed_dates = []
    for date_value in (date_from, date_to):
        if date_value is None:
            parsed_dates.append(None)
            continue
        try:
            parsed = datetime.strptime(date_value, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="dates must be YYYY-MM-DD")
        if parsed.isoformat() != date_value:
            raise HTTPException(status_code=422, detail="dates must be YYYY-MM-DD")
        parsed_dates.append(parsed)
    if parsed_dates[0] and parsed_dates[1] and parsed_dates[0] > parsed_dates[1]:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    q = {}
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
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [attendance_response(doc) for doc in docs]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        if datetime.strptime(date, "%Y-%m-%d").date().isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    employee = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not employee or not record:
        raise HTTPException(status_code=404, detail="Employee or attendance record not found")

    supplied = body.model_fields_set
    if "status" in supplied and body.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null")
    for field in ("punch_in", "punch_out"):
        if field in supplied and getattr(body, field) is None:
            raise HTTPException(status_code=422, detail=f"{field} must be epoch milliseconds")

    old = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    final = dict(old)

    if "status" in supplied:
        final["status"] = body.status
    for field in ("punch_in", "punch_out"):
        if field in supplied:
            final[field] = datetime.fromtimestamp(
                getattr(body, field) // 1000,
                tz=timezone.utc,
            )

    if final["status"] in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="punch times cannot be supplied for ABSENT or LEAVE",
            )
        final["punch_in"] = None
        final["punch_out"] = None
    elif final["punch_in"] is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    if final["punch_in"] is not None:
        final["punch_in"] = truncate_seconds(final["punch_in"])
        if attendance_date_for_punch_in(
            final["punch_in"],
            employee["shift_start"],
            employee["shift_end"],
        ) != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

    if final["punch_out"] is not None:
        final["punch_out"] = truncate_seconds(final["punch_out"])
        if final["punch_in"] is None or final["punch_out"] <= final["punch_in"]:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )
        if final["punch_out"] - final["punch_in"] > timedelta(hours=24):
            raise HTTPException(
                status_code=422,
                detail="punch_out must be within 24 hours of punch_in",
            )

    final["late_minutes"] = (
        compute_late_minutes(
            final["punch_in"],
            employee["shift_start"],
            employee["shift_end"],
        )
        if final["punch_in"] is not None
        else 0
    )
    if final["punch_in"] is not None and final["punch_out"] is not None:
        final["work_hours"] = compute_work_hours(final["punch_in"], final["punch_out"])
        final["overtime_minutes"] = compute_overtime(
            final["punch_out"],
            employee["shift_start"],
            employee["shift_end"],
            date,
        )
        final["half_day"] = final["work_hours"] < 4.5
    else:
        final["work_hours"] = None
        final["overtime_minutes"] = 0
        final["half_day"] = False

    changes = {
        field: {"from": old[field], "to": final[field]}
        for field in final
        if old[field] != final[field]
    }
    if not changes:
        raise HTTPException(status_code=422, detail="request does not change the record")

    now = datetime.now(timezone.utc)
    history = list(record.get("history", []))
    history.append(
        {
            "at": now,
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }
    )
    update_filter = {"emp_code": emp_code, "date": date}
    update_filter["history"] = record["history"] if "history" in record else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                **final,
                "history": history,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently")
    return attendance_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, next_month, month_end = month_bounds(month)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()},
            }
        },
        {
            "$addFields": {
                "_attendance_day": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_overtime": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$_attendance_day", 2]},
                                    {"$lte": ["$_attendance_day", 6]},
                                ]
                            },
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$_late"},
                "total_overtime_minutes": {"$sum": "$_overtime"},
            }
        },
    ]
    totals = next(db.attendance_logs.aggregate(pipeline), {})
    working_start = max(start, datetime.fromisoformat(employee["joined_on"]).date())
    working_days = count_working_days(working_start, month_end)
    present_days = totals.get("present_days", 0)
    attendance_pct = (
        round_half_up(Decimal(str(present_days)) * 100 / working_days, 4)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, next_month, month_end = month_bounds(month)
    employee_filter = {"joined_on": {"$lte": month_end.isoformat()}}
    if department:
        employee_filter["department"] = department

    pipeline = [
        {"$match": employee_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                ]
                            }
                        }
                    },
                    {"$project": {"date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "_attendance_day": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
                "_late": {"$ifNull": ["$logs.late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$_attendance_day", 2]},
                                    {"$lte": ["$_attendance_day", 6]},
                                ]
                            },
                            {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$_late"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 100]}, 0.5]}},
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, next_month, _ = month_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)):
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="from and to must be YYYY-MM-DD")
    if start.isoformat() != from_date or end.isoformat() != to:
        raise HTTPException(status_code=422, detail="from and to must be YYYY-MM-DD")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range must be ordered and at most 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Department not found")

    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    pipeline = [
        {"$documents": [{"date": start_dt}]},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {"$match": {"date": {"$lt": end_exclusive}}},
        {
            "$addFields": {
                "_date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "_day_of_week": {"$dayOfWeek": "$date"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$_date_string", "department": department},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$project": {
                            "status": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$_date_string"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "headcount"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.headcount", 0]}, 0]},
                "present_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1]},
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "is_working_day": {"$and": [{"$gte": ["$_day_of_week", 2]}, {"$lte": ["$_day_of_week", 6]}]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}},
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$_moving_avg", None]},
                        {"$divide": [{"$floor": {"$add": [{"$multiply": ["$_moving_avg", 10000]}, 0.5]}}, 10000]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    items = list(db.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        parsed_dates = []
        for date_value in (date_from, date_to):
            if date_value is None:
                parsed_dates.append(None)
                continue
            try:
                parsed = datetime.strptime(date_value, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=422, detail="dates must be YYYY-MM-DD")
            if parsed.isoformat() != date_value:
                raise HTTPException(status_code=422, detail="dates must be YYYY-MM-DD")
            parsed_dates.append(parsed)
        if parsed_dates[0] and parsed_dates[1] and parsed_dates[0] > parsed_dates[1]:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "projection": {"_id": 0},
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start, next_month, _ = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}},
            {"$addFields": {"_attendance_day": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, "_late": {"$ifNull": ["$late_minutes", 0]}, "_overtime": {"$ifNull": ["$overtime_minutes", 0]}}},
            {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$gte": ["$_attendance_day", 2]}, {"$lte": ["$_attendance_day", 6]}]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$_late"}, "total_overtime_minutes": {"$sum": "$_overtime"}}},
        ]
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, next_month, month_end = month_bounds(month)
        match = {"joined_on": {"$lte": month_end.isoformat()}}
        if department:
            match["department"] = department
        pipeline = [
            {"$match": match},
            {"$lookup": {"from": "attendance_logs", "let": {"employee_code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$employee_code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lt": ["$date", next_month.isoformat()]}]}}}, {"$project": {"date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}}], "as": "logs"}},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$addFields": {"_attendance_day": {"$cond": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, None]}, "_late": {"$ifNull": ["$logs.late_minutes", 0]}}},
            {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"}, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$gte": ["$_attendance_day", 2]}, {"$lte": ["$_attendance_day", 6]}]}, {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}}, "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}}, "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$_late"}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
            {"$group": {"_id": "$_id.department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"}, "work_hours_sum": {"$sum": "$work_hours_sum"}, "work_hours_count": {"$sum": "$work_hours_count"}, "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"}, "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"}}},
            {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 100]}, 0.5]}}, 100]}, None]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
            {"$sort": {"department": 1}},
        ]
        command = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, next_month, _ = month_bounds(month)
        match = {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}, "late_minutes": {"$gt": 0}}
        pipeline = [
            {"$match": match},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
        ]
        if department:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            start = datetime.strptime(from_date, "%Y-%m-%d").date()
            end = datetime.strptime(to, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="from and to must be YYYY-MM-DD")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="date range must be ordered and at most 92 days")
        pipeline = [
            {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        ]
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        collection = "attendance_logs"

    explanation = db.command(
        "explain",
        command,
        verbosity="executionStats",
    )
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explanation)),
    }


