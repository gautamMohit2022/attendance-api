"""Analytics endpoints compared with the independent oracle on an awkward dataset
(ties, weekend records, no-log employees, mid-month joiner, future joiner, unknown employee, legacy docs)."""
import pytest

import oracle

MONTH = "2026-07"


@pytest.fixture()
def seeded(main, api):
    emps, logs = oracle.build_dataset()
    main.db.employees.insert_many([dict(e) for e in emps])
    main.db.attendance_logs.insert_many([dict(l) for l in logs])
    return api, emps, logs


def test_employee_monthly_matches_oracle(seeded):
    api, emps, logs = seeded
    for e in emps:
        r = api.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": MONTH})
        assert r.status_code == 200, e["emp_code"]
        assert r.json() == oracle.monthly(e, logs, MONTH), e["emp_code"]
    assert api.get("/analytics/employees/EMP7777/monthly", params={"month": MONTH}).status_code == 404
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-7"}).status_code == 422
    assert api.get("/analytics/employees/EMP0001/monthly").status_code == 422


def test_monthly_special_cases(seeded):
    api, *_ = seeded
    nobody = api.get("/analytics/employees/EMP0005/monthly", params={"month": MONTH}).json()   # no logs
    assert nobody["present_days"] == 0 and nobody["working_days"] == 23 and nobody["attendance_pct"] == 0.0
    future = api.get("/analytics/employees/EMP0004/monthly", params={"month": MONTH}).json()   # joins in August
    assert future["working_days"] == 0 and future["attendance_pct"] is None
    joiner = api.get("/analytics/employees/EMP0003/monthly", params={"month": MONTH}).json()   # joined Jul 15
    assert joiner["working_days"] == 13


def test_department_summary_matches_oracle(seeded):
    api, emps, logs = seeded
    r = api.get("/analytics/departments/summary", params={"month": MONTH})
    assert r.status_code == 200
    assert r.json() == oracle.summary(emps, logs, MONTH)
    names = [i["department"] for i in r.json()["items"]]
    assert "Ghost Dept" not in names                       # headcount 0 -> omitted
    eng = next(i for i in r.json()["items"] if i["department"] == "Engineering")
    assert eng["headcount"] == 4                           # EMP0001/2/3/5; EMP0004 has not joined; EMP0005 has no logs
    only = api.get("/analytics/departments/summary", params={"month": MONTH, "department": "Sales"}).json()
    assert only == oracle.summary(emps, logs, MONTH, "Sales")
    assert api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Nope"}).json()["items"] == []
    assert api.get("/analytics/departments/summary", params={"month": "2026-00"}).status_code == 422


def test_department_summary_other_months(seeded):
    api, emps, logs = seeded
    for month in ("2026-01", "2026-08", "2026-09"):
        assert api.get("/analytics/departments/summary", params={"month": month}).json() == oracle.summary(emps, logs, month)


def test_leaderboard_ties_and_limit(seeded):
    api, emps, logs = seeded
    full = api.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 50}).json()
    assert full == oracle.leaderboard(emps, logs, MONTH, 50)
    top = [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in full["items"][:4]]
    assert top[:3] == [(1, "EMP0013", 900), (1, "EMP0014", 900), (3, "EMP0015", 899)]    # 1, 1, 3 and tie order by emp_code
    assert all(i["emp_code"] != "EMP9999" for i in full["items"])                          # unknown employee ignored
    # limit applies AFTER ranking: limit=1 returns BOTH tied rank-1 rows
    one = api.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 1}).json()
    assert [i["emp_code"] for i in one["items"]] == ["EMP0013", "EMP0014"]
    two = api.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 2}).json()
    assert len(two["items"]) == 2                           # rank 3 is > 2 so it is excluded
    for limit in (1, 2, 3, 5, 10, 50):
        for dept in (None, "Sales", "Engineering", "Ops"):
            params = {"month": MONTH, "limit": limit, **({"department": dept} if dept else {})}
            assert api.get("/analytics/leaderboard/late", params=params).json() == oracle.leaderboard(emps, logs, MONTH, limit, dept)
    for bad in ({"limit": 0}, {"limit": 51}):
        assert api.get("/analytics/leaderboard/late", params={"month": MONTH, **bad}).status_code == 422


@pytest.mark.parametrize("dept", ["Engineering", "Sales", "Ops", "Ghost Dept"])
@pytest.mark.parametrize("rng", [("2026-07-01", "2026-07-31"), ("2026-06-20", "2026-07-12"), ("2026-07-13", "2026-07-13"),
                                 ("2026-07-04", "2026-07-05"), ("2026-07-10", "2026-09-10")])
def test_trend_matches_oracle(seeded, dept, rng):
    api, emps, logs = seeded
    r = api.get(f"/analytics/departments/{dept}/trend", params={"from": rng[0], "to": rng[1]})
    assert r.status_code == 200
    assert r.json() == oracle.trend(emps, logs, dept, *rng)


def test_trend_shape_and_errors(seeded):
    api, *_ = seeded
    r = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-31"}).json()
    rows = r["items"]
    assert len(rows) == 31 and rows[0]["date"] == "2026-07-01" and rows[-1]["date"] == "2026-07-31"
    sat = next(x for x in rows if x["date"] == "2026-07-04")
    assert sat["is_working_day"] is False and sat["attendance_rate"] is None
    assert rows[0]["moving_avg_7d"] == rows[0]["attendance_rate"]           # shortest window: just itself
    assert api.get("/analytics/departments/Nowhere/trend", params={"from": "2026-07-01", "to": "2026-07-05"}).status_code == 404
    assert api.get("/analytics/departments/Sales/trend", params={"from": "2026-07-05", "to": "2026-07-01"}).status_code == 422
    assert api.get("/analytics/departments/Sales/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422   # 93 days
    assert api.get("/analytics/departments/Sales/trend", params={"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200   # 92 days
    assert api.get("/analytics/departments/Sales/trend", params={"from": "2026-01-01"}).status_code == 422


def test_trend_working_day_without_records_has_rate_zero(main, api):
    main.db.employees.insert_one(oracle.employee("EMP0001", "Asha", "Solo", "2026-07-01"))
    rows = api.get("/analytics/departments/Solo/trend", params={"from": "2026-07-01", "to": "2026-07-07"}).json()["items"]
    wd = [r for r in rows if r["is_working_day"]]
    assert all(r["attendance_rate"] == 0.0 and r["headcount"] == 1 for r in wd)
    assert [r["attendance_rate"] for r in rows if not r["is_working_day"]] == [None, None]
