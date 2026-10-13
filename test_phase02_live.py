"""
Phase 02 live tests: punch-out and regularize endpoints.
ASCII-only output for Windows cp1252 console.
"""
import subprocess, time, urllib.request, urllib.error, json, os, sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

PORT = 8790
IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     serverSelectionTimeoutMS=4000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

TEST_EMP  = "EMP9901"
TEST_DATE = "2026-07-06"

def cleanup():
    db.employees.delete_many({"emp_code": TEST_EMP})
    db.attendance_logs.delete_many({"emp_code": TEST_EMP})

def to_epoch_ms(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

punch_in_ist  = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
punch_out_ist = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
punch_in_ms   = to_epoch_ms(punch_in_ist)
punch_out_ms  = to_epoch_ms(punch_out_ist)

cleanup()
db.employees.insert_one({
    "emp_code": TEST_EMP,
    "name": "Test Worker",
    "email": "test@example.com",
    "department": "Testing",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2025-01-01",
    "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
})
db.employees.create_index("emp_code", unique=True, background=True)
db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, background=True)

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

def req(method, path, body=None):
    url = f"http://localhost:{PORT}{path}"
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(r, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as ex:
        return None, str(ex)

errors = []

def check(label, code, body, expect_code, **expect_fields):
    ok = code == expect_code
    for k, v in expect_fields.items():
        if body.get(k) != v:
            ok = False
    tag = "PASS" if ok else "FAIL"
    extra = ""
    if code != expect_code:
        extra += f" (expected {expect_code})"
    if not ok:
        extra += f" body={json.dumps(body)[:120]}"
    print(f"{tag}: {label} -> HTTP {code}{extra}")
    if not ok:
        errors.append(label)

# ---- seed open punch-in directly via MongoDB ----
pi_utc = punch_in_ist.astimezone(timezone.utc).replace(tzinfo=None)
db.attendance_logs.insert_one({
    "emp_code": TEST_EMP, "date": TEST_DATE, "status": "PRESENT",
    "punch_in": pi_utc, "punch_out": None,
    "work_hours": None, "late_minutes": 10, "overtime_minutes": 0,
    "half_day": False, "history": [],
})
print(f"Seeded punch-in for {TEST_EMP} at IST 09:40:01")

# T01 punch-out unknown employee -> 404
code, body = req("POST", "/attendance/punch-out", {"emp_code": "EMP0000", "punched_at": punch_out_ms})
check("T01 punch-out unknown emp -> 404", code, body, 404)

# T02 punch-out before punch_in -> 422
before_ms = to_epoch_ms(punch_in_ist - timedelta(minutes=5))
code, body = req("POST", "/attendance/punch-out", {"emp_code": TEST_EMP, "punched_at": before_ms})
check("T02 punch-out before punch_in -> 422", code, body, 422)

# T03 punch-out >24h after punch_in -> 422
over24h_ms = to_epoch_ms(punch_in_ist + timedelta(hours=25))
code, body = req("POST", "/attendance/punch-out", {"emp_code": TEST_EMP, "punched_at": over24h_ms})
check("T03 punch-out >24h -> 422", code, body, 422)

# T04 punch-out happy path -> 200, verify derived fields
code, body = req("POST", "/attendance/punch-out", {"emp_code": TEST_EMP, "punched_at": punch_out_ms})
check("T04 punch-out happy path -> 200", code, body, 200)
if code == 200:
    wh   = body.get("work_hours")
    ot   = body.get("overtime_minutes")
    hd   = body.get("half_day")
    po   = body.get("punch_out")
    # 19:10 - 09:40:01 = 9h 29m 59s = 34199s / 3600 = 9.4997... -> half_up 9.50
    # overtime: 19:10 - 18:30 = 40 min >= 30 -> 40
    # half_day: 9.50 < 4.50 -> False
    for sub, got, exp in [("work_hours", wh, 9.50), ("overtime", ot, 40),
                           ("half_day", hd, False), ("punch_out_ms", po, punch_out_ms)]:
        ok2 = got == exp
        print(f"  T04-{sub}: {got} (exp {exp}) {'OK' if ok2 else 'FAIL'}")
        if not ok2:
            errors.append(f"T04-{sub}")

# T05 punch-out duplicate -> 409
code, body = req("POST", "/attendance/punch-out", {"emp_code": TEST_EMP, "punched_at": punch_out_ms})
check("T05 punch-out duplicate -> 409", code, body, 409)

# T06 regularize unknown employee -> 404
rb = {"reason": "Test correction here", "regularized_by": "admin"}
code, body = req("PATCH", f"/attendance/EMP0000/{TEST_DATE}", rb)
check("T06 regularize unknown emp -> 404", code, body, 404)

# T07 regularize unknown date -> 404
code, body = req("PATCH", f"/attendance/{TEST_EMP}/2000-01-01", rb)
check("T07 regularize unknown date -> 404", code, body, 404)

# T08 regularize no change -> 422 (sending same status that's already stored)
code, body = req("PATCH", f"/attendance/{TEST_EMP}/{TEST_DATE}",
                 {**rb, "status": "PRESENT"})
check("T08 regularize no change -> 422", code, body, 422)

# T09 regularize ABSENT + punch_in -> 422
code, body = req("PATCH", f"/attendance/{TEST_EMP}/{TEST_DATE}",
                 {**rb, "status": "ABSENT", "punch_in": punch_in_ms})
check("T09 regularize ABSENT+punch_in -> 422", code, body, 422)

# T10 regularize happy path: change status to WFH
code, body = req("PATCH", f"/attendance/{TEST_EMP}/{TEST_DATE}",
                 {**rb, "status": "WFH"})
check("T10 regularize status WFH -> 200", code, body, 200)
if code == 200:
    hist = body.get("history", [])
    for sub, got, exp in [
        ("status", body.get("status"), "WFH"),
        ("hist_len", len(hist), 1),
        ("changes_has_status", "status" in hist[0].get("changes", {}) if hist else False, True),
        ("hist_by", hist[0].get("by") if hist else None, "admin"),
        ("hist_at_int", isinstance(hist[0].get("at"), int) if hist else False, True),
    ]:
        ok2 = got == exp
        print(f"  T10-{sub}: {got!r} (exp {exp!r}) {'OK' if ok2 else 'FAIL'}")
        if not ok2:
            errors.append(f"T10-{sub}")

# T11 second correction appends second history entry
code, body = req("PATCH", f"/attendance/{TEST_EMP}/{TEST_DATE}",
                 {"reason": "Reverting to present status", "regularized_by": "admin2", "status": "PRESENT"})
check("T11 second correction -> 200, history len=2", code, body, 200)
if code == 200:
    ok2 = len(body.get("history", [])) == 2
    print(f"  T11-hist_len: {len(body.get('history', []))} (exp 2) {'OK' if ok2 else 'FAIL'}")
    if not ok2:
        errors.append("T11-hist_len")

# ---- cleanup ----
proc.terminate()
proc.wait()
cleanup()

print()
if errors:
    print(f"PHASE 02 FAIL: {errors}")
    sys.exit(1)
else:
    print("PHASE 02 LIVE TESTS: ALL PASSED")
