"""Targeted live tests for the 3 audit fixes:
1. Punch-out selects eligible record and handles null punch-in values safely.
2. Epoch-millisecond fields reject strings and floats.
3. joined_on rejects impossible calendar dates.
"""
import subprocess, time, urllib.request, urllib.error, json, os, sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
PORT = 8798
IST = timezone(timedelta(hours=5, minutes=30))
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=4000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

EMP_CODE = "EMP9920"
db.employees.delete_many({"emp_code": EMP_CODE})
db.attendance_logs.delete_many({"emp_code": EMP_CODE})

# Insert test employee
db.employees.insert_one({
    "emp_code": EMP_CODE,
    "name": "Audit Test",
    "email": "audit@test.com",
    "department": "QA",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-07-01",
    "created_at": datetime.now(timezone.utc),
})

proc = subprocess.Popen(
    [r".venv\Scripts\python.exe", "-m", "uvicorn", "app.main:app",
     "--port", str(PORT), "--log-level", "warning"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
for _ in range(20):
    time.sleep(0.5)
    try:
        urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=1)
        break
    except Exception:
        pass

def post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://localhost:{PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as ex:
        return None, str(ex)

def patch(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://localhost:{PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="PATCH")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as ex:
        return None, str(ex)

errors = []
def check(label, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag}: {label}" + (f" -- {detail}" if not cond else ""))
    if not cond:
        errors.append(label)

# -------------------------------------------------------------
# 1. joined_on rejects impossible calendar dates
# -------------------------------------------------------------
code, body = post("/employees", {
    "emp_code": "EMP9921",
    "name": "Feb 31",
    "email": "feb31@x.com",
    "department": "QA",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-02-31",
})
check("T01 joined_on 2026-02-31 -> 422", code == 422)

code, body = post("/employees", {
    "emp_code": "EMP9922",
    "name": "Apr 31",
    "email": "apr31@x.com",
    "department": "QA",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-04-31",
})
check("T02 joined_on 2026-04-31 -> 422", code == 422)

# -------------------------------------------------------------
# 2. Epoch-millisecond fields reject strings and floats
# -------------------------------------------------------------
# String punched_at in punch-in -> 422
code, body = post("/attendance/punch-in", {
    "emp_code": EMP_CODE,
    "punched_at": "1783312500000",
})
check("T03 punch-in string punched_at -> 422", code == 422)

# Float punched_at in punch-in -> 422
code, body = post("/attendance/punch-in", {
    "emp_code": EMP_CODE,
    "punched_at": 1783312500000.5,
})
check("T04 punch-in float punched_at -> 422", code == 422)

# Float punched_at in punch-out -> 422
code, body = post("/attendance/punch-out", {
    "emp_code": EMP_CODE,
    "punched_at": 1783345200000.0,
})
check("T05 punch-out float punched_at -> 422", code == 422)

# String punch_in in patch regularize -> 422
code, body = patch(f"/attendance/{EMP_CODE}/2026-07-06", {
    "punch_in": "1783312500000",
    "reason": "Correction test",
    "regularized_by": "admin",
})
check("T06 regularize string punch_in -> 422", code == 422)

# -------------------------------------------------------------
# 3. Punch-out: handles null punch-in & selects eligible record
# -------------------------------------------------------------
# Seed an ABSENT record (punch_in: None)
db.attendance_logs.insert_one({
    "emp_code": EMP_CODE,
    "date": "2026-07-06",
    "status": "ABSENT",
    "punch_in": None,
    "punch_out": None,
    "work_hours": None,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": False,
    "history": [],
})

# Punch-out when employee ONLY has a null punch-in record -> 404 (not 500!)
code, body = post("/attendance/punch-out", {
    "emp_code": EMP_CODE,
    "punched_at": 1783345200000,
})
check("T07 punch-out with only null punch_in record -> 404", code == 404)

# Now seed a valid punch-in on 2026-07-07: 09:30 IST (04:00 UTC)
# 2026-07-07 04:00:00 UTC = 1783396800000 ms
pi_utc = datetime(2026, 7, 7, 4, 0, 0)
db.attendance_logs.insert_one({
    "emp_code": EMP_CODE,
    "date": "2026-07-07",
    "status": "PRESENT",
    "punch_in": pi_utc,
    "punch_out": None,
    "work_hours": None,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": False,
    "history": [],
})

# Punch-out at 18:30 IST (13:00 UTC) = 1783429200000 ms -> should select 2026-07-07 record, not null 2026-07-06
code, body = post("/attendance/punch-out", {
    "emp_code": EMP_CODE,
    "punched_at": 1783429200000,
})
check("T08 punch-out selects correct eligible record -> 200", code == 200)
if code == 200:
    check("T08a date is 2026-07-07", body.get("date") == "2026-07-07")
    check("T08b work_hours is 9.0", body.get("work_hours") == 9.0)

# Punch-out before punch-in on a record with punch_in > punched_at -> 422
code, body = post("/attendance/punch-out", {
    "emp_code": EMP_CODE,
    "punched_at": 1783396700000, # 100s before 1783396800000
})
check("T09 punch-out before punch-in -> 422", code == 422)

# T10: Older eligible open record vs newer future record
# Seed older open record (2026-07-08 09:30 IST / 04:00 UTC = 1783483200000 ms)
pi_old = datetime(2026, 7, 8, 4, 0, 0)
db.attendance_logs.insert_one({
    "emp_code": EMP_CODE,
    "date": "2026-07-08",
    "status": "PRESENT",
    "punch_in": pi_old,
    "punch_out": None,
    "work_hours": None,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": False,
    "history": [],
})
# Seed newer future record (2026-07-09 09:30 IST / 04:00 UTC = 1783569600000 ms)
pi_future = datetime(2026, 7, 9, 4, 0, 0)
db.attendance_logs.insert_one({
    "emp_code": EMP_CODE,
    "date": "2026-07-09",
    "status": "PRESENT",
    "punch_in": pi_future,
    "punch_out": None,
    "work_hours": None,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": False,
    "history": [],
})

# Punch-out at 2026-07-08 18:30 IST (13:00 UTC = 1783515600000 ms)
code, body = post("/attendance/punch-out", {
    "emp_code": EMP_CODE,
    "punched_at": 1783515600000,
})
check("T10 punch-out selects older eligible record over future record -> 200", code == 200)
if code == 200:
    check("T10a selected record date is 2026-07-08", body.get("date") == "2026-07-08")
    check("T10b work_hours is 9.0", body.get("work_hours") == 9.0)
    future_doc = db.attendance_logs.find_one({"emp_code": EMP_CODE, "date": "2026-07-09"})
    check("T10c future record remains open", future_doc and future_doc.get("punch_out") is None)

proc.terminate()
proc.wait()

db.employees.delete_many({"emp_code": EMP_CODE})
db.attendance_logs.delete_many({"emp_code": EMP_CODE})

print()
if errors:
    print(f"AUDIT FIXES FAIL: {errors}")
    sys.exit(1)
else:
    print("ALL AUDIT FIX TESTS PASSED")
