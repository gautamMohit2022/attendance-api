"""Endpoint behaviour: validation, status codes, punch-in / punch-out / PATCH rules (mongomock-backed)."""
import pytest

from oracle import at

MS = lambda dtm: int(dtm.timestamp() * 1000)  # noqa: E731


def make_emp(api, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "Asha Rao", "email": "asha@example.com", "department": "Engineering",
            "joined_on": "2026-01-05", **kw}
    return api.post("/employees", json=body)


# ---------------------------------------------------------------- employees
def test_create_and_duplicate_and_validation(api):
    r = make_emp(api)
    assert r.status_code == 201
    body = r.json()
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30" and isinstance(body["created_at"], int)
    assert "_id" not in body
    assert make_emp(api).status_code == 409
    for bad in ({"emp_code": "E1"}, {"emp_code": "EMP0002", "email": "nope"}, {"emp_code": "EMP0002", "joined_on": "2026-02-30"},
                {"emp_code": "EMP0002", "shift_start": "9:30"}, {"emp_code": "EMP0002", "shift_start": "09:30", "shift_end": "09:30"},
                {"emp_code": "EMP0002", "name": ""}, {"emp_code": "EMP0002", "department": "x" * 51}):
        assert make_emp(api, **bad).status_code == 422, bad


def test_list_employees_pagination_sort_total(api):
    for i in (3, 1, 2):
        make_emp(api, f"EMP000{i}", department="A" if i < 3 else "B")
    r = api.get("/employees", params={"page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 3
    r = api.get("/employees", params={"page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003"]
    r = api.get("/employees", params={"department": "A"}).json()
    assert r["total"] == 2                                     # total reflects the filter
    for bad in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
        assert api.get("/employees", params=bad).status_code == 422


# ---------------------------------------------------------------- punch-in
def test_punch_in_basics(api):
    make_emp(api)
    t = MS(at("2026-07-06", 10, 15, 59))
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t})
    assert r.status_code == 201
    b = r.json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 45 and b["status"] == "PRESENT"
    assert b["punch_in"] == t // 1000 * 1000 and b["punch_out"] is None and b["work_hours"] is None
    assert b["history"] == [] and b["half_day"] is False and "id" not in b and "_id" not in b
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t + 5000}).status_code == 409


def test_punch_in_truncates_seconds_and_uses_ist_date(api):
    make_emp(api)
    t = MS(at("2026-07-07", 0, 20, 0)) + 900             # 00:20:00.900 IST on the 7th = 18:50 UTC on the 6th
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t}).json()
    assert b["date"] == "2026-07-07" and b["punch_in"] == t - 900


@pytest.mark.parametrize("payload", [
    {"emp_code": "EMP0001", "punched_at": 1783312500},            # seconds
    {"emp_code": "EMP0001", "punched_at": 1783312500000.0},       # float
    {"emp_code": "EMP0001", "punched_at": "1783312500000"},       # string
    {"emp_code": "EMP0001", "punched_at": True},                  # bool
    {"emp_code": "EMP0001", "punched_at": 4102444800001},         # out of range
    {"emp_code": "EMP0001", "status": "ABSENT"},
    {"emp_code": "EMP0001", "status": "LEAVE"},
    {"emp_code": "EMP0001", "status": "present"},
    {},
])
def test_punch_in_validation(api, payload):
    make_emp(api)
    assert api.post("/attendance/punch-in", json=payload).status_code == 422


def test_punch_in_unknown_employee_and_default_now(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP7777", "punched_at": MS(at("2026-07-06", 9, 0))}).status_code == 404
    make_emp(api)
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH"}).json()["status"] == "WFH"


def test_derived_fields_in_body_are_ignored(api):
    make_emp(api)
    t = MS(at("2026-07-06", 9, 0))
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t, "late_minutes": 99, "work_hours": 7}).json()
    assert b["late_minutes"] == 0 and b["work_hours"] is None


# ---------------------------------------------------------------- punch-out
def test_punch_out_flow_and_errors(api):
    make_emp(api)
    pin = MS(at("2026-07-06", 9, 28))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    pout = MS(at("2026-07-06", 19, 10))
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})
    assert r.status_code == 200
    b = r.json()
    assert b["work_hours"] == 9.7 and b["overtime_minutes"] == 40 and b["half_day"] is False and b["punch_out"] == pout
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout + 60000}).status_code == 409
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP7777", "punched_at": pout}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin - 3600_000}).status_code == 404


def test_punch_out_422_cases(api):
    make_emp(api)
    pin = MS(at("2026-07-06", 9, 30))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin + 24 * 3600_000 + 1000}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 12345}).status_code == 422
    # still open after all the rejected attempts, and exactly 24h is allowed
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin + 24 * 3600_000}).status_code == 200


def test_overnight_punch_out_across_midnight(api):
    make_emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    pin, pout = MS(at("2026-07-06", 21, 55)), MS(at("2026-07-07", 6, 40))
    a = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": pin}).json()
    assert a["date"] == "2026-07-06" and a["late_minutes"] == 0
    b = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": pout}).json()
    assert b["work_hours"] == 8.75 and b["overtime_minutes"] == 40 and b["date"] == "2026-07-06"   # sample doc #7


def test_punch_out_half_day(api):
    make_emp(api)
    pin = MS(at("2026-07-07", 9, 30))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    b = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin + 3 * 3600_000 + 1800_000}).json()
    assert b["work_hours"] == 3.5 and b["half_day"] is True


# ---------------------------------------------------------------- list attendance
def seed_days(api, n=5):
    make_emp(api); make_emp(api, "EMP0002")
    for code in ("EMP0001", "EMP0002"):
        for day in range(1, n + 1):
            api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": MS(at(f"2026-07-0{day}", 9, 30))})


def test_list_attendance_sort_filters_pagination(api):
    seed_days(api)
    r = api.get("/attendance", params={"page_size": 4}).json()
    assert r["total"] == 10
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [("2026-07-05", "EMP0001"), ("2026-07-05", "EMP0002"),
                                                                ("2026-07-04", "EMP0001"), ("2026-07-04", "EMP0002")]
    r = api.get("/attendance", params={"emp_code": "EMP0002", "date_from": "2026-07-02", "date_to": "2026-07-04"}).json()
    assert r["total"] == 3 and [i["date"] for i in r["items"]] == ["2026-07-04", "2026-07-03", "2026-07-02"]
    assert api.get("/attendance", params={"status": "WFH"}).json()["total"] == 0
    assert api.get("/attendance", params={"page": 3, "page_size": 4}).json()["items"].__len__() == 2
    for bad in ({"date_from": "2026-07-05", "date_to": "2026-07-01"}, {"date_from": "2026-13-01"}, {"date_from": "07/01/2026"},
                {"status": "BOGUS"}, {"page": 0}, {"page_size": 101}):
        assert api.get("/attendance", params=bad).status_code == 422, bad


def test_legacy_documents_get_defaults(api, main):
    make_emp(api, "EMP0002")
    main.db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                        "punch_in": at("2026-07-14", 9, 32), "punch_out": at("2026-07-14", 18, 31),
                                        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    item = api.get("/attendance").json()["items"][0]
    assert item["half_day"] is False and item["history"] == []


# ---------------------------------------------------------------- PATCH
def patch(api, code, date, **body):
    return api.patch(f"/attendance/{code}/{date}", json={"reason": "biometric glitch", "regularized_by": "hr.admin", **body})


def test_patch_recomputes_and_appends_history(api):
    make_emp(api)
    pin = MS(at("2026-07-07", 10, 5))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS(at("2026-07-07", 18, 30))})
    new_in = MS(at("2026-07-07", 9, 28))
    r = patch(api, "EMP0001", "2026-07-07", punch_in=new_in)
    assert r.status_code == 200
    b = r.json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 9.03
    h = b["history"]
    assert len(h) == 1 and h[0]["by"] == "hr.admin" and h[0]["reason"] == "biometric glitch"
    assert h[0]["changes"] == {"punch_in": {"from": pin, "to": new_in}, "late_minutes": {"from": 35, "to": 0},
                               "work_hours": {"from": 8.42, "to": 9.03}}                # sample doc #8
    r2 = patch(api, "EMP0001", "2026-07-07", status="WFH")
    assert r2.status_code == 200 and len(r2.json()["history"]) == 2 and r2.json()["history"][1]["changes"] == {
        "status": {"from": "PRESENT", "to": "WFH"}}
    assert r2.json()["history"][0] == h[0]       # earlier entries are never touched


def test_patch_rules(api):
    make_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS(at("2026-07-07", 9, 30))})
    D = "2026-07-07"
    assert patch(api, "EMP0001", D).status_code == 422                                          # nothing changes
    assert patch(api, "EMP0001", D, status="PRESENT").status_code == 422                       # same value
    assert patch(api, "EMP0001", D, status="LEAVE", punch_in=MS(at(D, 9, 0))).status_code == 422
    assert patch(api, "EMP0001", D, punch_in=MS(at("2026-07-08", 9, 0))).status_code == 422     # moves off the date
    assert patch(api, "EMP0001", D, punch_out=MS(at(D, 9, 0))).status_code == 422              # before punch_in
    assert patch(api, "EMP0001", D, punch_out=MS(at(D, 9, 30)) + 24 * 3600_000 + 1000).status_code == 422
    assert patch(api, "EMP0001", "2026-07-09").status_code == 404
    assert patch(api, "EMP7777", D).status_code == 404
    assert api.patch(f"/attendance/EMP0001/{D}", json={"reason": "abc", "regularized_by": "x", "status": "WFH"}).status_code == 422
    assert patch(api, "EMP0001", "2026-02-30", status="WFH").status_code == 422
    # absent clears the punch times and the derived fields
    r = patch(api, "EMP0001", D, status="ABSENT")
    assert r.status_code == 200 and r.json()["punch_in"] is None
    ch = r.json()["history"][0]["changes"]
    assert set(ch) == {"status", "punch_in"} and ch["punch_in"]["to"] is None
    # a presence status needs a punch_in
    assert patch(api, "EMP0001", D, status="PRESENT").status_code == 422
    assert patch(api, "EMP0001", D, status="PRESENT", punch_in=MS(at(D, 9, 45))).json()["late_minutes"] == 15


def test_patch_sequential_corrections_keep_all_history(api):
    make_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS(at("2026-07-07", 9, 30))})
    for minute in range(31, 36):
        assert patch(api, "EMP0001", "2026-07-07", punch_in=MS(at("2026-07-07", 9, minute))).status_code == 200
    assert len(api.get("/attendance").json()["items"][0]["history"]) == 5


def test_health_and_explain_validation(api):
    assert api.get("/health").json() == {"status": "ok"}
    assert api.get("/admin/explain/department_trend", params={"department": "x"}).status_code == 422
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert api.get("/admin/explain/nope").status_code == 422
    assert api.get("/admin/explain/late_leaderboard", params={"month": "2026-07"}).status_code == 200
