"""Pure-function tests for rules R1-R5 and the validation types. No database involved."""
import datetime as dt

import pytest

from oracle import at


@pytest.fixture()
def m(main):
    return main


def late(m, hh, mm, ss=0, ms=0, shift="09:30"):
    punch = at("2026-07-06", hh, mm, ss).replace(microsecond=ms * 1000)
    start, _ = m.shift_bounds("2026-07-06", shift, "18:30")
    return m.compute_late_minutes(punch, start)


def test_r2_late_examples_from_the_contract(m):
    assert late(m, 9, 40, 0) == 0          # exactly 10:00 after start: still inside grace
    assert late(m, 9, 40, 1) == 10         # one second over -> floor(10:01 / 1min) = 10
    assert late(m, 10, 15, 59) == 45
    assert late(m, 9, 40, 0, ms=900) == 0  # 09:40:00.900 is truncated to 09:40:00 (R1)
    assert late(m, 9, 0) == 0              # early arrival is never negative


def test_r3_overtime_threshold(m):
    _, end = m.shift_bounds("2026-07-06", "09:30", "18:30")
    assert m.compute_overtime(at("2026-07-06", 18, 59, 59), end) == 0   # 29 min 59 s
    assert m.compute_overtime(at("2026-07-06", 19, 0, 0), end) == 30
    assert m.compute_overtime(at("2026-07-06", 19, 40, 59), end) == 70
    assert m.compute_overtime(at("2026-07-06", 17, 0, 0), end) == 0


def test_r4_r5_half_up_and_half_day_uses_rounded_value(m):
    pin = at("2026-07-06", 9, 30)
    one = m.compute_work_hours(pin, pin + dt.timedelta(seconds=18))      # 0.005 h: a true tie
    assert float(one) == 0.01                                            # half-up (banker's would say 0.00)
    d = m.derive_fields("PRESENT", pin, pin + dt.timedelta(seconds=16182), "2026-07-06", "09:30", "18:30")
    assert d["work_hours"] == 4.5 and d["half_day"] is False             # 4.495 rounds to 4.50 -> not a half day
    d = m.derive_fields("PRESENT", pin, pin + dt.timedelta(seconds=16160), "2026-07-06", "09:30", "18:30")
    assert d["work_hours"] == 4.49 and d["half_day"] is True


def test_r1_attendance_date_overnight(m):
    f = lambda h, mi, s=0, day="2026-07-07": m.attendance_date(at(day, h, mi, s), "22:00", "06:00")  # noqa: E731
    assert f(21, 55, day="2026-07-06") == "2026-07-06"
    assert f(23, 30, day="2026-07-06") == "2026-07-06"
    assert f(3, 0) == "2026-07-06"          # after midnight, before shift_end -> previous day's shift
    assert f(5, 59, 59) == "2026-07-06"
    assert f(6, 0) == "2026-07-07"          # at shift_end exactly it is no longer the old shift
    # normal shift: date is just the IST date, even when the UTC date differs
    assert m.attendance_date(at("2026-07-07", 0, 20), "09:30", "18:30") == "2026-07-07"


def test_overnight_late_and_overtime(m):
    start, end = m.shift_bounds("2026-07-06", "22:00", "06:00")
    assert end - start == dt.timedelta(hours=8) and end.date() == dt.date(2026, 7, 7)
    assert m.compute_overtime(at("2026-07-07", 6, 40), end) == 40
    assert m.compute_late_minutes(at("2026-07-06", 21, 55), start) == 0
    assert m.compute_late_minutes(at("2026-07-06", 22, 20), start) == 20


def test_ms_conversion_roundtrip_and_naive_handling(m):
    ms = 1783312500123
    assert m.dt_to_ms(m.ms_to_dt(ms)) == ms
    assert m.dt_to_ms(m.ms_to_dt(ms).replace(tzinfo=None)) == ms   # naive is read as UTC


def test_weekday_counting(m):
    assert m.count_weekdays(dt.date(2026, 7, 1), dt.date(2026, 7, 31)) == 23
    assert m.count_weekdays(dt.date(2026, 7, 15), dt.date(2026, 7, 31)) == 13
    assert m.count_weekdays(dt.date(2026, 8, 1), dt.date(2026, 7, 31)) == 0
