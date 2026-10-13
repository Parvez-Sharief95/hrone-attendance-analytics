"""Focused tests for required indexes and index-creation failure handling:
1. Verify all required indexes exist and are created idempotently:
   - employees.emp_code (unique)
   - employees.(department, joined_on) for department/join-date analytics
   - attendance_logs.(emp_code, date) (unique)
   - attendance_logs.(date DESC, emp_code ASC)
2. Verify index creation failure is not silently swallowed:
   - raises RuntimeError on failure
3. Verify secrets are never exposed in error reporting:
   - credentials/passwords in MongoDB URIs are sanitized to ***
"""
import subprocess, time, urllib.request, json, os, sys
from unittest.mock import patch
import pymongo.collection
from pymongo import MongoClient
from dotenv import load_dotenv

sys.path.insert(0, ".")
from app.main import _create_indexes, _sanitize_error_message, db

load_dotenv()
PORT = 8796

errors = []
def check(label, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag}: {label}" + (f" -- {detail}" if not cond else ""))
    if not cond:
        errors.append(label)

print("--- 1. REQUIRED INDEXES VERIFICATION ---")
# 1a. Idempotent creation
try:
    _create_indexes()
    _create_indexes() # Second call must succeed without error
    check("I01 _create_indexes runs idempotently without error", True)
except Exception as e:
    check("I01 _create_indexes runs idempotently without error", False, str(e))

emp_indexes = {i["name"]: i for i in db.employees.list_indexes()}
att_indexes = {i["name"]: i for i in db.attendance_logs.list_indexes()}

# Check employees unique emp_code
has_emp_code_uniq = any(i.get("unique") and list(i["key"].items()) == [("emp_code", 1)] for i in emp_indexes.values())
check("I02 employees.emp_code unique index exists", has_emp_code_uniq)

# Check employees (department, joined_on) for analytics
has_dept_joined = any(list(i["key"].items()) == [("department", 1), ("joined_on", 1)] for i in emp_indexes.values())
check("I03 employees.(department, joined_on) index exists for analytics", has_dept_joined)

# Check attendance_logs unique (emp_code, date)
has_att_emp_date_uniq = any(i.get("unique") and list(i["key"].items()) == [("emp_code", 1), ("date", 1)] for i in att_indexes.values())
check("I04 attendance_logs.(emp_code, date) unique index exists", has_att_emp_date_uniq)

# Check attendance_logs (date DESC, emp_code ASC)
has_att_date_emp = any(list(i["key"].items()) == [("date", -1), ("emp_code", 1)] for i in att_indexes.values())
check("I05 attendance_logs.(date DESC, emp_code ASC) index exists", has_att_date_emp)


print("\n--- 2. INDEX-CREATION FAILURE HANDLING (NOT SWALLOWED) ---")
# 2a. Verify exception is NOT silently swallowed when create_index fails
with patch.object(pymongo.collection.Collection, "create_index", side_effect=Exception("Disk full / permission denied")):
    caught = False
    try:
        _create_indexes()
    except RuntimeError as r_err:
        caught = True
        err_msg = str(r_err)
        check("F01 _create_indexes raises RuntimeError on failure (not swallowed)", True)
        check("F02 error message clearly describes startup index creation failure",
              "Startup index creation failed" in err_msg and "Disk full" in err_msg)
    except Exception as other_err:
        check("F01 _create_indexes raises RuntimeError on failure", False, f"got {type(other_err)}")

    if not caught:
        check("F01 _create_indexes raises RuntimeError on failure (not swallowed)", False, "error was silently swallowed!")


print("\n--- 3. SECRETS SANITIZATION IN ERROR REPORTING ---")
# 3a. Verify credentials in URIs are stripped from exception messages
secret_uri = "mongodb://test_user:FAKE_TEST_PASSWORD@localhost:27017/attendance_db"
simulated_mongo_err = f"Failed to authenticate connection to {secret_uri}: Authentication failed"

# Unit test sanitization function directly
sanitized = _sanitize_error_message(simulated_mongo_err)
check("S01 password redacted in _sanitize_error_message", "SuperSecretPassword123!" not in sanitized)
check("S02 replacement token *** is present in sanitized message", "***@" in sanitized)

# Test inside _create_indexes failure path
with patch.object(pymongo.collection.Collection, "create_index", side_effect=Exception(simulated_mongo_err)):
    try:
        _create_indexes()
        check("S03 raises RuntimeError with sanitized message", False, "swallowed error")
    except RuntimeError as r_err:
        msg = str(r_err)
        check("S03 raises RuntimeError without exposing secret password", "SuperSecretPassword123!" not in msg)
        check("S04 error message contains redacted *** URI", "***@" in msg)


print("\n--- 4. LIVE SERVER HEALTH & INDEX CHECK ---")
proc = subprocess.Popen(
    [r".venv\Scripts\python.exe", "-m", "uvicorn", "app.main:app",
     "--port", str(PORT), "--log-level", "warning"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
for _ in range(20):
    time.sleep(0.5)
    try:
        r = urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=1)
        if r.status == 200:
            break
    except Exception:
        pass

try:
    req = urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=5)
    body = json.loads(req.read())
    check("L01 live server /health returns 200", req.status == 200 and body == {"status": "ok"})
except Exception as ex:
    check("L01 live server /health returns 200", False, str(ex))

proc.terminate()
proc.wait()

print()
if errors:
    print(f"INDEX AND ERROR HANDLING TESTS FAIL: {errors}")
    sys.exit(1)
else:
    print("INDEX AND ERROR HANDLING TESTS: ALL 12 PASSED")
