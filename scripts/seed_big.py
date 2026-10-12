"""
Generate a large, awkward dataset (about 100,000 attendance records) into a SCRATCH database.

    MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_scratch python scripts/seed_big.py

WARNING: drops and refills the `employees` and `attendance_logs` collections of MONGO_DB. Never point it at a
database you care about. Start the app afterwards (or restart it) so that its indexes are created.
"""
import datetime as dt
import os
import random
import sys

from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
import oracle  # noqa: E402


def build(n_employees=2000, days=70, seed=11):
    rng = random.Random(seed)
    depts = [f"Dept{i:02d}" for i in range(20)]
    emps = []
    for i in range(1, n_employees + 1):
        overnight = rng.random() < 0.05
        emps.append(oracle.employee(f"EMP{i:04d}", f"Name{i}", rng.choice(depts),
                                    (dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 200))).isoformat(),
                                    "22:00" if overnight else "09:30", "06:00" if overnight else "18:30"))
    logs, start = [], dt.date(2026, 6, 1)
    for e in emps:
        for k in range(days):
            day = start + dt.timedelta(days=k)
            if day < dt.date.fromisoformat(e["joined_on"]) or rng.random() < 0.15:
                continue
            status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [6, 2, 1, 1, 1])[0]
            present = status in oracle.PRESENCE
            hours = round(rng.uniform(3, 10), 2) if present and rng.random() < 0.9 else None
            logs.append(oracle.log(e["emp_code"], day.isoformat(), status,
                                   rng.choice([0, 0, 0, 11, 25, 40]) if present else 0,
                                   rng.choice([0, 0, 35, 70]) if hours else 0, hours, hours is not None and hours < 4.5))
    return emps, logs


if __name__ == "__main__":
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
    emps, logs = build()
    db.employees.drop(); db.attendance_logs.drop()
    db.employees.insert_many(emps); db.attendance_logs.insert_many(logs)
    print(f"seeded {len(emps)} employees and {len(logs)} attendance records into {db.name}")
