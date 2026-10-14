"""End-to-end checks against a RUNNING server. Standard library + pymongo only.

    MONGO_DB=attendance_test uvicorn app.main:app --port 8000
    MONGO_DB=attendance_test python tests/check.py

WARNING: this wipes the `employees` and `attendance_logs` collections of MONGO_DB.

The analytics section seeds random data (ties, weekend records, employees with no
logs, mid-month joiners, orphan logs) and compares every analytics endpoint with a
plain-Python reference implementation of the rules.
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from pymongo import MongoClient

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_test")]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
failures = []
passed = 0


def call(method, path, body=None, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def check(name, got, want):
    global passed
    if got == want:
        passed += 1
    else:
        failures.append(name)
        print(f"FAIL {name}\n   got:  {got}\n   want: {want}")


def ist_ms(day, hms, millis=0):
    h, m, s = map(int, hms.split(":"))
    y, mo, d = map(int, day.split("-"))
    return int(datetime(y, mo, d, h, m, s, tzinfo=IST).timestamp()) * 1000 + millis


def parallel(n, fn):
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(lambda _: fn(), range(n)))


def emp(code, dept="Engineering", start="09:30", end="18:30", joined="2026-01-05"):
    return {"emp_code": code, "name": f"N {code}", "email": f"{code}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined}


def reset():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


# --------------------------------------------------------------------------- #
def test_employees():
    reset()
    check("health", call("GET", "/health"), (200, {"status": "ok"}))
    status, body = call("POST", "/employees", emp("EMP0001"))
    check("create 201", status, 201)
    check("create fields", sorted(body), sorted(["emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"]))
    check("created_at is ms int", isinstance(body["created_at"], int) and abs(body["created_at"] - time.time() * 1000) < 10_000, True)
    check("duplicate 409", call("POST", "/employees", emp("EMP0001"))[0], 409)
    codes = [s for s, _ in parallel(12, lambda: call("POST", "/employees", emp("EMP0002")))]
    check("concurrent create", sorted(codes), [201] + [409] * 11)

    bad = [
        ("emp_code", "EMP1"), ("emp_code", "X0001"), ("emp_code", "EMP1234567"), ("name", ""), ("email", "nope"),
        ("shift_start", "9:30"), ("shift_end", "24:00"), ("joined_on", "2026-02-30"), ("joined_on", "20260105"),
        ("joined_on", "05-01-2026"), ("department", ""), ("shift_end", "09:30"),
    ]
    for field, value in bad:
        check(f"422 {field}={value!r}", call("POST", "/employees", {**emp("EMP0099"), field: value})[0], 422)
    check("422 missing field", call("POST", "/employees", {"emp_code": "EMP0099"})[0], 422)

    for i in range(3, 8):
        call("POST", "/employees", emp(f"EMP{i:04d}", dept="Sales" if i % 2 else "Support"))
    _, page = call("GET", "/employees", page=1, page_size=3)
    check("list first page", [e["emp_code"] for e in page["items"]], ["EMP0001", "EMP0002", "EMP0003"])
    check("list total", page["total"], 7)
    _, page = call("GET", "/employees", page=3, page_size=3)
    check("list last page", [e["emp_code"] for e in page["items"]], ["EMP0007"])
    _, page = call("GET", "/employees", department="Sales")
    check("filter total", (page["total"], [e["emp_code"] for e in page["items"]]), (3, ["EMP0003", "EMP0005", "EMP0007"]))
    check("case-sensitive department", call("GET", "/employees", department="sales")[1]["total"], 0)
    for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}):
        check(f"422 {params}", call("GET", "/employees", **params)[0], 422)
    check("no _id anywhere", "_id" in json.dumps(call("GET", "/employees")[1]), False)


def test_punch():
    reset()
    for i in range(1, 9):
        call("POST", "/employees", emp(f"EMP{i:04d}"))
    call("POST", "/employees", emp("EMP0100", dept="Support", start="22:00", end="06:00"))
    day = "2026-07-06"

    def pin(code, hms, d=day, millis=0, **extra):
        return call("POST", "/attendance/punch-in", {"emp_code": code, "punched_at": ist_ms(d, hms, millis), **extra})

    def pout(code, hms, d=day):
        return call("POST", "/attendance/punch-out", {"emp_code": code, "punched_at": ist_ms(d, hms)})

    # R2
    s, r = pin("EMP0001", "09:40:00", millis=900)
    check("grace boundary", (s, r["late_minutes"], r["date"]), (201, 0, day))
    check("truncated to seconds", r["punch_in"], ist_ms(day, "09:40:00"))
    check("new record defaults", (r["punch_out"], r["work_hours"], r["overtime_minutes"], r["half_day"], r["history"], r["status"]), (None, None, 0, False, [], "PRESENT"))
    check("record fields", sorted(r), sorted(["emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"]))
    check("late 09:40:01", pin("EMP0002", "09:40:01")[1]["late_minutes"], 10)
    check("late 10:15:59", pin("EMP0003", "10:15:59", status="WFH")[1]["late_minutes"], 45)
    check("early", pin("EMP0004", "07:00:00")[1]["late_minutes"], 0)
    check("already punched in", pin("EMP0001", "11:00:00")[0], 409)
    check("unknown employee", pin("EMP9999", "09:00:00")[0], 404)
    for value in (ist_ms(day, "09:00:00") // 1000, float(ist_ms(day, "09:00:00")), str(ist_ms(day, "09:00:00")), 4102444800001, -5, True, 1783312500000.5):
        check(f"422 punched_at={value!r}", call("POST", "/attendance/punch-in", {"emp_code": "EMP0005", "punched_at": value})[0], 422)
    for value in ("ABSENT", "LEAVE", "present"):
        check(f"422 status={value}", pin("EMP0005", "09:00:00", status=value)[0], 422)
    check("422 no emp_code", call("POST", "/attendance/punch-in", {})[0], 422)
    s, r = call("POST", "/attendance/punch-in", {"emp_code": "EMP0008"})
    check("punched_at defaults to now", (s, abs(r["punch_in"] - time.time() * 1000) < 10_000), (201, True))
    codes = [s for s, _ in parallel(12, lambda: pin("EMP0005", "09:30:00"))]
    check("concurrent punch-in", sorted(codes), [201] + [409] * 11)
    check("IST date, not UTC", pin("EMP0006", "02:00:00", d="2026-07-08")[1]["date"], "2026-07-08")

    # R3 - R5
    s, r = pout("EMP0001", "18:59:59")
    check("overtime 29 -> 0", (s, r["overtime_minutes"], r["work_hours"], r["half_day"]), (200, 0, 9.33, False))
    check("overtime 30", pout("EMP0002", "19:00:00")[1]["overtime_minutes"], 30)
    check("already punched out", pout("EMP0002", "19:30:00")[0], 409)
    s, r = pout("EMP0003", "14:45:17")  # 16158 s = 4.48833 h
    check("half day", (r["work_hours"], r["half_day"], r["late_minutes"]), (4.49, True, 45))
    # 07:00:00 -> 11:30:18 = 16218 s = 4.505 h -> 4.51 half-up (float round() gives 4.5)
    s, r = pout("EMP0004", "11:30:18")
    check("work hours half-up", (r["work_hours"], r["half_day"]), (4.51, False))
    check("no punch-in", pout("EMP0007", "18:00:00")[0], 404)
    check("unknown employee", pout("EMP9999", "18:00:00")[0], 404)
    check("punch-out before punch-in", pout("EMP0005", "09:00:00")[0], 404)
    check("punch-out == punch-in", pout("EMP0005", "09:30:00")[0], 422)
    check("punch-out > 24h", pout("EMP0005", "09:30:01", d="2026-07-07")[0], 422)
    check("punch-out exactly 24h", pout("EMP0005", "09:30:00", d="2026-07-07")[0], 200)
    pin("EMP0007", "09:30:00")
    codes = [s for s, _ in parallel(12, lambda: pout("EMP0007", "18:30:00"))]
    check("concurrent punch-out", sorted(codes), [200] + [409] * 11)

    # Overnight shift 22:00 - 06:00
    s, r = pin("EMP0100", "21:55:00", d="2026-07-06")
    check("overnight early", (r["date"], r["late_minutes"]), ("2026-07-06", 0))
    s, r = pout("EMP0100", "06:40:00", d="2026-07-07")
    check("overnight punch-out", (s, r["date"], r["work_hours"], r["overtime_minutes"]), (200, "2026-07-06", 8.75, 40))
    s, r = pin("EMP0100", "00:30:00", d="2026-07-08")
    check("overnight after midnight", (r["date"], r["late_minutes"]), ("2026-07-07", 150))
    check("overnight same shift twice", pin("EMP0100", "23:00:00", d="2026-07-07")[0], 409)
    check("overnight 05:59:59", pin("EMP0100", "05:59:59", d="2026-07-10")[1]["date"], "2026-07-09")
    check("overnight 06:00:00", pin("EMP0100", "06:00:00", d="2026-07-11")[1]["date"], "2026-07-11")

    # Listing
    _, page = call("GET", "/attendance", page_size=100)
    keys = [(r["date"], r["emp_code"]) for r in page["items"]]
    check("list sorted", keys, sorted(keys, key=lambda k: (-int(k[0].replace("-", "")), k[1])))
    check("list total", page["total"], len(keys))
    _, page = call("GET", "/attendance", date_from=day, date_to=day, status="WFH")
    check("list filters", (page["total"], [r["emp_code"] for r in page["items"]]), (1, ["EMP0003"]))
    _, page = call("GET", "/attendance", emp_code="EMP0100")
    check("list by employee", [r["date"] for r in page["items"]], ["2026-07-11", "2026-07-09", "2026-07-07", "2026-07-06"])
    _, page = call("GET", "/attendance", emp_code="EMP0100", page=2, page_size=3)
    check("list paging", ([r["date"] for r in page["items"]], page["total"], page["page"], page["page_size"]), (["2026-07-06"], 4, 2, 3))
    for params in ({"date_from": "2026-07-09", "date_to": "2026-07-08"}, {"date_from": "x"}, {"date_to": "2026-13-01"}, {"status": "NOPE"}, {"page": 0}, {"page_size": 101}):
        check(f"422 {params}", call("GET", "/attendance", **params)[0], 422)


def test_regularize():
    reset()
    call("POST", "/employees", emp("EMP0001"))
    call("POST", "/employees", emp("EMP0100", dept="Support", start="22:00", end="06:00"))
    day = "2026-07-07"
    who = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    call("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": ist_ms(day, "10:05:00")})
    call("POST", "/attendance/punch-out", {"emp_code": "EMP0001", "punched_at": ist_ms(day, "18:30:00")})
    path = f"/attendance/EMP0001/{day}"

    s, r = call("PATCH", path, {**who, "punch_in": ist_ms(day, "09:28:00", 400)})
    check("patch 200", (s, r["late_minutes"], r["work_hours"], r["punch_in"]), (200, 0, 9.03, ist_ms(day, "09:28:00")))
    check("history appended", len(r["history"]), 1)
    h = r["history"][0]
    check("history meta", (h["by"], h["reason"], isinstance(h["at"], int)), ("hr.admin", "biometric glitch", True))
    check("history changes", h["changes"], {
        "punch_in": {"from": ist_ms(day, "10:05:00"), "to": ist_ms(day, "09:28:00")},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    })
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": day})
    check("stored as BSON dates", (isinstance(stored["punch_in"], datetime), isinstance(stored["history"][0]["at"], datetime),
                                   isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)), (True, True, True))
    check("no extra stored fields", sorted(stored), sorted(["_id", "emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"]))

    check("nothing changes", call("PATCH", path, {**who, "punch_in": ist_ms(day, "09:28:00")})[0], 422)
    check("same status", call("PATCH", path, {**who, "status": "PRESENT"})[0], 422)
    check("empty change", call("PATCH", path, who)[0], 422)
    check("derived fields ignored", call("PATCH", path, {**who, "work_hours": 1, "late_minutes": 99})[0], 422)
    check("short reason", call("PATCH", path, {**who, "reason": "abc", "status": "WFH"})[0], 422)
    check("missing by", call("PATCH", path, {"reason": "long enough", "status": "WFH"})[0], 422)
    check("punch_in other day", call("PATCH", path, {**who, "punch_in": ist_ms("2026-07-08", "09:28:00")})[0], 422)
    check("punch_out before in", call("PATCH", path, {**who, "punch_out": ist_ms(day, "09:00:00")})[0], 422)
    check("punch_out > 24h", call("PATCH", path, {**who, "punch_out": ist_ms("2026-07-08", "09:28:01")})[0], 422)
    check("seconds timestamp", call("PATCH", path, {**who, "punch_out": ist_ms(day, "19:00:00") // 1000})[0], 422)
    check("ABSENT with punch", call("PATCH", path, {**who, "status": "ABSENT", "punch_in": ist_ms(day, "09:00:00")})[0], 422)
    check("unknown employee", call("PATCH", f"/attendance/EMP9999/{day}", {**who, "status": "WFH"})[0], 404)
    check("no record", call("PATCH", "/attendance/EMP0001/2026-07-01", {**who, "status": "WFH"})[0], 404)
    check("bad date", call("PATCH", "/attendance/EMP0001/07-07-2026", {**who, "status": "WFH"})[0], 422)

    s, r = call("PATCH", path, {**who, "status": "ON_DUTY", "punch_out": ist_ms(day, "19:10:00")})
    check("status + punch_out", (s, r["status"], r["overtime_minutes"], r["work_hours"], sorted(r["history"][-1]["changes"])),
          (200, "ON_DUTY", 40, 9.7, ["overtime_minutes", "punch_out", "status", "work_hours"]))
    s, r = call("PATCH", path, {**who, "status": "LEAVE"})
    check("to LEAVE clears", (s, r["punch_in"], r["punch_out"], r["work_hours"], r["late_minutes"], r["overtime_minutes"], r["half_day"], len(r["history"])),
          (200, None, None, None, 0, 0, False, 3))
    check("LEAVE changes", r["history"][-1]["changes"]["punch_out"], {"from": ist_ms(day, "19:10:00"), "to": None})
    check("presence needs punch_in", call("PATCH", path, {**who, "status": "PRESENT"})[0], 422)
    check("punch on LEAVE record", call("PATCH", path, {**who, "punch_in": ist_ms(day, "09:00:00")})[0], 422)
    s, r = call("PATCH", path, {**who, "status": "WFH", "punch_in": ist_ms(day, "09:45:00")})
    check("back to presence", (s, r["status"], r["late_minutes"], r["punch_out"], r["work_hours"]), (200, "WFH", 15, None, None))
    s, r = call("PATCH", path, {**who, "punch_out": ist_ms(day, "13:00:00")})
    check("half day via patch", (r["work_hours"], r["half_day"], r["history"][-1]["changes"]["half_day"]), (3.25, True, {"from": False, "to": True}))

    # Overnight: the record's date is the day the shift started.
    call("POST", "/attendance/punch-in", {"emp_code": "EMP0100", "punched_at": ist_ms("2026-07-06", "22:30:00")})
    s, r = call("PATCH", "/attendance/EMP0100/2026-07-06", {**who, "punch_in": ist_ms("2026-07-07", "01:00:00")})
    check("overnight patch after midnight", (s, r["late_minutes"]), (200, 180))
    check("overnight patch off-shift", call("PATCH", "/attendance/EMP0100/2026-07-06", {**who, "punch_in": ist_ms("2026-07-07", "06:00:00")})[0], 422)

    # Concurrent corrections: every 200 must leave exactly one history entry.
    before = len(db.attendance_logs.find_one({"emp_code": "EMP0001", "date": day})["history"])
    minutes = iter(range(10, 40))
    results = parallel(12, lambda: call("PATCH", path, {**who, "punch_out": ist_ms(day, f"14:{next(minutes)}:00")}))
    ok = sum(1 for s, _ in results if s == 200)
    after = len(db.attendance_logs.find_one({"emp_code": "EMP0001", "date": day})["history"])
    check("concurrent patch codes", {s for s, _ in results} <= {200, 409} and ok >= 1, True)
    check("concurrent patch history", after - before, ok)


def test_sample_shapes():
    """Legacy record (no history / half_day) and a seeded history entry."""
    reset()
    utc = timezone.utc
    db.employees.insert_one({**emp("EMP0001"), "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=utc)})
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT", "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=utc),
         "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=utc), "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "punch_in": datetime(2026, 7, 7, 3, 58, tzinfo=utc),
         "punch_out": datetime(2026, 7, 7, 13, 0, tzinfo=utc), "work_hours": 9.03, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
         "history": [{"at": datetime(2026, 7, 8, 5, 0, tzinfo=utc), "by": "hr.admin", "reason": "biometric glitch",
                      "changes": {"punch_in": {"from": datetime(2026, 7, 7, 4, 35, tzinfo=utc), "to": datetime(2026, 7, 7, 3, 58, tzinfo=utc)},
                                  "late_minutes": {"from": 35, "to": 0}}}]},
        {"emp_code": "EMP0001", "date": "2026-07-08", "status": "LEAVE"},
    ])
    check("employee created_at", call("GET", "/employees")[1]["items"][0]["created_at"], 1767587400000)
    _, page = call("GET", "/attendance")
    legacy, leave, fixed = page["items"]
    check("legacy defaults", (legacy["half_day"], legacy["history"], legacy["punch_in"]), (False, [], 1784001720000))
    check("bare LEAVE record", (leave["punch_in"], leave["punch_out"], leave["work_hours"], leave["late_minutes"], leave["overtime_minutes"]), (None, None, None, 0, 0))
    check("history instants are ms", fixed["history"][0], {"at": 1783486800000, "by": "hr.admin", "reason": "biometric glitch",
          "changes": {"punch_in": {"from": 1783398900000, "to": 1783396680000}, "late_minutes": {"from": 35, "to": 0}}})
    s, r = call("PATCH", "/attendance/EMP0001/2026-07-14", {"reason": "forgot to punch", "regularized_by": "hr", "punch_out": ist_ms("2026-07-14", "19:00:00")})
    check("patch legacy record", (s, len(r["history"]), r["overtime_minutes"]), (200, 1, 30))


# --------------------------------------------------------------------------- #
# Analytics: random data vs a plain-Python reference
# --------------------------------------------------------------------------- #
def hu(value, places):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def days(first, last):
    out, d = [], first
    while d <= last:
        out.append(d)
        d += timedelta(days=1)
    return out


def present_value(log):
    if log["status"] not in PRESENCE:
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day") else Decimal(1)


def seed_analytics(rng):
    reset()
    depts = ["Engineering", "Sales", "Support", "Ops"]
    employees = []
    for i in range(1, 41):
        joined = rng.choice(["2026-01-05", "2026-03-10", "2026-07-01", "2026-07-13", "2026-07-22", "2026-08-03", "2026-09-15"])
        employees.append({**emp(f"EMP{i:04d}", dept=rng.choice(depts), joined=joined), "created_at": datetime.now(timezone.utc)})
    employees.append({**emp("EMP0900", dept="Ghost", joined="2026-08-20"), "created_at": datetime.now(timezone.utc)})  # joins later
    db.employees.insert_many(employees)
    silent = {"EMP0005", "EMP0011", "EMP0023", "EMP0900"}  # employees with zero logs
    logs = []
    for e in employees + [{"emp_code": "EMP7777"}]:  # EMP7777: logs without an employee
        if e["emp_code"] in silent:
            continue
        for d in days(date(2026, 6, 25), date(2026, 8, 5)):
            if rng.random() < (0.25 if d.weekday() >= 5 else 0.8):
                status = rng.choice(["PRESENT"] * 6 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])
                log = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": status, "punch_in": None, "punch_out": None,
                       "work_hours": None, "late_minutes": 0, "overtime_minutes": 0}
                if status in PRESENCE:
                    log["punch_in"] = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc)
                    log["late_minutes"] = rng.choice([0, 0, 0, 15, 15, 30, 45])  # few values -> many ties
                    if rng.random() < 0.9:
                        log["punch_out"] = log["punch_in"] + timedelta(hours=9)
                        log["work_hours"] = rng.choice([3.5, 4.49, 4.5, 8.0, 8.75, 9.03, 9.12, 9.335, 10.005])
                        log["overtime_minutes"] = rng.choice([0, 0, 30, 45])
                        log["half_day"] = log["work_hours"] < 4.5
                if rng.random() < 0.85:  # the rest are "legacy": no half_day / history
                    log.setdefault("half_day", False)
                    log["history"] = []
                logs.append(log)
    db.attendance_logs.insert_many([dict(l) for l in logs])
    return employees, logs


def ref_monthly(e, logs, month_first, month_last):
    mine = [l for l in logs if l["emp_code"] == e["emp_code"] and month_first.isoformat() <= l["date"] <= month_last.isoformat()]
    start = max(month_first, date.fromisoformat(e["joined_on"]))
    working = sum(1 for d in days(start, month_last) if d.weekday() < 5)
    present = sum((present_value(l) for l in mine if date.fromisoformat(l["date"]).weekday() < 5), Decimal(0))
    return {
        "emp_code": e["emp_code"], "month": month_first.isoformat()[:7], "working_days": working, "present_days": float(present),
        "leave_days": sum(l["status"] == "LEAVE" for l in mine), "late_count": sum(l["late_minutes"] > 0 for l in mine),
        "total_late_minutes": sum(l["late_minutes"] for l in mine), "total_overtime_minutes": sum(l["overtime_minutes"] for l in mine),
        "attendance_pct": hu(present * 100 / working, 2) if working else None,
    }


def ref_summary(employees, logs, month_first, month_last, department=None):
    items = []
    for dept in sorted({e["department"] for e in employees}):
        if department is not None and dept != department:
            continue
        members = {e["emp_code"] for e in employees if e["department"] == dept and e["joined_on"] <= month_last.isoformat()}
        if not members:
            continue
        mine = [l for l in logs if l["emp_code"] in members and month_first.isoformat() <= l["date"] <= month_last.isoformat()]
        hours = [Decimal(str(l["work_hours"])) for l in mine if l["status"] in PRESENCE and l["work_hours"] is not None]
        items.append({
            "department": dept, "headcount": len(members),
            "present_days": float(sum((present_value(l) for l in mine if date.fromisoformat(l["date"]).weekday() < 5), Decimal(0))),
            "avg_work_hours": hu(sum(hours) / len(hours), 2) if hours else None,
            "late_count": sum(l["late_minutes"] > 0 for l in mine), "total_late_minutes": sum(l["late_minutes"] for l in mine),
            "leave_count": sum(l["status"] == "LEAVE" for l in mine), "on_duty_count": sum(l["status"] == "ON_DUTY" for l in mine),
        })
    return items


def ref_leaderboard(employees, logs, month_first, month_last, limit, department=None):
    by_code = {e["emp_code"]: e for e in employees}
    totals = {}
    for l in logs:
        e = by_code.get(l["emp_code"])
        if e is None or (department is not None and e["department"] != department):
            continue
        if month_first.isoformat() <= l["date"] <= month_last.isoformat() and l["late_minutes"] > 0:
            t = totals.setdefault(l["emp_code"], [0, 0])
            t[0] += l["late_minutes"]
            t[1] += 1
    rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for code, (total, count) in rows:
        rank = 1 + sum(1 for _, (other, _) in rows if other > total)
        if rank <= limit:
            out.append({"rank": rank, "emp_code": code, "name": by_code[code]["name"], "department": by_code[code]["department"],
                        "total_late_minutes": total, "late_count": count})
    return out


def ref_trend(employees, logs, dept, first, last):
    members = [e for e in employees if e["department"] == dept]
    codes = {e["emp_code"] for e in members}
    out, rates = [], []
    for d in days(first, last):
        ds = d.isoformat()
        mine = [l for l in logs if l["emp_code"] in codes and l["date"] == ds]
        headcount = sum(e["joined_on"] <= ds for e in members)
        present = sum((present_value(l) for l in mine), Decimal(0))
        working = d.weekday() < 5
        rate = hu(present / headcount, 4) if working and headcount else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        out.append({"date": ds, "is_working_day": working, "headcount": headcount, "present_count": float(present),
                    "late_count": sum(l["late_minutes"] > 0 for l in mine), "attendance_rate": rate,
                    "moving_avg_7d": hu(sum(Decimal(str(r)) for r in window) / len(window), 4) if window else None})
    return out


def test_analytics():
    rng = random.Random(7)
    employees, logs = seed_analytics(rng)
    for month, first, last in (("2026-07", date(2026, 7, 1), date(2026, 7, 31)), ("2026-08", date(2026, 8, 1), date(2026, 8, 31)),
                               ("2026-06", date(2026, 6, 1), date(2026, 6, 30)), ("2025-02", date(2025, 2, 1), date(2025, 2, 28))):
        for e in employees:
            check(f"monthly {e['emp_code']} {month}", call("GET", f"/analytics/employees/{e['emp_code']}/monthly", month=month),
                  (200, ref_monthly(e, logs, first, last)))
        check(f"summary {month}", call("GET", "/analytics/departments/summary", month=month),
              (200, {"month": month, "items": ref_summary(employees, logs, first, last)}))
        for dept in ("Sales", "Ghost", "Nope"):
            check(f"summary {month} {dept}", call("GET", "/analytics/departments/summary", month=month, department=dept)[1]["items"],
                  ref_summary(employees, logs, first, last, dept))
        for limit in (1, 2, 3, 5, 10, 50):
            check(f"leaderboard {month} limit={limit}", call("GET", "/analytics/leaderboard/late", month=month, limit=limit),
                  (200, {"month": month, "items": ref_leaderboard(employees, logs, first, last, limit)}))
            check(f"leaderboard {month} Sales limit={limit}", call("GET", "/analytics/leaderboard/late", month=month, limit=limit, department="Sales")[1]["items"],
                  ref_leaderboard(employees, logs, first, last, limit, "Sales"))
    board = call("GET", "/analytics/leaderboard/late", month="2026-07", limit=5)[1]["items"]
    ranks = [r["rank"] for r in board]
    check("leaderboard has ties", len(ranks) != len(set(ranks)), True)
    check("leaderboard default limit", call("GET", "/analytics/leaderboard/late", month="2026-07")[1]["items"],
          ref_leaderboard(employees, logs, date(2026, 7, 1), date(2026, 7, 31), 10))

    ranges = [(date(2026, 7, 1), date(2026, 7, 31)), (date(2026, 6, 20), date(2026, 8, 20)), (date(2026, 7, 4), date(2026, 7, 5)),
              (date(2026, 7, 13), date(2026, 7, 13)), (date(2025, 1, 1), date(2025, 1, 20)), (date(2026, 9, 1), date(2026, 12, 1))]
    for dept in ("Engineering", "Sales", "Support", "Ops", "Ghost"):
        for first, last in ranges:
            got = call("GET", f"/analytics/departments/{dept}/trend", **{"from": first.isoformat(), "to": last.isoformat()})
            check(f"trend {dept} {first}..{last}", got, (200, {"department": dept, "items": ref_trend(employees, logs, dept, first, last)}))

    trend = "/analytics/departments/Sales/trend"
    check("trend unknown department", call("GET", "/analytics/departments/Nope/trend", **{"from": "2026-07-01", "to": "2026-07-02"})[0], 404)
    check("trend to < from", call("GET", trend, **{"from": "2026-07-02", "to": "2026-07-01"})[0], 422)
    check("trend 93 days", call("GET", trend, **{"from": "2026-07-01", "to": "2026-10-01"})[0], 422)
    check("trend missing to", call("GET", trend, **{"from": "2026-07-01"})[0], 422)
    check("trend bad date", call("GET", trend, **{"from": "2026-07-01", "to": "soon"})[0], 422)
    check("monthly unknown employee", call("GET", "/analytics/employees/EMP9999/monthly", month="2026-07")[0], 404)
    for path in ("/analytics/employees/EMP0001/monthly", "/analytics/departments/summary", "/analytics/leaderboard/late"):
        check(f"{path} missing month", call("GET", path)[0], 422)
        check(f"{path} bad month", call("GET", path, month="2026-13")[0], 422)
    for limit in (0, 51):
        check(f"leaderboard limit={limit}", call("GET", "/analytics/leaderboard/late", month="2026-07", limit=limit)[0], 422)


# --------------------------------------------------------------------------- #
# Scale: ~100k records, explain plans and timings
# --------------------------------------------------------------------------- #
EXPLAINS = [
    ("attendance_list", {}),
    ("attendance_list", {"page": 50, "page_size": 100}),
    ("attendance_list", {"emp_code": "EMP0042"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("attendance_list", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("attendance_list", {"emp_code": "EMP0042", "status": "PRESENT", "date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("employee_monthly", {"emp_code": "EMP0042", "month": "2026-03"}),
    ("department_summary", {"month": "2026-03"}),
    ("department_summary", {"month": "2026-03", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-03"}),
    ("late_leaderboard", {"month": "2026-03", "department": "Sales", "limit": 5}),
    ("department_trend", {"department": "Sales", "from": "2026-03-01", "to": "2026-05-31"}),
]


def test_scale():
    reset()
    rng = random.Random(11)
    depts = ["Engineering", "Sales", "Support", "Ops", "Finance", "HR"]
    employees = [{**emp(f"EMP{i:04d}", dept=rng.choice(depts), joined="2025-06-01"), "created_at": datetime.now(timezone.utc)} for i in range(1, 501)]
    db.employees.insert_many(employees)
    logs = []
    for d in days(date(2025, 10, 1), date(2026, 6, 30)):
        for e in employees:
            if rng.random() < (0.05 if d.weekday() >= 5 else 0.98):
                status = rng.choice(["PRESENT"] * 7 + ["WFH", "ON_DUTY", "LEAVE"])
                present = status in PRESENCE
                stamp = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc)
                logs.append({"emp_code": e["emp_code"], "date": d.isoformat(), "status": status,
                             "punch_in": stamp if present else None, "punch_out": stamp + timedelta(hours=9) if present else None,
                             "work_hours": 9.0 if present else None, "late_minutes": rng.choice([0, 0, 0, 20]) if present else 0,
                             "overtime_minutes": 0, "half_day": False, "history": []})
    db.attendance_logs.insert_many(logs)
    print(f"   seeded {len(logs)} attendance records")

    for endpoint, params in EXPLAINS:
        status, body = call("GET", f"/admin/explain/{endpoint}", **params)
        text = json.dumps(body)
        check(f"explain {endpoint} {params}", (status, "IXSCAN" in text, "COLLSCAN" in text), (200, True, False))
    check("explain response keys", sorted(call("GET", "/admin/explain/attendance_list")[1]), ["collection", "endpoint", "explain"])
    for endpoint, params in (("employee_monthly", {"month": "2026-03"}), ("department_summary", {}), ("late_leaderboard", {}),
                             ("department_trend", {"department": "Sales"}), ("nope", {}), ("attendance_list", {"page": 0})):
        check(f"explain 422 {endpoint} {params}", call("GET", f"/admin/explain/{endpoint}", **params)[0], 422)

    timed = [
        ("/attendance", {}), ("/attendance", {"page": 900, "page_size": 100}), ("/attendance", {"status": "LEAVE"}),
        ("/attendance", {"emp_code": "EMP0042"}), ("/attendance", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
        ("/analytics/employees/EMP0042/monthly", {"month": "2026-03"}), ("/analytics/departments/summary", {"month": "2026-03"}),
        ("/analytics/leaderboard/late", {"month": "2026-03"}), ("/analytics/departments/Sales/trend", {"from": "2026-03-01", "to": "2026-05-31"}),
    ]
    for path, params in timed:
        t0 = time.perf_counter()
        status, _ = call("GET", path, **params)
        ms = (time.perf_counter() - t0) * 1000
        print(f"   {ms:7.1f} ms  {path} {params}")
        check(f"fast {path} {params}", (status, ms < 1000), (200, True))


if __name__ == "__main__":
    for test in (test_employees, test_punch, test_regularize, test_sample_shapes, test_analytics, test_scale):
        print(test.__name__)
        test()
    print(f"\n{passed} passed, {len(failures)} failed")
    sys.exit(1 if failures else 0)
