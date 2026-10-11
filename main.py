"""Employee Attendance & Analytics API. See openapi.yaml for the HTTP contract."""
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)], name="joined_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="department_joined_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_employee_date_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punchin")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_late")


@app.on_event("startup")
def startup():
    try:
        ensure_indexes()
    except PyMongoError:
        # /health exposes connectivity status; indexes are retried on service restart.
        pass


@app.on_event("shutdown")
def shutdown():
    client.close()


def utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).astimezone(UTC)


def trunc(value: datetime) -> datetime:
    return utc(value).replace(microsecond=0)


def epoch_datetime(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    delta = utc(value) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def round_half_up(value: Optional[float], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def clock(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def attendance_day(punch: datetime, employee: dict) -> date:
    local = trunc(punch).astimezone(IST)
    start, end = clock(employee["shift_start"]), clock(employee["shift_end"])
    day = local.date()
    if end <= start and local.timetz().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    return day


def late_minutes(punch: datetime, shift_start: str, day: date) -> int:
    start = datetime.combine(day, clock(shift_start), tzinfo=IST)
    elapsed = int((trunc(punch).astimezone(IST) - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def shift_end(employee: dict, day: date) -> datetime:
    end = clock(employee["shift_end"])
    if end <= clock(employee["shift_start"]):
        day += timedelta(days=1)
    return datetime.combine(day, end, tzinfo=IST)


def work_hours(start: datetime, end: datetime) -> float:
    seconds = Decimal(str((trunc(end) - trunc(start)).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def overtime_minutes(punch: datetime, employee: dict, day: date) -> int:
    seconds = int((trunc(punch) - shift_end(employee, day).astimezone(UTC)).total_seconds())
    minutes = max(0, seconds // 60)
    return minutes if minutes >= 30 else 0


def month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(MONTH_RE, month):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    if y > 9998:
        raise HTTPException(422, "month out of range")
    next_month = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, 1).isoformat(), next_month.isoformat()


def weekdays(start: date, end: date) -> int:
    if start > end:
        return 0
    n = (end - start).days + 1
    weeks, rem = divmod(n, 7)
    return weeks * 5 + sum((start.weekday() + i) % 7 < 5 for i in range(rem))


def attendance_json(doc: dict) -> dict:
    history = []
    for entry in doc.get("history", []):
        changes = {k: dict(v) for k, v in (entry.get("changes") or {}).items()}
        for key in ("punch_in", "punch_out"):
            if key in changes:
                changes[key]["from"] = epoch_ms(changes[key].get("from"))
                changes[key]["to"] = epoch_ms(changes[key].get("to"))
        history.append({"at": epoch_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes})
    return {"emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
            "punch_in": epoch_ms(doc.get("punch_in")), "punch_out": epoch_ms(doc.get("punch_out")),
            "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
            "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False), "history": history}


def employee_json(doc: dict) -> dict:
    fields = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    return {**{f: doc[f] for f in fields}, "created_at": epoch_ms(doc["created_at"])}


def date_filter(date_from: Optional[date], date_to: Optional[date]) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = {}
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    return query


def employee_monthly_pipeline(code: str, start: str, next_month: str) -> list:
    return [
        {"$match": {"emp_code": code, "date": {"$gte": start, "$lt": next_month}}},
        {"$set": {"wd": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lte": ["$wd", 5]}]}, {"$cond": [{"$eq": ["$half_day", True]}, .5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_pipeline(start: str, next_month: str, department: Optional[str]) -> list:
    match = {"joined_on": {"$lt": next_month}}
    if department is not None:
        match["department"] = department
    per_employee = [
        {"$match": {"date": {"$gte": start, "$lt": next_month}, "$expr": {"$eq": ["$emp_code", "$$code"]}}},
        {"$set": {"wd": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lte": ["$wd", 5]}]}, {"$cond": [{"$eq": ["$half_day", True]}, .5, 1]}, 0]}},
            "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
            "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": per_employee, "as": "m"}},
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            **{key: {"$sum": {"$ifNull": [f"$m.{key}", 0]}} for key in ("present_days", "hours_sum", "hours_count", "late_count", "total_late_minutes", "leave_count", "on_duty_count")}}},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(start: str, next_month: str, limit: int, department: Optional[str]) -> list:
    p = [
        {"$match": {"date": {"$gte": start, "$lt": next_month}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},
    ]
    if department is not None:
        p.append({"$match": {"e.department": department}})
    p.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department", "total_late_minutes": 1, "late_count": 1}},
    ])
    return p


def trend_pipeline(department: str, start: str, end: str) -> list:
    start_dt = {"$dateFromString": {"dateString": start, "timezone": "Asia/Kolkata"}}
    end_dt = {"$dateFromString": {"dateString": end, "timezone": "Asia/Kolkata"}}
    date_expr = {"$dateToString": {"date": "$days", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"code": "$emp_code", "joined": "$joined_on"}}}},
        {"$set": {"days": {"$map": {"input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start_dt, "endDate": end_dt, "unit": "day"}}, 1]}]}, "as": "i", "in": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$days"},
        {"$set": {"date": date_expr,
            "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined", date_expr]}}}},
            "codes": {"$map": {"input": "$employees", "as": "e", "in": "$$e.code"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": {"date": "$days", "timezone": "Asia/Kolkata"}}, 5]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date", "codes": "$codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, .5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        ], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"days": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"days": 1}}, {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EpochMillis(int):
    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        from pydantic_core import core_schema
        return core_schema.int_schema(strict=True, ge=MIN_MS, le=MAX_MS)


class EmployeeIn(Model):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=TIME_RE)] = "09:30"
    shift_end: Annotated[str, Field(pattern=TIME_RE)] = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_date(cls, value):
        date.fromisoformat(value)
        if len(value) != 10:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @field_validator("shift_end")
    @classmethod
    def shift_differs(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("shift times must differ")
        return value


class PunchInIn(Model):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(Model):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(Model):
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_json(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_json(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched = trunc(epoch_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    day = attendance_day(punched, employee)
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status, "punch_in": punched,
           "punch_out": None, "work_hours": None, "late_minutes": late_minutes(punched, employee["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched = trunc(epoch_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched}}, sort=[("punch_in", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    elapsed = (punched - trunc(record["punch_in"])).total_seconds()
    if elapsed <= 0 or elapsed > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    day = date.fromisoformat(record["date"])
    hours = work_hours(record["punch_in"], punched)
    update = {"punch_out": punched, "work_hours": hours, "overtime_minutes": overtime_minutes(punched, employee, day), "half_day": hours < 4.5}
    updated = db.attendance_logs.find_one_and_update({"_id": record["_id"], "punch_out": None}, {"$set": update}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return attendance_json(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    query = date_filter(date_from, date_to)
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_json(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, record_date: Annotated[date, Path(alias="date")], body: RegularizeIn):
    day_text = record_date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day_text})
    if employee is None or record is None:
        raise HTTPException(404, "employee or attendance record not found")
    supplied = body.model_fields_set - {"reason", "regularized_by"}
    if not supplied:
        raise HTTPException(422, "request must change at least one field")
    old_status = record["status"]
    status = body.status if "status" in supplied else old_status
    old_in = trunc(record["punch_in"]) if record.get("punch_in") else None
    old_out = trunc(record["punch_out"]) if record.get("punch_out") else None
    punch_in = trunc(epoch_datetime(body.punch_in)) if "punch_in" in supplied else old_in
    punch_out = trunc(epoch_datetime(body.punch_out)) if "punch_out" in supplied else old_out
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        punch_in = punch_out = None
    else:
        if punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_day(punch_in, employee).isoformat() != day_text:
            raise HTTPException(422, "punch_in must belong to record date")
        if punch_out is not None and ((punch_out - punch_in).total_seconds() <= 0 or (punch_out - punch_in).total_seconds() > 86400):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = late_minutes(punch_in, employee["shift_start"], record_date) if punch_in else 0
    hours = work_hours(punch_in, punch_out) if punch_in and punch_out else None
    overtime = overtime_minutes(punch_out, employee, record_date) if punch_in and punch_out else 0
    half = hours < 4.5 if hours is not None else False
    old = {"status": old_status, "punch_in": old_in, "punch_out": old_out, "work_hours": record.get("work_hours"),
           "late_minutes": record.get("late_minutes", 0), "overtime_minutes": record.get("overtime_minutes", 0), "half_day": record.get("half_day", False)}
    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changes = {key: {"from": old[key], "to": value} for key, value in new.items() if old[key] != value}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    condition = {"_id": record["_id"], "status": old_status,
                 "punch_in": record.get("punch_in") if record.get("punch_in") is not None else {"$in": [None]},
                 "punch_out": record.get("punch_out") if record.get("punch_out") is not None else {"$in": [None]},
                 "history": record["history"] if "history" in record else {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update(condition, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return attendance_json(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_RE)]):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    start, next_month = month_bounds(month)
    last = date.fromisoformat(next_month) - timedelta(days=1)
    workdays = weekdays(max(date.fromisoformat(start), date.fromisoformat(employee["joined_on"])), last)
    result = next(db.attendance_logs.aggregate(
        employee_monthly_pipeline(emp_code, start, next_month), hint="attendance_employee_date_unique"
    ), {})
    present = result.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": workdays, "present_days": round_half_up(present),
            "leave_days": result.get("leave_days", 0), "late_count": result.get("late_count", 0),
            "total_late_minutes": result.get("total_late_minutes", 0), "total_overtime_minutes": result.get("total_overtime_minutes", 0),
            "attendance_pct": round_half_up(present / workdays * 100) if workdays else None}


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_RE)], department: Optional[str] = None):
    start, nxt = month_bounds(month)
    items = []
    for row in db.employees.aggregate(
        department_pipeline(start, nxt, department), hint="joined_department_code"
    ):
        hc = row.get("hours_count", 0)
        items.append({"department": row["_id"], "headcount": row["headcount"], "present_days": round_half_up(row.get("present_days", 0)),
            "avg_work_hours": round_half_up(row["hours_sum"] / hc) if hc else None, "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "leave_count": row.get("leave_count", 0), "on_duty_count": row.get("on_duty_count", 0)})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Annotated[str, Query(pattern=MONTH_RE)], limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    start, nxt = month_bounds(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(
        leaderboard_pipeline(start, nxt, limit, department), hint="attendance_date_late"
    ))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, start_date: Annotated[date, Query(alias="from")], to: date):
    if to < start_date or (to - start_date).days + 1 > 92:
        raise HTTPException(422, "invalid date range")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    rows = db.employees.aggregate(
        trend_pipeline(department, start_date.isoformat(), to.isoformat()), hint="department_joined_code"
    )
    items = [{"date": r["date"], "is_working_day": r["is_working_day"], "headcount": r["headcount"],
        "present_count": round_half_up(r["present_count"]), "late_count": r["late_count"],
        "attendance_rate": round_half_up(r["attendance_rate"], 4), "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4)} for r in rows]
    return {"department": department, "items": items}


def missing(field: str):
    raise HTTPException(422, detail=[{"loc": ["query", field], "msg": "Field required", "type": "missing"}])


def explain_aggregate(collection, pipeline, hint):
    return collection.database.command("explain", {
        "aggregate": collection.name, "pipeline": pipeline, "cursor": {}, "hint": hint
    }, verbosity="executionStats")


def safe_json(value):
    return json_util.loads(json_util.dumps(value))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10, date_from: Optional[date] = None, date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    start_date: Optional[date] = Query(default=None, alias="from"), to: Optional[date] = None):
    if endpoint == "attendance_list":
        query = date_filter(date_from, date_to)
        if emp_code is not None: query["emp_code"] = emp_code
        if status is not None: query["status"] = status
        explain = db.command("explain", {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size, "hint": "attendance_date_code"}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None: missing("emp_code")
        if month is None: missing("month")
        a, b = month_bounds(month)
        explain, collection = explain_aggregate(db.attendance_logs, employee_monthly_pipeline(emp_code, a, b), "attendance_employee_date_unique"), "attendance_logs"
    elif endpoint == "department_summary":
        if month is None: missing("month")
        a, b = month_bounds(month)
        explain, collection = explain_aggregate(db.employees, department_pipeline(a, b, department), "joined_department_code"), "employees"
    elif endpoint == "late_leaderboard":
        if month is None: missing("month")
        a, b = month_bounds(month)
        explain, collection = explain_aggregate(db.attendance_logs, leaderboard_pipeline(a, b, limit, department), "attendance_date_late"), "attendance_logs"
    else:
        if department is None: missing("department")
        if start_date is None: missing("from")
        if to is None: missing("to")
        if to < start_date or (to-start_date).days + 1 > 92: raise HTTPException(422, "invalid date range")
        explain, collection = explain_aggregate(db.employees, trend_pipeline(department, start_date.isoformat(), to.isoformat()), "department_joined_code"), "employees"
    return {"endpoint": endpoint, "collection": collection, "explain": safe_json(explain)}
