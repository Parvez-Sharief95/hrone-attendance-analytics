"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

logger = logging.getLogger("app.main")

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

# Epoch-millisecond range enforced by openapi EpochMillis schema.
# Values below 1e11 look like seconds and are rejected with 422.
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


# --------------------------------------------------------------------------- #
# Startup: idempotent index creation
# --------------------------------------------------------------------------- #
def _sanitize_error_message(msg: str) -> str:
    """Strip passwords and credentials from error strings to prevent secret leakage."""
    sanitized = re.sub(r"://([^:@]+):([^@]+)@", r"://\1:***@", msg)
    sanitized = re.sub(r"://:?([^@]+)@", r"://***@", sanitized)
    mongo_uri = os.getenv("MONGO_URI", "")
    if "@" in mongo_uri:
        raw_redacted = re.sub(r"://([^:@]+):([^@]+)@", r"://\1:***@", mongo_uri)
        sanitized = sanitized.replace(mongo_uri, raw_redacted)
    return sanitized


def _create_indexes() -> None:
    """Create required indexes idempotently. Safe to call on every restart."""
    try:
        # I1: unique emp_code — concurrent-safe 409 for duplicate employees
        db.employees.create_index("emp_code", unique=True, background=True)
        # I2: unique (emp_code, date) — concurrent-safe 409 for duplicate punch-ins
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            background=True,
        )
        # I3: (date DESC, emp_code ASC) — DB-side sort for list_attendance
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            background=True,
        )
        # I4: (department ASC, joined_on ASC) — employee department/join-date analytics
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)],
            background=True,
        )
    except Exception as exc:
        sanitized = _sanitize_error_message(str(exc))
        logger.error(f"Startup index creation failed: {sanitized}")
        raise RuntimeError(f"Startup index creation failed: {sanitized}") from None


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    _create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers — datetime conversion
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a naive-UTC MongoDB datetime (or None) to epoch ms for API responses."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Convert epoch ms to an IST-aware datetime, truncated to whole seconds (R1)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)
    return dt.astimezone(IST)


def to_mongo_dt(ist_dt: datetime) -> datetime:
    """Convert an IST-aware datetime to naive UTC for MongoDB BSON storage."""
    return ist_dt.astimezone(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Helpers — rounding
# --------------------------------------------------------------------------- #
def half_up(value: float, places: int = 2) -> float:
    """Round half-up to `places` decimal places (R4, R8). Replaces Python banker's rounding."""
    q = "0." + "0" * places
    return float(Decimal(str(value)).quantize(Decimal(q), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Helpers — attendance-date and derived-field computation
# --------------------------------------------------------------------------- #
def attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    Return the attendance date YYYY-MM-DD per R1.

    Overnight shift (shift_end <= shift_start, e.g. 22:00-06:00): a punch-in
    whose IST time is strictly before shift_end belongs to the shift that started
    the previous calendar day.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        punch_min = punch_in_ist.hour * 60 + punch_in_ist.minute
        if punch_min < eh * 60 + em:
            return (punch_in_ist - timedelta(days=1)).date().isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: Grace period is 10 minutes strict (elapsed > 600 s).
    late_minutes = floor(elapsed_seconds / 60) when late, else 0.

    FIX D10: gate is elapsed_seconds > 600, not minutes > 10.
             Example — 09:40:01 with shift 09:30: elapsed=601s > 600 → returns 10.
    FIX D11: shift-start is anchored to att_date in IST, not to punch_in.date(),
             so overnight-shift punch-ins are correctly evaluated.
    """
    h, m = map(int, shift_start.split(":"))
    base = datetime.fromisoformat(att_date)
    start_ist = base.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    elapsed_s = (punch_in_ist - start_ist).total_seconds()
    if elapsed_s > 600:
        return int(elapsed_s / 60)
    return 0


def compute_work_hours(punch_in_ist: datetime, punch_out_ist: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, rounded half-up to 2 dp."""
    total_s = (punch_out_ist - punch_in_ist).total_seconds()
    return half_up(total_s / 3600, 2)


def compute_overtime(
    punch_out_ist: datetime,
    shift_end: str,
    shift_start: str,
    att_date: str,
) -> int:
    """
    R3: Floor minutes past shift_end; 0 when < 30.

    FIX D12: all datetimes are IST-aware (no naive/aware mismatch).
    For overnight shifts shift_end falls on the next calendar day after att_date.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    base_date = datetime.fromisoformat(att_date).date()
    end_date = base_date + timedelta(days=1) if is_overnight else base_date
    end_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    elapsed_m = int((punch_out_ist - end_ist).total_seconds() / 60)
    return elapsed_m if elapsed_m >= 30 else 0


# --------------------------------------------------------------------------- #
# Serialization helpers — MongoDB documents → API response dicts
# --------------------------------------------------------------------------- #
def _serialize_history(history: list) -> list:
    """Convert BSON dates inside history[].changes to epoch ms integers."""
    result = []
    for entry in history:
        changes: dict = {}
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": to_epoch_ms(change.get("from")),
                    "to": to_epoch_ms(change.get("to")),
                }
            else:
                changes[field] = change
        result.append(
            {
                "at": to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return result


def _serialize_attendance(doc: dict) -> dict:
    """Convert a MongoDB attendance_logs document to an API AttendanceRecord dict."""
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
        "history": _serialize_history(doc.get("history", [])),
    }


def _serialize_employee(doc: dict) -> dict:
    """Convert a MongoDB employees document to an API Employee dict."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
EpochMillisField = Annotated[int, Field(strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]


class EmployeeCreate(BaseModel):
    """Matches openapi EmployeeCreate schema."""

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
    )

    @model_validator(mode="after")
    def _validate_employee(self) -> "EmployeeCreate":
        """shift_start must differ from shift_end; joined_on must be a valid calendar date."""
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        from datetime import date as _date
        try:
            _date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on is not a valid calendar date")
        return self


class PunchInRequest(BaseModel):
    """Matches openapi PunchInRequest schema."""

    emp_code: str
    punched_at: Optional[EpochMillisField] = None  # FIX S2: range-validated
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"  # FIX D23: enum


class PunchOutRequest(BaseModel):
    """Matches openapi PunchOutRequest schema."""

    emp_code: str
    punched_at: Optional[EpochMillisField] = None


class RegularizeRequest(BaseModel):
    """Matches openapi RegularizeRequest schema."""

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillisField] = None
    punch_out: Optional[EpochMillisField] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """FIX D20: ping MongoDB; 503 if unreachable."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """
    FIX D8:  unique index I1 is the atomic concurrency gate for 409.
    FIX D9:  created_at stored as UTC datetime; response serialized to epoch ms.
    FIX D17: shift_start == shift_end → 422 via model_validator.
    FIX D18: emp_code pattern enforced by Pydantic Field.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    FIX D1: skip = (page-1) * page_size.
    FIX D2: total = count_documents(q) — respects department filter.
    FIX D3: .sort("emp_code", ASCENDING).
    FIX D22: page/page_size bounds enforced via Query constraints.
    """
    q: dict = {}
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
    return {
        "items": [_serialize_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    FIX D4/D5/D6: timestamps converted via from_epoch_ms → IST-aware datetime.
    FIX D7:  404 guard immediately after employee lookup.
    FIX D8:  DuplicateKeyError from index I2 → 409 (atomic, race-safe).
    FIX D10: compute_late_minutes uses elapsed_s > 600 boundary.
    FIX D11: shift-start anchored to att_date, not punch_in.date().
    FIX D16: response epoch-ms serialized; spurious 'id' field removed.
    FIX D23: status enum restricted by Literal in PunchInRequest.
    FIX D24: from_epoch_ms / datetime.now both truncate to whole seconds (R1).
    R1:      overnight-shift date derived via attendance_date().
    """
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is not None:
        ts_ist = from_epoch_ms(body.punched_at)
    else:
        ts_ist = datetime.now(timezone.utc).replace(microsecond=0).astimezone(IST)

    att_date = attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts_ist, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": to_mongo_dt(ts_ist),   # naive UTC for BSON storage
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    doc.pop("_id", None)
    return _serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    FIX D13: DB-side skip/limit — collection never loaded into memory.
    FIX D14: sort [date DESC, emp_code ASC] via MongoDB (uses index I3).
    FIX D15: date_from > date_to → 422.
    FIX D22: page_size capped at 100.
    """
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(ALL_STATUSES)}",
        )
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to",
        )

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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_serialize_attendance(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Phase 02 endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Close the employee's most recent open punch-in record at or before punched_at.
    Computes work_hours (R4), overtime_minutes (R3), half_day (R5).
    404: unknown employee or no open punch-in found.
    409: record already punched out (atomic via find_one_and_update filter).
    422: punched_at <= punch_in or > 24 h after punch_in.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is not None:
        po_ist = from_epoch_ms(body.punched_at)
    else:
        po_ist = datetime.now(timezone.utc).replace(microsecond=0).astimezone(IST)

    po_utc = to_mongo_dt(po_ist)  # naive UTC for BSON comparison

    # Find most recent record whose punch_in is at or before punched_at (ignoring null punch_ins).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": po_utc}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        # Check if a punch-in exists after punched_at (i.e. punched_at <= punch_in -> 422).
        future_rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", ASCENDING)],
        )
        if future_rec and future_rec.get("punch_in") is not None:
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
        raise HTTPException(status_code=404, detail="no punch-in found for this employee")

    if rec.get("punch_in") is None:
        raise HTTPException(status_code=404, detail="no punch-in found for this employee")

    # Convert stored naive-UTC punch_in to IST-aware for validation.
    pi_ist = rec["punch_in"].replace(tzinfo=timezone.utc).astimezone(IST)

    # 422 validations first (before open/closed check).
    elapsed_s = (po_ist - pi_ist).total_seconds()
    if elapsed_s <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if elapsed_s > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    # 409 if this specific record is already punched out.
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out for this record")

    # Compute derived fields (R3-R5).
    att_date = rec["date"]
    wh = compute_work_hours(pi_ist, po_ist)
    ot = compute_overtime(po_ist, emp["shift_end"], emp["shift_start"], att_date)
    hd = wh < 4.50  # R5: work_hours already rounded half-up by compute_work_hours

    # Atomic punch-out: filter punch_out=None is the concurrency gate (409 for the loser).
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": po_utc,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out for this record")

    updated.pop("_id", None)
    return _serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Manually correct one day's attendance record.
    Omitted fields are unchanged; recomputes R2-R5 on the final state.
    Appends exactly one history entry with only the fields that changed.
    Optimistic concurrency via history array-size check (409 for the loser).
    """
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="no attendance record for this date")

    # ------------------------------------------------------------------ #
    # 1. Determine target values, apply contract rules
    # ------------------------------------------------------------------ #
    new_status = body.status if body.status is not None else rec["status"]

    # ABSENT/LEAVE + punch times = 422.
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be set when status is ABSENT or LEAVE",
            )

    # Resolve new punch_in (IST-aware).
    if body.punch_in is not None:
        new_pi_ist = from_epoch_ms(body.punch_in)
    elif rec.get("punch_in") is not None:
        new_pi_ist = rec["punch_in"].replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        new_pi_ist = None

    # Resolve new punch_out (IST-aware).
    if body.punch_out is not None:
        new_po_ist = from_epoch_ms(body.punch_out)
    elif rec.get("punch_out") is not None:
        new_po_ist = rec["punch_out"].replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        new_po_ist = None

    # ABSENT/LEAVE clears both punch times.
    if new_status in ("ABSENT", "LEAVE"):
        new_pi_ist = None
        new_po_ist = None

    # Presence requires punch_in.
    if new_status in PRESENCE_STATUSES and new_pi_ist is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    # punch_in must stay on the record's attendance date (R1 overnight-safe).
    if new_pi_ist is not None:
        derived = attendance_date(new_pi_ist, emp["shift_start"], emp["shift_end"])
        if derived != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in would change the attendance date",
            )

    # punch_out must be after punch_in and within 24 h.
    if new_po_ist is not None:
        if new_pi_ist is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        elapsed_s = (new_po_ist - new_pi_ist).total_seconds()
        if elapsed_s <= 0:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if elapsed_s > 86400:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # ------------------------------------------------------------------ #
    # 2. Recompute derived fields (R2-R5) on the final state
    # ------------------------------------------------------------------ #
    if new_pi_ist is not None and new_status in PRESENCE_STATUSES:
        new_late = compute_late_minutes(new_pi_ist, emp["shift_start"], date)
    else:
        new_late = 0

    if new_pi_ist is not None and new_po_ist is not None:
        new_wh = compute_work_hours(new_pi_ist, new_po_ist)
        new_ot = compute_overtime(new_po_ist, emp["shift_end"], emp["shift_start"], date)
        new_hd = new_wh < 4.50  # R5
    else:
        new_wh = None
        new_ot = 0
        new_hd = False

    # ------------------------------------------------------------------ #
    # 3. Build changes dict — only fields that actually changed
    # ------------------------------------------------------------------ #
    # Convert IST datetimes back to naive UTC for comparison with stored values.
    new_pi_mongo = to_mongo_dt(new_pi_ist) if new_pi_ist is not None else None
    new_po_mongo = to_mongo_dt(new_po_ist) if new_po_ist is not None else None

    changes: dict = {}
    old_status = rec["status"]
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}

    old_pi = rec.get("punch_in")   # naive UTC or None
    if old_pi != new_pi_mongo:
        changes["punch_in"] = {"from": old_pi, "to": new_pi_mongo}

    old_po = rec.get("punch_out")  # naive UTC or None
    if old_po != new_po_mongo:
        changes["punch_out"] = {"from": old_po, "to": new_po_mongo}

    old_late = rec.get("late_minutes", 0)
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}

    old_wh = rec.get("work_hours")
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}

    old_ot = rec.get("overtime_minutes", 0)
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}

    old_hd = rec.get("half_day", False)
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    # Nothing changed → 422.
    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    # ------------------------------------------------------------------ #
    # 4. Build history entry; store naive-UTC for consistency with punch_in
    # ------------------------------------------------------------------ #
    history_entry = {
        "at": datetime.utcnow().replace(microsecond=0),  # naive UTC stored in BSON
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # ------------------------------------------------------------------ #
    # 5. Atomic update with optimistic concurrency on history array length
    # ------------------------------------------------------------------ #
    current_len = len(rec.get("history") or [])
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [
                {"$size": {"$ifNull": ["$history", []]}},
                current_len,
            ]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi_mongo,
                "punch_out": new_po_mongo,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="record was modified concurrently; please retry")

    result.pop("_id", None)
    return _serialize_attendance(result)


# --------------------------------------------------------------------------- #
# Phase 03 helpers — month boundaries
# --------------------------------------------------------------------------- #
import calendar as _calendar

def _month_bounds(month: str) -> tuple[str, str]:
    """Return (first_day, last_day) as 'YYYY-MM-DD' strings for a YYYY-MM month."""
    year, mon = int(month[:4]), int(month[5:])
    last = _calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def _count_working_days(start: str, end: str) -> int:
    """Count Mon-Fri days between two inclusive YYYY-MM-DD strings (R7)."""
    from datetime import date as _date
    s = _date.fromisoformat(start)
    e = _date.fromisoformat(end)
    count = 0
    cur = s
    while cur <= e:
        if cur.weekday() < 5:  # 0=Mon … 4=Fri
            count += 1
        cur += timedelta(days=1)
    return count


def _validate_month(month: str) -> None:
    import re
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")


# --------------------------------------------------------------------------- #
# Phase 03 endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """
    Monthly attendance summary for one employee (R7, R8).
    Backed by a MongoDB aggregation; reads stored derived values (do not recompute).
    404 for unknown employee.
    """
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    first_day, last_day = _month_bounds(month)

    # R7: working days from max(joined_on, first_day) to last_day
    effective_start = max(first_day, emp["joined_on"])
    working_days = _count_working_days(effective_start, last_day)

    # Aggregation — read stored derived values (per contract note)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$addFields": {
            # present_days: presence-status on Mon-Fri; half_day = 0.5
            # weekday: $dayOfWeek returns 1=Sun … 7=Sat; Mon-Fri = 2-6
            "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$addFields": {
            "_is_weekday": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
            "_is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": ["$_is_weekday", "$_is_presence"]},
                    {"$cond": ["$half_day", 0.5, 1.0]},
                    0.0,
                ]
            }},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days = half_up(r["present_days"], 2)
        leave_days = int(r["leave_days"])
        late_count = int(r["late_count"])
        total_late = int(r["total_late_minutes"])
        total_ot = int(r["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = late_count = total_late = total_ot = 0

    if working_days > 0:
        att_pct = half_up(present_days / working_days * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month (R8, R9).
    Headcount: employees with joined_on <= last day of month.
    avg_work_hours: mean over presence records that have work_hours (not per-employee average).
    Sorted by department ascending.
    """
    first_day, last_day = _month_bounds(month)

    # R9 headcount: employees with joined_on <= last_day, grouped by department
    emp_q: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_q["department"] = department

    headcount_pipe = [
        {"$match": emp_q},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
    ]
    headcount_map = {r["_id"]: r["headcount"] for r in db.employees.aggregate(headcount_pipe)}

    if not headcount_map:
        return {"month": month, "items": []}

    # Attendance aggregation across departments that have headcount
    att_q: dict = {
        "date": {"$gte": first_day, "$lte": last_day},
    }
    if department:
        att_q["department"] = department

    # Join attendance_logs with employees to get department
    att_pipe = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "_emp",
        }},
        {"$unwind": "$_emp"},
        {"$match": ({"_emp.department": department} if department else {})},
        {"$addFields": {
            "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "_is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "_dept": "$_emp.department",
        }},
        {"$addFields": {
            "_is_weekday": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
        }},
        {"$group": {
            "_id": "$_dept",
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": ["$_is_weekday", "$_is_presence"]},
                    {"$cond": ["$half_day", 0.5, 1.0]},
                    0.0,
                ]
            }},
            # avg_work_hours: sum and count of presence records that have work_hours
            "_wh_sum": {"$sum": {
                "$cond": [{"$and": ["$_is_presence", {"$ne": ["$work_hours", None]}]},
                           "$work_hours", 0.0]
            }},
            "_wh_count": {"$sum": {
                "$cond": [{"$and": ["$_is_presence", {"$ne": ["$work_hours", None]}]}, 1, 0]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    att_rows = {r["_id"]: r for r in db.attendance_logs.aggregate(att_pipe)}

    items = []
    for dept in sorted(headcount_map):
        hc = headcount_map[dept]
        if hc == 0:
            continue
        a = att_rows.get(dept, {})
        pd = half_up(a.get("present_days", 0.0), 2)
        wh_count = a.get("_wh_count", 0)
        avg_wh = half_up(a["_wh_sum"] / wh_count, 2) if wh_count > 0 else None
        items.append({
            "department": dept,
            "headcount": hc,
            "present_days": pd,
            "avg_work_hours": avg_wh,
            "late_count": int(a.get("late_count", 0)),
            "total_late_minutes": int(a.get("total_late_minutes", 0)),
            "leave_count": int(a.get("leave_count", 0)),
            "on_duty_count": int(a.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard (standard competition ranking).
    Ranking within department if given; only employees with total_late_minutes > 0.
    limit applies after ranking (ties at rank <= limit are all included).
    Order: total_late_minutes DESC, emp_code ASC.
    """
    first_day, last_day = _month_bounds(month)

    match_stage: dict = {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}

    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "_emp",
        }},
        {"$unwind": "$_emp"},
    ]
    if department:
        pipeline.append({"$match": {"_emp.department": department}})

    pipeline += [
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$_emp.name"},
            "department": {"$first": "$_emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1,
        }}
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department (R9).
    Exactly one row per calendar day from `from` to `to` inclusive.
    Uses $densify for gap-filling; no Python loops over date range.
    moving_avg_7d: mean of non-null attendance_rate in current + up to 6 preceding rows in range.
    """
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must not be before from")

    from datetime import date as _date
    days = (_date.fromisoformat(to_date) - _date.fromisoformat(from_date)).days + 1
    if days > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")

    # 404 if department has no employees at all
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="unknown department")

    # Build Python datetime bounds for $densify (bounds must be literal BSON dates)
    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt   = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)  # exclusive upper bound


    # Aggregation: convert string date -> BSON date, group, then densify for gap-fill
    att_pipe = [
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "_emp",
        }},
        {"$unwind": "$_emp"},
        {"$match": {
            "_emp.department": department,
        }},
        {"$addFields": {
            "_is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            # Convert string date field to BSON date so $densify can operate on it
            "_date_bson": {"$dateFromString": {"dateString": "$date"}},
        }},
        {"$group": {
            "_id": "$_date_bson",
            "present_count_raw": {"$sum": {
                "$cond": [
                    "$_is_presence",
                    {"$cond": ["$half_day", 0.5, 1.0]},
                    0.0,
                ]
            }},
            "late_count_raw": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        # $densify requires BSON date field; bounds are Python datetime literals
        {"$densify": {
            "field": "_id",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt],
            },
        }},
        {"$sort": {"_id": 1}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
            "_dow": {"$dayOfWeek": "$_id"},
            "present_count": {"$ifNull": ["$present_count_raw", 0.0]},
            "late_count": {"$ifNull": ["$late_count_raw", 0]},
        }},
        {"$addFields": {
            "is_working_day": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$day"]}
                        ]
                    }
                }},
                {"$count": "count"}
            ],
            "as": "_hc_arr"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$first": "$_hc_arr.count"}, 0]}
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"]
                    }
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }}
    ]

    dept_emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
    has_logs = bool(db.attendance_logs.find_one({
        "date": {"$gte": from_date, "$lte": to_date},
        "emp_code": {"$in": dept_emp_codes}
    }))

    if not has_logs:
        empty_pipe = [
            {"$documents": [{}]},
            {"$project": {"_idx": {"$range": [0, days]}}},
            {"$unwind": "$_idx"},
            {"$project": {
                "_id": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$_idx"}}
            }},
            {"$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "_dow": {"$dayOfWeek": "$_id"},
                "present_count": 0.0,
                "late_count": 0,
            }},
            {"$addFields": {
                "is_working_day": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$department", department]},
                                {"$lte": ["$joined_on", "$$day"]}
                            ]
                        }
                    }},
                    {"$count": "count"}
                ],
                "as": "_hc_arr"
            }},
            {"$addFields": {
                "headcount": {"$ifNull": [{"$first": "$_hc_arr.count"}, 0]}
            }},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }},
            {"$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }},
            {"$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }}
        ]
        rows = list(db.aggregate(empty_pipe))
    else:
        rows = list(db.attendance_logs.aggregate(att_pipe))

    items = []
    for r in rows:
        ar = r.get("attendance_rate")
        ma = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": int(r["headcount"]),
            "present_count": half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": half_up(ar, 4) if ar is not None else None,
            "moving_avg_7d": half_up(ma, 4) if ma is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Phase 04 endpoint — Admin Explain
# --------------------------------------------------------------------------- #
ExplainEndpointEnum = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpointEnum,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, built exactly as that endpoint builds it.
    """
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="invalid status")

    collection = "attendance_logs"

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be after date_to")
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
        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        first_day, last_day = _month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {"$addFields": {
                "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }},
            {"$addFields": {
                "_is_weekday": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
                "_is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {
                    "$cond": [
                        {"$and": ["$_is_weekday", "$_is_presence"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                }},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        first_day, last_day = _month_bounds(month)
        att_pipe = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "_emp",
            }},
            {"$unwind": "$_emp"},
        ]
        if department:
            att_pipe.append({"$match": {"_emp.department": department}})
        att_pipe += [
            {"$addFields": {
                "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "_is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "_dept": "$_emp.department",
            }},
            {"$addFields": {
                "_is_weekday": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
            }},
            {"$group": {
                "_id": "$_dept",
                "present_days": {"$sum": {
                    "$cond": [
                        {"$and": ["$_is_weekday", "$_is_presence"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                }},
                "_wh_sum": {"$sum": {
                    "$cond": [{"$and": ["$_is_presence", {"$ne": ["$work_hours", None]}]},
                               "$work_hours", 0.0]
                }},
                "_wh_count": {"$sum": {
                    "$cond": [{"$and": ["$_is_presence", {"$ne": ["$work_hours", None]}]}, 1, 0]
                }},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": att_pipe,
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        first_day, last_day = _month_bounds(month)
        match_stage = {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}
        pipeline = [
            {"$match": match_stage},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "_emp",
            }},
            {"$unwind": "$_emp"},
        ]
        if department:
            pipeline.append({"$match": {"_emp.department": department}})
        pipeline += [
            {"$group": {
                "_id": "$emp_code",
                "name": {"$first": "$_emp.name"},
                "department": {"$first": "$_emp.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }},
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }}
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to must not be before from")
        from datetime import date as _date
        days = (_date.fromisoformat(to_date) - _date.fromisoformat(from_date)).days + 1
        if days > 92:
            raise HTTPException(status_code=422, detail="date range must not exceed 92 days")
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)
        att_pipe = [
            {"$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "_emp",
            }},
            {"$unwind": "$_emp"},
            {"$match": {
                "_emp.department": department,
            }},
            {"$addFields": {
                "_is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "_date_bson": {"$dateFromString": {"dateString": "$date"}},
            }},
            {"$group": {
                "_id": "$_date_bson",
                "present_count_raw": {"$sum": {
                    "$cond": [
                        "$_is_presence",
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                }},
                "late_count_raw": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }},
            {"$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }},
            {"$sort": {"_id": 1}},
            {"$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "_dow": {"$dayOfWeek": "$_id"},
                "present_count": {"$ifNull": ["$present_count_raw", 0.0]},
                "late_count": {"$ifNull": ["$late_count_raw", 0]},
            }},
            {"$addFields": {
                "is_working_day": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$department", department]},
                                {"$lte": ["$joined_on", "$$day"]}
                            ]
                        }
                    }},
                    {"$count": "count"}
                ],
                "as": "_hc_arr"
            }},
            {"$addFields": {
                "headcount": {"$ifNull": [{"$first": "$_hc_arr.count"}, 0]}
            }},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }},
            {"$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }},
            {"$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }}
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": att_pipe,
            "cursor": {},
        }

    raw_explain = db.command("explain", cmd, verbosity="executionStats")
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": raw_explain,
    }
