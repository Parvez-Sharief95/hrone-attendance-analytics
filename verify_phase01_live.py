"""Live Phase 01 verification: starts uvicorn, runs HTTP checks, verifies indexes."""
import subprocess, time, urllib.request, json, os, sys
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

PORT = 8780
proc = subprocess.Popen(
    [r".venv\Scripts\python.exe", "-m", "uvicorn", "app.main:app",
     "--port", str(PORT), "--log-level", "warning"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)

# Wait up to 8 s for server to bind
for _ in range(16):
    time.sleep(0.5)
    try:
        urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=1)
        break
    except Exception:
        pass

def get(path):
    try:
        r = urllib.request.urlopen(f"http://localhost:{PORT}{path}", timeout=5)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as ex:
        return None, str(ex)

results = {}

# 1. GET /health
code, body = get("/health")
results["health"] = (code, body)
print(f"GET /health -> {code} {body}")

# 2. GET /employees (empty DB, page=1)
code, body = get("/employees")
results["employees"] = (code, body)
print(f"GET /employees -> {code} {body}")

# 3. GET /employees?page=0 → expect 422
code, body = get("/employees?page=0")
results["employees_page0"] = (code, body)
print(f"GET /employees?page=0 -> {code} (expect 422)")

proc.terminate()
proc.wait()

# 4. Verify indexes via PyMongo
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     serverSelectionTimeoutMS=4000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
try:
    emp_idx   = {i["name"]: i for i in db.employees.list_indexes()}
    att_idx   = {i["name"]: i for i in db.attendance_logs.list_indexes()}

    I1 = any(i.get("unique") and list(i["key"].keys()) == ["emp_code"]
             for i in emp_idx.values())
    I2 = any(i.get("unique") and list(i["key"].keys()) == ["emp_code", "date"]
             for i in att_idx.values())
    I3 = any(list(i["key"].keys()) == ["date", "emp_code"] and not i.get("unique")
             for i in att_idx.values())

    print(f"\nIndex I1 employees.emp_code unique:         {'PRESENT' if I1 else 'MISSING'}")
    print(f"Index I2 attendance (emp_code,date) unique: {'PRESENT' if I2 else 'MISSING'}")
    print(f"Index I3 attendance (date,emp_code):        {'PRESENT' if I3 else 'MISSING'}")
    index_ok = I1 and I2 and I3
except Exception as e:
    print(f"Index check FAILED: {e}")
    index_ok = False

# Summary
http_ok = (results["health"][0] == 200
           and results["employees"][0] == 200
           and results["employees_page0"][0] == 422)

print("\n--- SUMMARY ---")
print(f"HTTP checks: {'PASS' if http_ok else 'FAIL'}")
print(f"Index checks: {'PASS' if index_ok else 'FAIL'}")
print(f"Overall: {'PASS' if http_ok and index_ok else 'FAIL'}")
sys.exit(0 if (http_ok and index_ok) else 1)
