"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
import json
from bson import json_util
from datetime import datetime, timedelta, timezone
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo.errors import DuplicateKeyError, PyMongoError
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True
    )
    db.attendance_logs.create_index([
        ("date", -1),
        ("emp_code", 1)
    ])
    db.attendance_logs.create_index([
        ("date", 1),
        ("late_minutes", 1)
    ])
    yield

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None) -> int:
    h, m = map(int, shift_start.split(":"))
    if punch_in.tzinfo is None:
        punch_in_ist = punch_in.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        punch_in_ist = punch_in.astimezone(IST)

    if date_str:
        shift_start_dt = datetime.fromisoformat(date_str).replace(
            hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
        )
    else:
        shift_start_dt = punch_in_ist.replace(
            hour=h, minute=m, second=0, microsecond=0
        )

    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    raw_seconds = (punch_out - punch_in).total_seconds()
    raw_hours = Decimal(str(raw_seconds)) / Decimal("3600")
    return float(raw_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    if punch_out.tzinfo is None:
        punch_out_ist = punch_out.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        punch_out_ist = punch_out.astimezone(IST)

    h, m = map(int, shift_end.split(":"))
    shift_end_dt = datetime.fromisoformat(date_str).replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )
    if shift_end <= shift_start:
        shift_end_dt += timedelta(days=1)

    overtime = int((punch_out_ist - shift_end_dt).total_seconds() // 60)
    return overtime if overtime >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            parsed = datetime.strptime(v, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != v:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @field_validator("shift_end")
    @classmethod
    def validate_shift_diff(cls, v: str, info) -> str:
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start must differ from shift_end")
        return v



class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = int(created_at_dt.timestamp() * 1000)
    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department:
        query["department"] = department.strip()

    total = db.employees.count_documents(query)

    skip = (page - 1) * page_size

    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    items = list(cursor)

    for employee in items:
        created_at = employee.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            employee["created_at"] = int(created_at.timestamp() * 1000)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "invalid attendance status")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        if (
            type(body.punched_at) is not int
            or not 100000000000 <= body.punched_at <= 4102444800000
        ):
            raise HTTPException(422, "punched_at must be epoch milliseconds")

        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        ).replace(microsecond=0)

    ts_ist = ts.astimezone(IST)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    punch_time = ts_ist.strftime("%H:%M")

    overnight = shift_end <= shift_start
    if overnight and punch_time < shift_end:
        attendance_date = (ts_ist.date() - timedelta(days=1)).isoformat()
    else:
        attendance_date = ts_ist.date().isoformat()

    late_minutes = compute_late_minutes(ts_ist, shift_start, attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": int(ts.timestamp() * 1000),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        if (
            type(body.punched_at) is not int
            or not 100000000000 <= body.punched_at <= 4102444800000
        ):
            raise HTTPException(422, "punched_at must be epoch milliseconds")

        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        ).replace(microsecond=0)

    # MongoDB stores these timestamps as naive UTC datetimes.
    ts_db = ts.replace(tzinfo=None)

    # Find the most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts_db},
            "punch_out": None,
        },
        sort=[("punch_in", -1), ("date", -1)],
    )

    if record is None:
        future_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$gt": ts_db},
            },
            sort=[("punch_in", 1)],
        )
        if future_record is not None:
            raise HTTPException(422, "punch-out must be after punch-in")

        already_out = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": ts_db},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", -1), ("date", -1)],
        )
        if already_out is not None:
            raise HTTPException(409, "already punched out")

        raise HTTPException(404, "attendance record not found")

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is not None:
        punch_in_time = punch_in_time.astimezone(
            timezone.utc
        ).replace(tzinfo=None)

    duration_seconds = (ts_db - punch_in_time).total_seconds()
    if duration_seconds <= 0:
        raise HTTPException(422, "punch-out must be after punch-in")

    if duration_seconds > 24 * 3600:
        raise HTTPException(422, "punch-out cannot be more than 24 hours after punch-in")

    # Calculate work duration and round to 2 decimal places using ROUND_HALF_UP
    work_hours = compute_work_hours(punch_in_time, ts_db)

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    overtime_minutes = compute_overtime(ts_db, shift_end, record["date"], shift_start)

    half_day = work_hours < 4.5

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts_db,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "already punched out")

    return {
        "emp_code": record["emp_code"],
        "date": record["date"],
        "status": record["status"],
        "punch_in": int(
            punch_in_time.replace(tzinfo=timezone.utc).timestamp() * 1000
        ),
        "punch_out": int(ts.timestamp() * 1000),
        "work_hours": work_hours,
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": record.get("history", []),
    }


class RegularizationIn(BaseModel):
    regularized_by: str = Field(..., min_length=1, max_length=50)
    reason: str = Field(..., min_length=5, max_length=200)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizationIn):
    from datetime import date as date_type

    try:
        parsed_d = date_type.fromisoformat(date)
        if parsed_d.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    if not body.regularized_by.strip() or not body.reason.strip():
        raise HTTPException(422, "regularized_by and reason are required")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    updates = {}
    changes = {}

    if body.status is not None:
        if body.status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise HTTPException(422, "invalid attendance status")
        updates["status"] = body.status

    for field in ("punch_in", "punch_out"):
        value = getattr(body, field)
        if value is not None:
            if type(value) is not int or not 100000000000 <= value <= 4102444800000:
                raise HTTPException(422, f"{field} must be epoch milliseconds")
            updates[field] = datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0).replace(tzinfo=None)

    new_status = updates.get("status", record["status"])
    new_in = updates.get("punch_in", record.get("punch_in"))
    new_out = updates.get("punch_out", record.get("punch_out"))

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch_in and punch_out cannot be supplied with ABSENT or LEAVE")
        new_in = None
        new_out = None
        updates["punch_in"] = None
        updates["punch_out"] = None
        updates["work_hours"] = None
        updates["late_minutes"] = 0
        updates["overtime_minutes"] = 0
        updates["half_day"] = False
    else:
        if new_in is None:
            raise HTTPException(422, "punch_in is required for presence statuses")

        # Validate punch_in stays on record's attendance date per R1
        new_in_ist = new_in.replace(tzinfo=timezone.utc).astimezone(IST)
        shift_start = emp["shift_start"]
        shift_end = emp["shift_end"]
        punch_time_ist = new_in_ist.strftime("%H:%M")

        overnight = shift_end <= shift_start
        if overnight and punch_time_ist < shift_end:
            computed_date = (new_in_ist.date() - timedelta(days=1)).isoformat()
        else:
            computed_date = new_in_ist.date().isoformat()

        if computed_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

        updates["late_minutes"] = compute_late_minutes(new_in_ist, shift_start, date)

        if new_out is not None:
            updates["work_hours"] = compute_work_hours(new_in, new_out)
            updates["overtime_minutes"] = compute_overtime(new_out, shift_end, date, shift_start)
            updates["half_day"] = updates["work_hours"] < 4.5
        else:
            updates["work_hours"] = None
            updates["overtime_minutes"] = 0
            updates["half_day"] = False

    for field, new_value in updates.items():
        old_value = record.get(field)
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, "no changes supplied")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0).replace(tzinfo=None),
        "by": body.regularized_by.strip(),
        "reason": body.reason.strip(),
        "changes": changes,
    }

    # Concurrent edits guard: ensure history length hasn't changed
    curr_hist_len = len(record.get("history", []))
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            f"history.{curr_hist_len}": {"$exists": False},
        },
        {
            "$set": updates,
            "$push": {"history": history_entry},
        },
    )
    if result.modified_count != 1:
        raise HTTPException(409, "concurrent modification conflict")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})

    def epoch_ms(value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)

    history = []
    for entry in updated.get("history", []):
        history_changes = {}
        for field, pair in entry.get("changes", {}).items():
            history_changes[field] = {
                "from": epoch_ms(pair["from"]) if field in {"punch_in", "punch_out"} and pair["from"] is not None else pair["from"],
                "to": epoch_ms(pair["to"]) if field in {"punch_in", "punch_out"} and pair["to"] is not None else pair["to"],
            }
        history.append({
            "at": epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": history_changes,
        })

    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": epoch_ms(updated.get("punch_in")),
        "punch_out": epoch_ms(updated.get("punch_out")),
        "work_hours": updated.get("work_hours"),
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated.get("overtime_minutes", 0),
        "half_day": updated.get("half_day", False),
        "history": history,
    }


def to_epoch_ms(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return int(value.timestamp() * 1000)


def attendance_response(doc):
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, pair in entry.get("changes", {}).items():
            old_value = pair.get("from")
            new_value = pair.get("to")
            if field in {"punch_in", "punch_out"}:
                old_value = to_epoch_ms(old_value)
                new_value = to_epoch_ms(new_value)
            changes[field] = {"from": old_value, "to": new_value}
        history.append({
            "at": to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history
    }



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    from datetime import date as date_type

    for value, name in ((date_from, "date_from"), (date_to, "date_to")):
        if value is not None:
            try:
                parsed = date_type.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(422, f"{name} must be YYYY-MM-DD")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be before or equal to date_to")

    if status and status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "invalid attendance status")

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

    total = db.attendance_logs.count_documents(query)

    docs = db.attendance_logs.find(query, {"_id": 0}).sort([
        ("date", -1),
        ("emp_code", 1)
    ]).skip((page - 1) * page_size).limit(page_size)

    return {
        "items": [attendance_response(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size
    }



from calendar import monthrange
from decimal import Decimal, ROUND_HALF_UP
import re


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    year, month_number = map(int, month.split("-"))
    month_start = f"{month}-01"
    last_day = monthrange(year, month_number)[1]
    month_end = f"{month}-{last_day:02d}"

    joined_on = emp["joined_on"]
    working_start = max(joined_on, month_start)

    working_days = 0
    if working_start <= month_end:
        from datetime import date, timedelta

        current = date.fromisoformat(working_start)
        end_date = date.fromisoformat(month_end)

        while current <= end_date:
            if current.weekday() < 5:
                working_days += 1
            current += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))
    summary = results[0] if results else {}

    present_days = summary.get("present_days", 0)
    attendance_pct = None

    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(present_days)) * Decimal("100") / Decimal(working_days))
            .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


def build_employee_monthly_pipeline(emp_code: str, month: str):
    year, month_number = map(int, month.split("-"))
    month_start = f"{month}-01"
    last_day = monthrange(year, month_number)[1]
    month_end = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start,
                    "$lte": month_end,
                },
            }
        },
        {
            "$addFields": {
                "attendance_date": {
                    "$dateFromString": {"dateString": "$date"}
                },
                "late_value": {"$ifNull": ["$late_minutes", 0]},
                "overtime_value": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$addFields": {
                "is_weekday": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$attendance_date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$attendance_date"}, 6]},
                    ]
                }
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
                                    "$is_weekday",
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_value", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_value"},
                "total_overtime_minutes": {"$sum": "$overtime_value"},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    month_start = f"{month}-01"
    year, month_number = map(int, month.split("-"))

    if month_number == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month_number + 1:02d}-01"

    month_end = (
        datetime.strptime(next_month, "%Y-%m-%d") - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    employee_filter = {"joined_on": {"$lte": month_end}}

    if department:
        employee_filter["department"] = department

    return [
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
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "attendance",
            }
        },
        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$attendance.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$attendance.date", None]},
                                    {
                                        "$gte": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$attendance.date"}}},
                                            2,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$attendance.date"}}},
                                            6,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$attendance.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$attendance.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$attendance.work_hours", None]},
                                ]
                            },
                            "$attendance.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$attendance.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$attendance.late_minutes", 0]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employee_codes"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$ne": ["$avg_work_hours", None]},
                        {"$round": ["$avg_work_hours", 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must be in YYYY-MM format")

    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    year, month_number = map(int, month.split("-"))
    month_start = f"{month}-01"

    if month_number == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month_number + 1:02d}-01"

    month_end = (
        datetime.strptime(next_month, "%Y-%m-%d") - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "late_count": {"$sum": 1},
                "total_late_minutes": {"$sum": "$late_minutes"},
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
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {
                        "total_late_minutes": -1
                    },
                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    }
                }
            },
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "rank": 1,
                }
            },
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(pipeline)),
    }


def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    start = datetime.strptime(from_date, "%Y-%m-%d").date()
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    start_datetime = datetime.combine(start, datetime.min.time())
    end_datetime = datetime.combine(end, datetime.min.time())

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "_employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$set": {
                "_start": {"$literal": start_datetime},
                "_end": {"$literal": end_datetime},
            }
        },
        {
            "$set": {
                "_dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$_start",
                                                "endDate": "$_end",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$_start",
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_dates"},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_dates",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_dates"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_dates"}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "_eligible": {
                    "$filter": {
                        "input": "$_employees",
                        "as": "employee",
                        "cond": {
                            "$lte": ["$$employee.joined_on", "$date"]
                        },
                    }
                }
            }
        },
        {
            "$set": {
                "_codes": {
                    "$map": {
                        "input": "$_eligible",
                        "as": "employee",
                        "in": "$$employee.emp_code",
                    }
                },
                "headcount": {"$size": "$_eligible"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date", "codes": "$_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "_daily",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_daily.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_daily.late_count", 0]},
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_dates": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_dates",
                    }
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"_dates": 1}},
        {
            "$group": {
                "_id": None,
                "items": {
                    "$push": {
                        "date": "$date",
                        "is_working_day": "$is_working_day",
                        "headcount": "$headcount",
                        "present_count": "$present_count",
                        "late_count": "$late_count",
                        "attendance_rate": "$attendance_rate",
                        "moving_avg_7d": "$moving_avg_7d",
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": {"$literal": department},
                "from": {"$literal": from_date},
                "to": {"$literal": to_date},
                "items": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    for label, value in (("from", from_date), ("to", to_date)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise HTTPException(422, f"'{label}' must use YYYY-MM-DD format")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(422, f"'{label}' must be a valid calendar date")

    start = datetime.strptime(from_date, "%Y-%m-%d").date()
    end = datetime.strptime(to_date, "%Y-%m-%d").date()

    if start > end:
        raise HTTPException(422, "'from' must be on or before 'to'")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.employees.aggregate(pipeline))

    if not results:
        raise HTTPException(404, "department trend not found")

    return results[0]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Unknown endpoint",
                "supported_endpoints": valid_endpoints,
            },
        )

    if endpoint == "attendance_list":
        for val, name in ((date_from, "date_from"), (date_to, "date_to")):
            if val is not None:
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(422, f"{name} must be YYYY-MM-DD")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be before or equal to date_to")
        if status and status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise HTTPException(422, "invalid attendance status")

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

        skip = (page - 1) * page_size
        collection = db.attendance_logs
        explain_result = collection.database.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": [("date", -1), ("emp_code", 1)],
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_result)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly explain")
        if not month or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(422, "month must be YYYY-MM")

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        collection = db.attendance_logs
        explain_result = collection.database.command(
            "explain",
            {
                "aggregate": collection.name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_result)),
        }

    elif endpoint == "department_summary":
        if not month or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(422, "month must be YYYY-MM")

        pipeline = build_department_summary_pipeline(month, department)
        collection = db.employees
        explain_result = collection.database.command(
            "explain",
            {
                "aggregate": collection.name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(explain_result)),
        }

    elif endpoint == "late_leaderboard":
        if not month or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(422, "month must be YYYY-MM")

        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        collection = db.attendance_logs
        explain_result = collection.database.command(
            "explain",
            {
                "aggregate": collection.name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_result)),
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required")
        if not from_date or not to_date:
            raise HTTPException(422, "'from' and 'to' are required")

        for label, val in (("from", from_date), ("to", to_date)):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
                raise HTTPException(422, f"'{label}' must use YYYY-MM-DD format")
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(422, f"'{label}' must be a valid calendar date")

        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
        if start > end:
            raise HTTPException(422, "'from' must be on or before 'to'")
        if (end - start).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        collection = db.employees
        explain_result = collection.database.command(
            "explain",
            {
                "aggregate": collection.name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(explain_result)),
        }

