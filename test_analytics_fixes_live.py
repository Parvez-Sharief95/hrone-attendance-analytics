"""Strengthened focused live tests for analytics fixes:
1. Late leaderboard: verify MongoDB-side competition ranking ($rank in $setWindowFields)
   and cutoff ($match rank <= limit) directly in MongoDB pipeline and via live endpoint.
2. Department trend populated: verify MongoDB-side headcount ($lookup), attendance_rate ($divide),
   and 7-calendar-day moving average ($setWindowFields) via live endpoint.
3. Department trend empty: verify empty calendar rows generated entirely in MongoDB via
   database-level aggregation ($documents + $range + $lookup + $setWindowFields),
   without Python loops or fallback calculations, via live endpoint.
"""
import subprocess, time, urllib.request, urllib.error, json, os, sys
from datetime import datetime, timezone
from dotenv import load_dotenv
import pymongo.collection, pymongo.database
from pymongo import MongoClient

# Ensure app can be imported
sys.path.insert(0, ".")
from app.main import late_leaderboard, department_trend

load_dotenv()
PORT = 8799
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=4000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

TEST_EMPS = [f"EMP700{i}" for i in range(1, 8)]

def cleanup():
    db.employees.delete_many({"emp_code": {"$in": TEST_EMPS}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_EMPS}})

cleanup()

# Seed employees for Leaderboard test (EMP7001 to EMP7005 in dept "AnalyticsQA")
for i in range(1, 6):
    db.employees.insert_one({
        "emp_code": f"EMP700{i}",
        "name": f"Tester {i}",
        "email": f"t{i}@qa.com",
        "department": "AnalyticsQA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
        "created_at": datetime.now(timezone.utc),
    })

# EMP7001: 60, EMP7002: 40, EMP7003: 40 (tie), EMP7004: 20, EMP7005: 10
late_data = [
    ("EMP7001", 60),
    ("EMP7002", 40),
    ("EMP7003", 40),
    ("EMP7004", 20),
    ("EMP7005", 10),
]
for emp, late_m in late_data:
    db.attendance_logs.insert_one({
        "emp_code": emp,
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0),
        "punch_out": datetime(2026, 7, 6, 13, 0),
        "work_hours": 9.0,
        "late_minutes": late_m,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

# EMP7006 in "EmptyDept" (joined 2026-07-01, zero attendance logs)
db.employees.insert_one({
    "emp_code": "EMP7006",
    "name": "Empty Emp",
    "email": "empty@qa.com",
    "department": "EmptyDept",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-07-01",
    "created_at": datetime.now(timezone.utc),
})

# EMP7007 in "RollDept" (for rolling calendar window test, joined 2026-07-01)
db.employees.insert_one({
    "emp_code": "EMP7007",
    "name": "Roll Emp",
    "email": "roll@qa.com",
    "department": "RollDept",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-07-01",
    "created_at": datetime.now(timezone.utc),
})

# RollDept logs (Mon 2026-07-06 to Mon 2026-07-13):
db.attendance_logs.insert_many([
    {"emp_code": "EMP7007", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 4, 0), "punch_out": datetime(2026, 7, 6, 13, 0), "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
    {"emp_code": "EMP7007", "date": "2026-07-07", "status": "PRESENT", "punch_in": datetime(2026, 7, 7, 4, 0), "punch_out": datetime(2026, 7, 7, 7, 30), "work_hours": 3.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True, "history": []},
    {"emp_code": "EMP7007", "date": "2026-07-08", "status": "PRESENT", "punch_in": datetime(2026, 7, 8, 4, 0), "punch_out": datetime(2026, 7, 8, 7, 30), "work_hours": 3.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True, "history": []},
    {"emp_code": "EMP7007", "date": "2026-07-09", "status": "PRESENT", "punch_in": datetime(2026, 7, 9, 4, 0), "punch_out": datetime(2026, 7, 9, 13, 0), "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
    {"emp_code": "EMP7007", "date": "2026-07-10", "status": "ABSENT", "punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
    {"emp_code": "EMP7007", "date": "2026-07-13", "status": "PRESENT", "punch_in": datetime(2026, 7, 13, 4, 0), "punch_out": datetime(2026, 7, 13, 13, 0), "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
])

# Start server
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

print("--- 1. VERIFY ENDPOINT USES MONGODB AGGREGATION FOR LEADERBOARD RANKING ---")
# Spy on PyMongo collection aggregation to verify endpoint executes MongoDB pipeline
coll_calls = []
orig_coll_agg = pymongo.collection.Collection.aggregate
def spy_coll_agg(self, pipe, *args, **kwargs):
    coll_calls.append((self.name, pipe))
    return orig_coll_agg(self, pipe, *args, **kwargs)
pymongo.collection.Collection.aggregate = spy_coll_agg

try:
    direct_res = late_leaderboard(month="2026-07", limit=2, department="AnalyticsQA")
    check("M01 late_leaderboard called Collection.aggregate on attendance_logs",
          len(coll_calls) > 0 and coll_calls[-1][0] == "attendance_logs")
    pipe_used = coll_calls[-1][1]
    stage_names = [list(s.keys())[0] for s in pipe_used]
    check("M02 pipeline contains $setWindowFields", "$setWindowFields" in stage_names)
    check("M03 pipeline contains $match for cutoff", stage_names.count("$match") >= 2)
    swf_stage = [s["$setWindowFields"] for s in pipe_used if "$setWindowFields" in s][0]
    check("M04 $setWindowFields specifies $rank output", "rank" in swf_stage.get("output", {}) and "$rank" in swf_stage["output"]["rank"])
    cutoff_stage = [s["$match"] for s in pipe_used if "$match" in s and "rank" in s["$match"]][0]
    check("M05 $match specifies rank <= limit cutoff in MongoDB", cutoff_stage == {"rank": {"$lte": 2}})
finally:
    pymongo.collection.Collection.aggregate = orig_coll_agg

# Verify HTTP Live Endpoint results for leaderboard:
code, res = get("/analytics/leaderboard/late?month=2026-07&department=AnalyticsQA&limit=2")
items = res.get("items", [])
check("L01 HTTP status 200", code == 200)
check("L02 limit=2 returns 3 rows due to tie at rank 2", len(items) == 3, f"got {len(items)}")
check("L03 ranks are [1, 2, 2]", [i["rank"] for i in items] == [1, 2, 2])
check("L04 tie-break order is EMP7002, EMP7003 (emp_code asc)", [i["emp_code"] for i in items] == ["EMP7001", "EMP7002", "EMP7003"])
# limit=3 still returns 3 rows (standard competition ranking skips rank 3)
code, res = get("/analytics/leaderboard/late?month=2026-07&department=AnalyticsQA&limit=3")
items = res.get("items", [])
check("L05 limit=3 returns 3 rows (rank 3 skipped)", len(items) == 3)
# limit=4 returns 4 rows [1, 2, 2, 4]
code, res = get("/analytics/leaderboard/late?month=2026-07&department=AnalyticsQA&limit=4")
items = res.get("items", [])
check("L06 limit=4 returns 4 rows [1, 2, 2, 4]", [i["rank"] for i in items] == [1, 2, 2, 4])

# Explain verification for late_leaderboard
code, exp_res = get("/admin/explain/late_leaderboard?month=2026-07&department=AnalyticsQA&limit=2")
exp_str = json.dumps(exp_res)
check("L07 explain late_leaderboard IXSCAN and no COLLSCAN", ("IXSCAN" in exp_str or "COUNT_SCAN" in exp_str) and "COLLSCAN" not in exp_str)


print("\n--- 2. VERIFY ENDPOINT USES MONGODB AGGREGATION FOR POPULATED TREND ---")
coll_calls = []
pymongo.collection.Collection.aggregate = spy_coll_agg
try:
    trend_direct = department_trend(department="RollDept", from_date="2026-07-06", to_date="2026-07-13")
    check("P01 populated trend called Collection.aggregate on attendance_logs",
          len(coll_calls) > 0 and coll_calls[-1][0] == "attendance_logs")
    pipe_trend = coll_calls[-1][1]
    stages_trend = [list(s.keys())[0] for s in pipe_trend]
    check("P02 pipeline contains $densify", "$densify" in stages_trend)
    check("P03 pipeline contains correlated $lookup for headcount", "$lookup" in stages_trend)
    check("P04 pipeline contains $setWindowFields for moving average", "$setWindowFields" in stages_trend)
finally:
    pymongo.collection.Collection.aggregate = orig_coll_agg

# Live HTTP verification for populated trend:
code, res = get("/analytics/departments/RollDept/trend?from=2026-07-06&to=2026-07-13")
items = res.get("items", [])
check("P05 HTTP trend returns 200 with 8 calendar days", code == 200 and len(items) == 8)
# Day 8 rate=1.0, mavg=0.6 (Day 1 rolled out after 7 calendar days in MongoDB)
check("P06 Day 8 rate=1.0 and moving_avg_7d=0.6", items[7]["attendance_rate"] == 1.0 and items[7]["moving_avg_7d"] == 0.6)

# Explain verification for department_trend
code, exp_trend = get("/admin/explain/department_trend?department=RollDept&from=2026-07-06&to=2026-07-13")
exp_trend_str = json.dumps(exp_trend)
check("P07 explain trend IXSCAN and no COLLSCAN", ("IXSCAN" in exp_trend_str or "COUNT_SCAN" in exp_trend_str) and "COLLSCAN" not in exp_trend_str)


print("\n--- 3. VERIFY ENDPOINT GENERATES EMPTY TREND ROWS ENTIRELY IN MONGODB ---")
db_calls = []
orig_db_agg = pymongo.database.Database.aggregate
def spy_db_agg(self, pipe, *args, **kwargs):
    db_calls.append(pipe)
    return orig_db_agg(self, pipe, *args, **kwargs)
pymongo.database.Database.aggregate = spy_db_agg

try:
    empty_direct = department_trend(department="EmptyDept", from_date="2026-07-06", to_date="2026-07-10")
    check("E01 empty trend called Database.aggregate entirely in MongoDB", len(db_calls) > 0)
    empty_pipe_used = db_calls[-1]
    empty_stages = [list(s.keys())[0] for s in empty_pipe_used]
    check("E02 empty pipeline starts with $documents", empty_stages[0] == "$documents")
    check("E03 empty pipeline uses $range to generate dates in MongoDB",
          any("$range" in str(s) for s in empty_pipe_used))
    check("E04 empty pipeline uses correlated $lookup for headcount", "$lookup" in empty_stages)
    check("E05 empty pipeline uses $setWindowFields for moving average", "$setWindowFields" in empty_stages)
    check("E06 empty trend returned 5 days from MongoDB aggregation", len(empty_direct["items"]) == 5)
finally:
    pymongo.database.Database.aggregate = orig_db_agg

# Live HTTP verification for empty department trend:
code, res = get("/analytics/departments/EmptyDept/trend?from=2026-07-06&to=2026-07-10")
items = res.get("items", [])
check("E07 HTTP empty trend returns 200 with 5 days", code == 200 and len(items) == 5)
check("E08 all present_count=0.0 and late_count=0", all(i["present_count"] == 0.0 and i["late_count"] == 0 for i in items))
check("E09 all attendance_rate=0.0 and moving_avg_7d=0.0 on working days", all(i["attendance_rate"] == 0.0 and i["moving_avg_7d"] == 0.0 for i in items))
check("E10 all headcount=1 and is_working_day=True", all(i["headcount"] == 1 and i["is_working_day"] is True for i in items))

# Date range before employee joined (headcount=0) -> rate and moving_avg must be null
code, res = get("/analytics/departments/EmptyDept/trend?from=2026-06-01&to=2026-06-03")
items = res.get("items", [])
check("E11 range before joined_on returns 3 days with headcount=0", len(items) == 3 and all(i["headcount"] == 0 for i in items))
check("E12 attendance_rate=None and moving_avg_7d=None when headcount=0", all(i["attendance_rate"] is None and i["moving_avg_7d"] is None for i in items))

proc.terminate()
proc.wait()
cleanup()

print()
if errors:
    print(f"ANALYTICS FIXES FAIL: {errors}")
    sys.exit(1)
else:
    print("STRENGTHENED ANALYTICS TESTS: ALL 26 PASSED")
