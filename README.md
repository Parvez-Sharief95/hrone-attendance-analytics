# Employee Attendance & Analytics API

A FastAPI and MongoDB service implementing the HROne Employee Attendance & Analytics API assignment according to `openapi.yaml` and `DATA_MODEL.md`.

## Features
- **Core Operations**: Employee creation, pagination, filtering, punch-in, punch-out, and manual regularization with full audit history (`history`).
- **Concurrency & Race Handling**:
  - `POST /employees`: Atomic concurrency gate via unique index on `employees.emp_code`; concurrent identical creations yield one 201 and 409 for duplicates.
  - `POST /attendance/punch-in`: Atomic unique compound index on `attendance_logs.(emp_code, date)` guarantees exactly one daily record per employee; concurrent requests raise 409 Conflict.
  - `POST /attendance/punch-out`: Atomic `find_one_and_update` on eligible open record (`{"emp_code": emp_code, "punch_out": None}`) ensures concurrent requests cannot both succeed; already-closed records raise 409 Conflict.
- **Time & Shift Rules (R1–R5)**: UTC/IST conversions, whole-second truncation, overnight shift resolution, 10-minute late grace window, half-up rounding for work hours, and half-day presence flags.
- **Analytics Aggregations (R6–R9)**: MongoDB aggregation pipelines for monthly attendance summaries, department summaries with zero-log headcount retention, late-comer leaderboard with competition ranking, and 7-calendar-day rolling attendance trend with `$densify` gap filling (and database-level date generation when zero logs match).
- **Query Optimization & Explain (Phase 04)**: Idempotent startup indexes (`employees.emp_code`, `attendance_logs.(emp_code, date)`, `attendance_logs.(date DESC, emp_code ASC)`, `employees.(department, joined_on)`) ensuring indexed scans (`IXSCAN`) and zero collection scans (`COLLSCAN`), with secret-sanitizing failure reporting.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (MongoDB 7.0 / 9.0 supported)

## Setup & Running
```bash
# Setup virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the application
uvicorn app.main:app --port 8000
```

Configuration is loaded from environment variables (`MONGO_URI`, `MONGO_DB`), with fallback to local defaults (`mongodb://localhost:27017`, `attendance_db`).

## Test Suites
Complete test suites covering application endpoints, data integrity, and business logic:
- `python test_helpers.py`: Core date, shift, rounding, and late helper verification.
- `python verify_phase01_live.py`: Baseline Phase 01 HTTP contract and index verification.
- `python test_phase02_live.py`: Punch-out, regularization, duplicate punch-out handling, and history tracking.
- `python test_phase03_live.py`: Analytics endpoints (monthly, department summary, leaderboard, trend).
- `python test_phase04_live.py`: Admin explain endpoint and MongoDB execution plans (IXSCAN vs COLLSCAN).
- `python test_audit_fixes_live.py`: Eligible open punch-out selection, epoch ms float/string validation, calendar date validation.
- `python test_analytics_fixes_live.py`: MongoDB-side leaderboard competition ranking/cutoff, populated trends, and empty trends.
- `python test_index_error_handling_live.py`: Idempotent index creation, failure handling, and secret redaction.
