"""
End-to-end check of a RUNNING server against a real MongoDB. This is the check that matters most:
the offline unit tests use a small stand-in aggregation interpreter, this script uses the real database.

    # 1. scratch DB + server (indexes are created at startup)
    export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_scratch
    uvicorn app.main:app --port 8000 &
    # 2. run
    pip install -r requirements-dev.txt
    python scripts/e2e_check.py --base http://localhost:8000 [--big]

WARNING: it DROPS and refills `employees` and `attendance_logs` in MONGO_DB. Use a scratch database.
It checks: analytics vs the oracle, race conditions (parallel requests), explain plans (IXSCAN, no COLLSCAN),
and latency on ~100k records (--big).
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
import time

import httpx
from pymongo import MongoClient

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
sys.path.insert(0, HERE)
import oracle  # noqa: E402
import seed_big  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        failures.append(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--big", action="store_true", help="also load ~100k records and time the endpoints")
    args = ap.parse_args()
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
    http = httpx.Client(base_url=args.base, timeout=60)
    month = "2026-07"

    # ------------------------------------------------------------------ analytics vs oracle (awkward dataset)
    emps, logs = oracle.build_dataset()
    db.employees.delete_many({}); db.attendance_logs.delete_many({})
    db.employees.insert_many([dict(e) for e in emps]); db.attendance_logs.insert_many([dict(l) for l in logs])
    check("health", http.get("/health").status_code == 200)
    bad = [e["emp_code"] for e in emps
           if http.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": month}).json() != oracle.monthly(e, logs, month)]
    check("employee monthly == oracle (all employees)", not bad, bad)
    for m in ("2026-01", "2026-07", "2026-08"):
        check(f"department summary {m}", http.get("/analytics/departments/summary", params={"month": m}).json() == oracle.summary(emps, logs, m))
    check("department summary dept filter", http.get("/analytics/departments/summary", params={"month": month, "department": "Sales"}).json() == oracle.summary(emps, logs, month, "Sales"))
    for limit in (1, 2, 3, 10, 50):
        for dept in (None, "Sales", "Engineering", "Ops"):
            p = {"month": month, "limit": limit, **({"department": dept} if dept else {})}
            check(f"leaderboard limit={limit} dept={dept}", http.get("/analytics/leaderboard/late", params=p).json() == oracle.leaderboard(emps, logs, month, limit, dept))
    for dept in ("Engineering", "Sales", "Ops", "Ghost Dept"):
        for rng in (("2026-07-01", "2026-07-31"), ("2026-06-20", "2026-07-12"), ("2026-07-04", "2026-07-05"), ("2026-07-10", "2026-09-10")):
            r = http.get(f"/analytics/departments/{dept}/trend", params={"from": rng[0], "to": rng[1]})
            check(f"trend {dept} {rng[0]}..{rng[1]}", r.status_code == 200 and r.json() == oracle.trend(emps, logs, dept, *rng), r.text[:200])

    # ------------------------------------------------------------------ concurrency
    def many(fn, n=20):
        with cf.ThreadPoolExecutor(n) as ex:
            return [f.result() for f in [ex.submit(fn, i) for i in range(n)]]

    body = {"emp_code": "EMP7001", "name": "Race", "email": "r@x.com", "department": "Race", "joined_on": "2026-01-01"}
    codes = [r.status_code for r in many(lambda i: http.post("/employees", json=body))]
    check("parallel create employee: one 201, rest 409", codes.count(201) == 1 and codes.count(409) == 19, codes)
    t_in = 1783312500000  # 2026-07-06 10:15 IST
    codes = [r.status_code for r in many(lambda i: http.post("/attendance/punch-in", json={"emp_code": "EMP7001", "punched_at": t_in}))]
    check("parallel punch-in: one 201, rest 409", codes.count(201) == 1 and codes.count(409) == 19, codes)
    codes = [r.status_code for r in many(lambda i: http.post("/attendance/punch-out", json={"emp_code": "EMP7001", "punched_at": t_in + 9 * 3600_000}))]
    check("parallel punch-out: exactly one 200", codes.count(200) == 1 and set(codes) <= {200, 409}, codes)
    day = http.get("/attendance", params={"emp_code": "EMP7001"}).json()["items"][0]["date"]
    res = many(lambda i: http.patch(f"/attendance/EMP7001/{day}", json={"reason": "parallel fix", "regularized_by": f"u{i}", "punch_out": t_in + (3600 + 60 * i) * 1000}), 12)
    ok = sum(r.status_code == 200 for r in res)
    hist = http.get("/attendance", params={"emp_code": "EMP7001"}).json()["items"][0]["history"]
    check("parallel PATCH: history length == number of successful 200s", len(hist) == ok and set(r.status_code for r in res) <= {200, 409, 422}, (ok, len(hist)))

    # ------------------------------------------------------------------ big dataset: explain + latency
    if args.big:
        e2, l2 = seed_big.build()
        db.employees.delete_many({}); db.attendance_logs.delete_many({})
        db.employees.insert_many(e2); db.attendance_logs.insert_many(l2)
        print(f"loaded {len(e2)} employees / {len(l2)} records")
    targets = {
        "attendance_list": [{}, {"emp_code": "EMP0007"}, {"status": "WFH", "date_from": "2026-06-10", "date_to": "2026-06-20"},
                            {"date_from": "2026-06-10", "date_to": "2026-06-20", "page": 3, "page_size": 50}],
        "employee_monthly": [{"emp_code": "EMP0007", "month": "2026-07"}],
        "department_summary": [{"month": "2026-07"}, {"month": "2026-07", "department": "Dept03"}],
        "late_leaderboard": [{"month": "2026-07"}, {"month": "2026-07", "department": "Dept03", "limit": 5}],
        "department_trend": [{"department": "Dept03", "from": "2026-06-01", "to": "2026-07-20"}],
    }
    for ep, variants in targets.items():
        for params in variants:
            r = http.get(f"/admin/explain/{ep}", params=params)
            txt = json.dumps(r.json()) if r.status_code == 200 else ""
            check(f"explain {ep} {params}", r.status_code == 200 and "IXSCAN" in txt and "COLLSCAN" not in txt, r.text[:200])
    timed = [("/attendance", {}), ("/attendance", {"emp_code": "EMP0042"}), ("/attendance", {"status": "LEAVE", "page": 5}),
             ("/analytics/employees/EMP0042/monthly", {"month": "2026-07"}), ("/analytics/departments/summary", {"month": "2026-07"}),
             ("/analytics/leaderboard/late", {"month": "2026-07"}), ("/analytics/departments/Dept03/trend", {"from": "2026-06-01", "to": "2026-07-20"})]
    for path, params in timed:
        t0 = time.perf_counter(); r = http.get(path, params=params); ms = (time.perf_counter() - t0) * 1000
        print(f"      {r.status_code}  {ms:7.1f} ms  {path} {params}")
        check(f"status 200 {path}", r.status_code == 200, r.text[:200])

    print("\nALL CHECKS PASSED" if not failures else f"\n{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
