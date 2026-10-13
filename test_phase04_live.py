"""Phase 04 live tests: /admin/explain/{endpoint} for all 5 operations."""
import subprocess, time, urllib.request, urllib.error, json, os, sys
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
PORT = 8796

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

def get(path):
    try:
        r = urllib.request.urlopen(f"http://localhost:{PORT}{path}", timeout=10)
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

def check_explain_structure(body, expected_ep):
    check(f"{expected_ep} endpoint field", body.get("endpoint") == expected_ep)
    check(f"{expected_ep} collection field", body.get("collection") == "attendance_logs")
    explain = body.get("explain", {})
    exp_str = json.dumps(explain)
    check(f"{expected_ep} has IXSCAN or COUNT_SCAN", ("IXSCAN" in exp_str or "COUNT_SCAN" in exp_str))
    check(f"{expected_ep} no COLLSCAN", ("COLLSCAN" not in exp_str))

# --- T01 invalid endpoint -> 422
code, body = get("/admin/explain/unknown_operation")
check("T01 invalid endpoint -> 422", code == 422)

# --- T02 attendance_list happy path -> 200
code, body = get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20")
check("T02 attendance_list -> 200", code == 200)
if code == 200:
    check_explain_structure(body, "attendance_list")

# --- T03 attendance_list date_from > date_to -> 422
code, body = get("/admin/explain/attendance_list?date_from=2026-07-31&date_to=2026-07-01")
check("T03 attendance_list bad dates -> 422", code == 422)

# --- T04 employee_monthly missing required params -> 422
code, body = get("/admin/explain/employee_monthly")
check("T04 employee_monthly missing params -> 422", code == 422)

# --- T05 employee_monthly happy path -> 200
code, body = get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
check("T05 employee_monthly -> 200", code == 200)
if code == 200:
    check_explain_structure(body, "employee_monthly")

# --- T06 department_summary missing month -> 422
code, body = get("/admin/explain/department_summary")
check("T06 department_summary missing month -> 422", code == 422)

# --- T07 department_summary happy path -> 200
code, body = get("/admin/explain/department_summary?month=2026-07&department=Engineering")
check("T07 department_summary -> 200", code == 200)
if code == 200:
    check_explain_structure(body, "department_summary")

# --- T08 late_leaderboard missing month -> 422
code, body = get("/admin/explain/late_leaderboard")
check("T08 late_leaderboard missing month -> 422", code == 422)

# --- T09 late_leaderboard happy path -> 200
code, body = get("/admin/explain/late_leaderboard?month=2026-07&limit=10&department=Engineering")
check("T09 late_leaderboard -> 200", code == 200)
if code == 200:
    check_explain_structure(body, "late_leaderboard")

# --- T10 department_trend missing required params -> 422
code, body = get("/admin/explain/department_trend")
check("T10 department_trend missing params -> 422", code == 422)

# --- T11 department_trend to < from -> 422
code, body = get("/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-01")
check("T11 department_trend to < from -> 422", code == 422)

# --- T12 department_trend happy path -> 200
code, body = get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
check("T12 department_trend -> 200", code == 200)
if code == 200:
    check_explain_structure(body, "department_trend")

# --- T13 status validation in explain -> 422
code, body = get("/admin/explain/attendance_list?status=BOGUS")
check("T13 explain invalid status -> 422", code == 422)

proc.terminate()
proc.wait()

print()
if errors:
    print(f"PHASE 04 FAIL: {errors}")
    sys.exit(1)
else:
    print("PHASE 04 LIVE TESTS: ALL PASSED")
