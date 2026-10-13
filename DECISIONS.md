# DECISIONS.md

### 1. Indexes
* `employees.emp_code` (unique): Prevents duplicate employees; ensures race-safe 409s.
* `employees.(department, joined_on)`: Accelerates department lookups and join-date headcount filtering.
* `attendance_logs.(emp_code, date)` (unique): Enforces one daily record per employee; ensures atomic 409s on concurrent punch-ins.
* `attendance_logs.(date DESC, emp_code ASC)`: Powers DB-side pagination and date-range index scans (`IXSCAN`).

### 2. Punch-in race
Both concurrent requests resolve to the same IST attendance date and attempt `insert_one`. The first succeeds (201). The second triggers MongoDB's unique index on `(emp_code, date)` raising `DuplicateKeyError`, intercepted as HTTP 409.

### 3. Ties
Standard competition ranking ($rank: 1, 2, 2, 4). The `limit` parameter filters `rank <= limit` rather than slicing array length, returning all tied employees.

### 4. Headcount
Headcount is computed from `employees` filtered by `joined_on <= date`. Attendance records are aggregated separately, ensuring employees with zero attendance records are preserved.

### 5. One thing to change at 100x data
Pre-aggregate daily attendance summaries during punch events or via background rollups, avoiding runtime cross-collection `$lookup` and `$densify` over tens of millions of records.
