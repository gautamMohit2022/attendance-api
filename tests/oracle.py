"""
Independent, deliberately naive reference implementation of the analytics rules (R5-R10) and a generator for an
awkward test dataset. It shares no code with app/main.py: it loops over plain Python lists and uses exact
Fraction arithmetic, so a disagreement means a bug in one of the two.
"""
import calendar
import datetime as dt
import random
from fractions import Fraction
from math import floor

UTC = dt.timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


def half_up(x: Fraction, places: int) -> float:
    scale = 10 ** places
    return floor(x * scale + Fraction(1, 2)) / scale


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def month_range(month: str):
    y, m = int(month[:4]), int(month[5:])
    return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])


def in_month(log, month):
    a, b = month_range(month)
    return a <= d(log["date"]) <= b


def present_value(log):
    if log["status"] not in PRESENCE:
        return Fraction(0)
    return Fraction(1, 2) if log.get("half_day") else Fraction(1)


def monthly(emp, logs, month):
    first, last = month_range(month)
    start = max(first, d(emp["joined_on"]))
    working_days = sum(1 for i in range((last - start).days + 1) if (start + dt.timedelta(days=i)).weekday() < 5) \
        if start <= last else 0
    mine = [l for l in logs if l["emp_code"] == emp["emp_code"] and in_month(l, month)]
    present = sum((present_value(l) for l in mine if d(l["date"]).weekday() < 5), Fraction(0))
    return {
        "emp_code": emp["emp_code"], "month": month, "working_days": working_days, "present_days": float(present),
        "leave_days": sum(1 for l in mine if l["status"] == "LEAVE"),
        "late_count": sum(1 for l in mine if l.get("late_minutes", 0) > 0),
        "total_late_minutes": sum(l.get("late_minutes", 0) for l in mine),
        "total_overtime_minutes": sum(l.get("overtime_minutes", 0) for l in mine),
        "attendance_pct": None if working_days == 0 else half_up(present / working_days * 100, 2),
    }


def summary(emps, logs, month, department=None):
    _, last = month_range(month)
    rows = {}
    for e in emps:
        if d(e["joined_on"]) > last or (department is not None and e["department"] != department):
            continue
        r = rows.setdefault(e["department"], {"department": e["department"], "headcount": 0, "present": Fraction(0),
                                              "hours": Fraction(0), "n_hours": 0, "late_count": 0, "late_total": 0,
                                              "leave_count": 0, "on_duty_count": 0})
        r["headcount"] += 1
        for l in logs:
            if l["emp_code"] != e["emp_code"] or not in_month(l, month):
                continue
            if d(l["date"]).weekday() < 5:
                r["present"] += present_value(l)
            if l.get("late_minutes", 0) > 0:
                r["late_count"] += 1
            r["late_total"] += l.get("late_minutes", 0)
            r["leave_count"] += l["status"] == "LEAVE"
            r["on_duty_count"] += l["status"] == "ON_DUTY"
            if l["status"] in PRESENCE and l.get("work_hours") is not None:
                r["hours"] += Fraction(str(l["work_hours"]))
                r["n_hours"] += 1
    out = []
    for name in sorted(rows):
        r = rows[name]
        out.append({"department": name, "headcount": r["headcount"], "present_days": float(r["present"]),
                    "avg_work_hours": half_up(r["hours"] / r["n_hours"], 2) if r["n_hours"] else None,
                    "late_count": r["late_count"], "total_late_minutes": r["late_total"],
                    "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"]})
    return {"month": month, "items": out}


def leaderboard(emps, logs, month, limit=10, department=None):
    by_code = {e["emp_code"]: e for e in emps}
    totals = {}
    for l in logs:
        if not in_month(l, month) or l["emp_code"] not in by_code:
            continue
        if department is not None and by_code[l["emp_code"]]["department"] != department:
            continue
        t = totals.setdefault(l["emp_code"], [0, 0])
        t[0] += l.get("late_minutes", 0)
        t[1] += l.get("late_minutes", 0) > 0
    rows = [(c, t[0], t[1]) for c, t in totals.items() if t[0] > 0]
    rows.sort(key=lambda r: (-r[1], r[0]))
    items = []
    for i, (code, total, cnt) in enumerate(rows):
        rank = 1 + sum(1 for r in rows if r[1] > total)
        if rank <= limit:
            e = by_code[code]
            items.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                          "total_late_minutes": total, "late_count": cnt})
    return {"month": month, "items": items}


def trend(emps, logs, department, from_day, to_day):
    team = [e for e in emps if e["department"] == department]
    codes = {e["emp_code"] for e in team}
    start, end = d(from_day), d(to_day)
    rows, rates = [], []
    for i in range((end - start).days + 1):
        day = start + dt.timedelta(days=i)
        hc = sum(1 for e in team if d(e["joined_on"]) <= day)
        todays = [l for l in logs if l["emp_code"] in codes and l["date"] == day.isoformat()]
        present = sum((present_value(l) for l in todays), Fraction(0))
        working = day.weekday() < 5
        rate = Fraction(str(half_up(present / hc, 4))) if working and hc > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({"date": day.isoformat(), "is_working_day": working, "headcount": hc,
                     "present_count": float(present), "late_count": sum(1 for l in todays if l.get("late_minutes", 0) > 0),
                     "attendance_rate": None if rate is None else float(rate),
                     "moving_avg_7d": half_up(sum(window) / len(window), 4) if window else None})
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- dataset
def at(date_str, hh, mm, ss=0):
    """IST wall-clock -> aware UTC datetime."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    y, m, dd = map(int, date_str.split("-"))
    return dt.datetime(y, m, dd, hh, mm, ss, tzinfo=ist).astimezone(UTC)


def employee(code, name, dept, joined, ss="09:30", se="18:30"):
    return {"emp_code": code, "name": name, "email": f"{name.lower()}@x.com", "department": dept,
            "shift_start": ss, "shift_end": se, "joined_on": joined, "created_at": dt.datetime(2026, 1, 1, tzinfo=UTC)}


def log(code, date, status="PRESENT", late=0, ot=0, hours=None, half=None, legacy=False):
    doc = {"emp_code": code, "date": date, "status": status,
           "punch_in": at(date, 9, 30) if status in PRESENCE else None,
           "punch_out": at(date, 18, 30) if hours is not None else None,
           "work_hours": hours, "late_minutes": late, "overtime_minutes": ot, "half_day": bool(half), "history": []}
    if legacy:
        doc.pop("half_day"), doc.pop("history")
    return doc


def build_dataset(seed=7, month="2026-07"):
    rng = random.Random(seed)
    emps = [
        employee("EMP0001", "Asha", "Engineering", "2026-01-05"),
        employee("EMP0002", "Bela", "Engineering", "2026-01-05"),
        employee("EMP0003", "Chen", "Engineering", "2026-07-15"),      # mid-month joiner
        employee("EMP0004", "Dev", "Engineering", "2026-08-10"),       # joins after the month: not counted
        employee("EMP0005", "Esha", "Engineering", "2026-02-01"),      # no logs at all
        employee("EMP0006", "Farid", "Sales", "2026-03-01"),
        employee("EMP0007", "Gita", "Sales", "2026-03-01"),
        employee("EMP0008", "Hari", "Sales", "2026-03-01"),
        employee("EMP0009", "Isha", "Sales", "2026-03-01"),
        employee("EMP0010", "Jay", "Ops", "2026-01-01", "22:00", "06:00"),  # overnight shift
        employee("EMP0011", "Kia", "Ops", "2026-07-01"),                  # joined on the 1st
        employee("EMP0012", "Lal", "Ghost Dept", "2026-09-01"),            # department with nobody joined yet
        employee("EMP0013", "Mia", "Sales", "2026-03-01"),                 # two employees tied at the top
        employee("EMP0014", "Neel", "Sales", "2026-03-01"),
        employee("EMP0015", "Om", "Sales", "2026-03-01"),
    ]
    logs = []
    first, last = month_range(month)
    for e in emps[:11]:
        if e["emp_code"] in ("EMP0005", "EMP0004"):
            continue
        for i in range((last - first).days + 1):
            day = first + dt.timedelta(days=i)
            if rng.random() < 0.2:
                continue  # no record at all on that day
            status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [6, 2, 1, 1, 1])[0]
            late = rng.choice([0, 0, 0, 11, 25, 40]) if status in PRESENCE else 0
            hours = round(rng.uniform(3, 10), 2) if status in PRESENCE and rng.random() < 0.85 else None
            half = hours is not None and hours < 4.5
            logs.append(log(e["emp_code"], day.isoformat(), status, late, rng.choice([0, 0, 35, 70]) if hours else 0,
                            hours, half, legacy=rng.random() < 0.1))
    # explicit awkward cases ------------------------------------------------------------------
    logs += [log("EMP0009", "2026-07-04", "PRESENT", 30, 0, 8.0),    # Saturday record: counts for late, not for present_days
             log("EMP0009", "2026-07-05", "WFH", 0, 0, 3.0, True),    # Sunday half day
             log("EMP9999", "2026-07-06", "PRESENT", 500, 0, 8.0),    # employee does not exist: ignored
             log("EMP0013", "2026-07-20", "PRESENT", 900, 0, 8.0),    # ranks 1, 1, 3 (tie, then rank skipped)
             log("EMP0014", "2026-07-20", "PRESENT", 900, 0, 8.0),
             log("EMP0015", "2026-07-20", "PRESENT", 899, 0, 8.0)]
    unique = {(l["emp_code"], l["date"]): l for l in logs}  # explicit cases replace random ones on the same day
    return emps, list(unique.values())
