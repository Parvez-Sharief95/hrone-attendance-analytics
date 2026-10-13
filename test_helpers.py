"""Phase 01 helper logic verification — no MongoDB required."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

IST = timezone(timedelta(hours=5, minutes=30))


def half_up(v, p=2):
    q = "0." + "0" * p
    return float(Decimal(str(v)).quantize(Decimal(q), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in_ist, shift_start, att_date):
    h, m = map(int, shift_start.split(":"))
    base = datetime.fromisoformat(att_date)
    start_ist = base.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    elapsed_s = (punch_in_ist - start_ist).total_seconds()
    if elapsed_s > 600:
        return int(elapsed_s / 60)
    return 0


def attendance_date(punch_in_ist, shift_start, shift_end):
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        punch_min = punch_in_ist.hour * 60 + punch_in_ist.minute
        if punch_min < eh * 60 + em:
            return (punch_in_ist - timedelta(days=1)).date().isoformat()
    return punch_in_ist.date().isoformat()


def to_epoch_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)
    return dt.astimezone(IST)


def compute_work_hours(pi, po):
    total_s = (po - pi).total_seconds()
    return half_up(total_s / 3600, 2)


def compute_overtime(po_ist, shift_end, shift_start, att_date):
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    base_date = datetime.fromisoformat(att_date).date()
    end_date = base_date + timedelta(days=1) if is_overnight else base_date
    end_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    elapsed_m = int((po_ist - end_ist).total_seconds() / 60)
    return elapsed_m if elapsed_m >= 30 else 0


errors = []

# --- half_up (R4, R8) ---
cases_halfup = [
    (8.445, 2, 8.45),    # rounds UP at midpoint (Python round() would give 8.44)
    (8.444, 2, 8.44),
    (9.125, 2, 9.13),
    (3.5,   2, 3.5),
]
for v, p, expected in cases_halfup:
    got = half_up(v, p)
    status = "OK" if got == expected else f"FAIL: expected {expected} got {got}"
    print(f"half_up({v},{p}) = {got}  {status}")
    if got != expected:
        errors.append(f"half_up({v},{p})")

# --- compute_late_minutes (R2) — openapi examples ---
cases_late = [
    # shift 09:30; punch at 09:40:00 -> 0 (at grace boundary, not past it)
    (datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST), "09:30", "2026-07-06", 0),
    # punch at 09:40:01 -> 10 (D10 fixed: elapsed=601s > 600 -> int(601/60)=10)
    (datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST), "09:30", "2026-07-06", 10),
    # punch at 10:15:59 -> 45
    (datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST), "09:30", "2026-07-06", 45),
    # inside grace: 09:39:59 -> 0
    (datetime(2026, 7, 6, 9, 39, 59, tzinfo=IST), "09:30", "2026-07-06", 0),
]
for pi_ist, ss, ad, expected in cases_late:
    got = compute_late_minutes(pi_ist, ss, ad)
    status = "OK" if got == expected else f"FAIL: expected {expected} got {got}"
    print(f"late_minutes({pi_ist.time()}, shift={ss}) = {got}  {status}")
    if got != expected:
        errors.append(f"late_minutes({pi_ist.time()})")

# --- attendance_date (R1 overnight) ---
cases_date = [
    # Normal shift: punch 09:40 IST 2026-07-06 -> 2026-07-06
    (datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST), "09:30", "18:30", "2026-07-06"),
    # Overnight shift 22:00-06:00: punch 21:55 IST 2026-07-06 -> 2026-07-06
    (datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST), "22:00", "06:00", "2026-07-06"),
    # Overnight shift 22:00-06:00: punch 01:00 IST 2026-07-07 (< 06:00) -> 2026-07-06
    (datetime(2026, 7, 7, 1, 0, 0, tzinfo=IST), "22:00", "06:00", "2026-07-06"),
    # Overnight shift 22:00-06:00: punch 22:15 IST 2026-07-07 (>= 06:00) -> 2026-07-07
    (datetime(2026, 7, 7, 22, 15, 0, tzinfo=IST), "22:00", "06:00", "2026-07-07"),
    # Overnight shift: exactly at shift_end 06:00 -> NOT previous day (strictly before)
    (datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST), "22:00", "06:00", "2026-07-07"),
]
for pi_ist, ss, se, expected in cases_date:
    got = attendance_date(pi_ist, ss, se)
    status = "OK" if got == expected else f"FAIL: expected {expected} got {got}"
    print(f"att_date({pi_ist.date()} {pi_ist.time()}, {ss}-{se}) = {got}  {status}")
    if got != expected:
        errors.append(f"att_date({pi_ist.time()})")

# --- to_epoch_ms / from_epoch_ms round-trip ---
# DATA_MODEL sample 1 punch_in: 2026-07-06T03:58:00Z = 09:28 IST
naive_utc = datetime(2026, 7, 6, 3, 58, 0)
ms = to_epoch_ms(naive_utc)
back = from_epoch_ms(ms)
ok = back.hour == 9 and back.minute == 28 and back.second == 0
print(f"epoch_ms({naive_utc}) -> {ms} -> IST {back.time()}  {'OK' if ok else 'FAIL'}")
if not ok:
    errors.append("epoch_ms round-trip")

# --- compute_work_hours (R4) ---
# DATA_MODEL sample 1: 03:58-13:05 UTC = 9h 7min = 9.1167h -> half_up 9.12 expected: 9.12
pi = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
po = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
wh = compute_work_hours(pi, po)
ok = wh == 9.12
print(f"work_hours(sample1) = {wh}  {'OK' if ok else f'FAIL: expected 9.12'}")
if not ok:
    errors.append("work_hours sample1")

# half_day: work_hours < 4.50
# DATA_MODEL sample 4: 3.5h -> half_day True
assert half_up(3.5, 2) < 4.50
print("half_day(3.5 < 4.50) = True  OK")

# --- compute_overtime (R3) ---
# DATA_MODEL sample 2: shift 09:30-18:30, punch_out 19:10 IST -> elapsed=40min >= 30 -> 40
pi2 = datetime(2026, 7, 6, 10, 5, 0, tzinfo=IST)
po2 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
ot = compute_overtime(po2, "18:30", "09:30", "2026-07-06")
ok = ot == 40
print(f"overtime(sample2, 40min) = {ot}  {'OK' if ok else 'FAIL: expected 40'}")
if not ok:
    errors.append("overtime sample2")

# Less than 30min: no overtime
po3 = datetime(2026, 7, 6, 18, 55, 0, tzinfo=IST)
ot3 = compute_overtime(po3, "18:30", "09:30", "2026-07-06")
ok3 = ot3 == 0
print(f"overtime(<30min) = {ot3}  {'OK' if ok3 else 'FAIL: expected 0'}")
if not ok3:
    errors.append("overtime <30min")

# DATA_MODEL sample 7 overnight: shift 22:00-06:00, punch_out 06:40 IST on 07-07 -> 40min -> 40
po_night = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
ot_night = compute_overtime(po_night, "06:00", "22:00", "2026-07-06")
ok_night = ot_night == 40
print(f"overtime(overnight sample7, 40min) = {ot_night}  {'OK' if ok_night else 'FAIL: expected 40'}")
if not ok_night:
    errors.append("overtime overnight")

print()
if errors:
    print(f"FAILED: {errors}")
    raise SystemExit(1)
else:
    print("ALL HELPER TESTS PASSED")
