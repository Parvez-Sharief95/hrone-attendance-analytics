"""Phase 03 live tests: all four analytics endpoints. ASCII-only output."""
import subprocess, time, urllib.request, urllib.error, json, os, sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
PORT = 8795
IST = timezone(timedelta(hours=5, minutes=30))
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=4000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

MONTH = "2026-07"
EMP1 = {"emp_code": "EMP9911", "name": "Alice", "email": "a@x.com", "department": "Engineering",
         "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01",
         "created_at": datetime.utcnow()}
EMP2 = {"emp_code": "EMP9912", "name": "Bob",   "email": "b@x.com", "department": "Engineering",
         "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01",
         "created_at": datetime.utcnow()}
EMP3 = {"emp_code": "EMP9913", "name": "Carol", "email": "c@x.com", "department": "Sales",
         "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01",
         "created_at": datetime.utcnow()}

def cleanup():
    for e in [EMP1, EMP2, EMP3]:
        db.employees.delete_many({"emp_code": e["emp_code"]})
        db.attendance_logs.delete_many({"emp_code": e["emp_code"]})

def utc(y,mo,d,h,mi,s=0):
    return datetime(y,mo,d,h,mi,s)

cleanup()
db.employees.insert_many([EMP1, EMP2, EMP3])

# Seed attendance for July 2026 (Mon=Jul6, Tue=Jul7)
# EMP9911: Mon Jul6 09:40:01 IST -> late 10 min; works until 19:10 -> OT 40; WF=9.50
# EMP9911: Tue Jul7 09:30:00 IST -> on time; works until 18:30 -> OT 0; WH=9.00
# EMP9912: Mon Jul6 10:00:00 IST -> late 30 min; works until 18:30 -> OT 0; WH=8.50
# EMP9913 (Sales): Mon Jul6 LEAVE
logs = [
    {"emp_code":"EMP9911","date":"2026-07-06","status":"PRESENT",
     "punch_in": utc(2026,7,6,4,10,1), "punch_out": utc(2026,7,6,13,40,0),
     "work_hours":9.50,"late_minutes":10,"overtime_minutes":40,"half_day":False,"history":[]},
    {"emp_code":"EMP9911","date":"2026-07-07","status":"PRESENT",
     "punch_in": utc(2026,7,7,4,0,0), "punch_out": utc(2026,7,7,13,0,0),
     "work_hours":9.00,"late_minutes":0,"overtime_minutes":0,"half_day":False,"history":[]},
    {"emp_code":"EMP9912","date":"2026-07-06","status":"PRESENT",
     "punch_in": utc(2026,7,6,4,30,0), "punch_out": utc(2026,7,6,13,0,0),
     "work_hours":8.50,"late_minutes":30,"overtime_minutes":0,"half_day":False,"history":[]},
    {"emp_code":"EMP9913","date":"2026-07-06","status":"LEAVE",
     "punch_in": None,"punch_out": None,
     "work_hours":None,"late_minutes":0,"overtime_minutes":0,"half_day":False,"history":[]},
]
db.attendance_logs.insert_many(logs)

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

# --- T01 employee_monthly unknown emp -> 404
code, body = get(f"/analytics/employees/EMP0000/monthly?month={MONTH}")
check("T01 employee_monthly unknown emp -> 404", code == 404)

# --- T02 employee_monthly bad month format -> 422
code, body = get(f"/analytics/employees/EMP9911/monthly?month=2026-13")
check("T02 employee_monthly bad month -> 422", code == 422)

# --- T03 employee_monthly EMP9911 correct values
code, body = get(f"/analytics/employees/EMP9911/monthly?month={MONTH}")
check("T03 employee_monthly -> 200", code == 200)
if code == 200:
    print(f"  working_days={body['working_days']} present_days={body['present_days']} "
          f"late_count={body['late_count']} total_late_minutes={body['total_late_minutes']} "
          f"total_overtime_minutes={body['total_overtime_minutes']} attendance_pct={body['attendance_pct']}")
    # Jul2026 has 23 working days; EMP9911 joined Jul1 -> 23 working days
    check("T03a working_days=23", body["working_days"] == 23, f"got {body['working_days']}")
    # 2 Mon-Fri records, both PRESENT, no half_day -> present_days=2.0
    check("T03b present_days=2.0", body["present_days"] == 2.0, f"got {body['present_days']}")
    # 1 late record (Jul6)
    check("T03c late_count=1", body["late_count"] == 1, f"got {body['late_count']}")
    check("T03d total_late_minutes=10", body["total_late_minutes"] == 10, f"got {body['total_late_minutes']}")
    check("T03e total_overtime_minutes=40", body["total_overtime_minutes"] == 40, f"got {body['total_overtime_minutes']}")
    # attendance_pct = 2/23*100 = 8.6957... -> half_up 2dp = 8.70
    check("T03f attendance_pct=8.70", body["attendance_pct"] == 8.70, f"got {body['attendance_pct']}")

# --- T04 department_summary Engineering
code, body = get(f"/analytics/departments/summary?month={MONTH}&department=Engineering")
check("T04 dept_summary -> 200", code == 200)
if code == 200 and body.get("items"):
    item = body["items"][0]
    print(f"  dept={item['department']} headcount={item['headcount']} "
          f"present_days={item['present_days']} avg_wh={item['avg_work_hours']} "
          f"late_count={item['late_count']} total_late_min={item['total_late_minutes']}")
    check("T04a department=Engineering", item["department"] == "Engineering")
    check("T04b headcount=2", item["headcount"] == 2, f"got {item['headcount']}")
    # EMP9911: 2 present Mon-Fri; EMP9912: 1 present Mon-Fri -> 3.0
    check("T04c present_days=3.0", item["present_days"] == 3.0, f"got {item['present_days']}")
    # avg_wh: (9.50+9.00+8.50)/3 = 27/3 = 9.00
    check("T04d avg_work_hours=9.0", item["avg_work_hours"] == 9.0, f"got {item['avg_work_hours']}")
    # late_count: EMP9911 Jul6(10min) + EMP9912 Jul6(30min) = 2
    check("T04e late_count=2", item["late_count"] == 2, f"got {item['late_count']}")
    check("T04f total_late_minutes=40", item["total_late_minutes"] == 40, f"got {item['total_late_minutes']}")
    check("T04g leave_count=0", item["leave_count"] == 0)
else:
    errors.append("T04 no items")

# --- T05 department_summary Sales headcount and leave_count
code, body = get(f"/analytics/departments/summary?month={MONTH}&department=Sales")
check("T05 dept_summary Sales -> 200", code == 200)
if code == 200 and body.get("items"):
    item = body["items"][0]
    check("T05a headcount=1", item["headcount"] == 1, f"got {item['headcount']}")
    check("T05b leave_count=1", item["leave_count"] == 1, f"got {item['leave_count']}")
    check("T05c present_days=0.0", item["present_days"] == 0.0, f"got {item['present_days']}")

# --- T06 late_leaderboard Engineering
code, body = get(f"/analytics/leaderboard/late?month={MONTH}&department=Engineering&limit=5")
check("T06 leaderboard -> 200", code == 200)
if code == 200:
    items = body.get("items", [])
    print(f"  leaderboard items: {[(i['emp_code'], i['rank'], i['total_late_minutes']) for i in items]}")
    check("T06a len=2", len(items) == 2, f"got {len(items)}")
    # EMP9912 has 30 min late -> rank 1; EMP9911 has 10 min late -> rank 2
    check("T06b EMP9912 rank=1", items[0]["emp_code"] == "EMP9912" and items[0]["rank"] == 1,
          f"got {items[0]}")
    check("T06c EMP9911 rank=2", items[1]["emp_code"] == "EMP9911" and items[1]["rank"] == 2,
          f"got {items[1]}")
    check("T06d EMP9912 total=30", items[0]["total_late_minutes"] == 30)
    check("T06e EMP9911 total=10", items[1]["total_late_minutes"] == 10)

# --- T07 department_trend unknown dept -> 404
code, body = get("/analytics/departments/UNKNOWN/trend?from=2026-07-06&to=2026-07-07")
check("T07 trend unknown dept -> 404", code == 404)

# --- T08 department_trend to < from -> 422
code, body = get("/analytics/departments/Engineering/trend?from=2026-07-08&to=2026-07-06")
check("T08 trend to<from -> 422", code == 422)

# --- T09 department_trend > 92 days -> 422
code, body = get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-10")
check("T09 trend >92 days -> 422", code == 422)

# --- T10 department_trend Engineering 2 days (Jul6 Mon, Jul7 Tue)
code, body = get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-07")
check("T10 trend -> 200", code == 200)
if code == 200:
    items = body.get("items", [])
    print(f"  trend items: {[{'date':i['date'],'hc':i['headcount'],'pc':i['present_count'],'rate':i['attendance_rate'],'mavg':i['moving_avg_7d']} for i in items]}")
    check("T10a len=2", len(items) == 2, f"got {len(items)}")
    if len(items) == 2:
        d0 = items[0]  # Jul 6 Mon
        d1 = items[1]  # Jul 7 Tue
        check("T10b Jul6 is_working=True", d0["is_working_day"])
        check("T10c Jul6 headcount=2", d0["headcount"] == 2, f"got {d0['headcount']}")
        # Jul6: EMP9911 PRESENT + EMP9912 PRESENT = 2.0; rate = 2/2 = 1.0
        check("T10d Jul6 present_count=2.0", d0["present_count"] == 2.0, f"got {d0['present_count']}")
        check("T10e Jul6 attendance_rate=1.0", d0["attendance_rate"] == 1.0, f"got {d0['attendance_rate']}")
        # moving_avg_7d after Jul6 = 1.0
        check("T10f Jul6 moving_avg=1.0", d0["moving_avg_7d"] == 1.0)
        check("T10g Jul7 is_working=True", d1["is_working_day"])
        # Jul7: only EMP9911 present=1; rate = 1/2 = 0.5
        check("T10h Jul7 attendance_rate=0.5", d1["attendance_rate"] == 0.5, f"got {d1['attendance_rate']}")
        # moving_avg_7d after Jul7 = (1.0+0.5)/2 = 0.75
        check("T10i Jul7 moving_avg=0.75", d1["moving_avg_7d"] == 0.75, f"got {d1['moving_avg_7d']}")

# --- T11 trend gap-filling: request spans days with no logs (weekends)
code, body = get("/analytics/departments/Engineering/trend?from=2026-07-04&to=2026-07-07")
check("T11 trend gap-fill 4 days -> 200", code == 200)
if code == 200:
    items = body.get("items", [])
    check("T11a len=4", len(items) == 4, f"got {len(items)}")
    if len(items) >= 2:
        # Jul4=Sat, Jul5=Sun -> not working, attendance_rate=null
        check("T11b Jul4 is_working=False", not items[0]["is_working_day"])
        check("T11c Jul5 is_working=False", not items[1]["is_working_day"])
        check("T11d Jul4 rate=null", items[0]["attendance_rate"] is None)

proc.terminate()
proc.wait()
cleanup()

print()
if errors:
    print(f"PHASE 03 FAIL: {errors}")
    sys.exit(1)
else:
    print("PHASE 03 LIVE TESTS: ALL PASSED")
