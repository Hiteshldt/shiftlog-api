# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or an Atlas URI
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

Needs Python 3.11+ and MongoDB 6.0+ (local: `docker run -d -p 27017:27017 mongo:7`).
`MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` is loaded only for values that are not already set.
All indexes are created at startup. There are no manual steps.

## What is implemented

Every endpoint in the contract: health, employees, punch-in, punch-out, attendance list, regularize (PATCH), the four analytics endpoints, and `/admin/explain/{endpoint}`. Nothing is missing.

## Tests

`tests/check.py` runs end-to-end checks against a running server (standard library + pymongo, no extra packages). It **wipes** both collections of `MONGO_DB`, so point it at a throwaway database:

```bash
MONGO_DB=attendance_test uvicorn app.main:app --port 8000
MONGO_DB=attendance_test python tests/check.py
```

It covers the rules R1 - R10, status codes, the three race conditions, and compares all analytics endpoints with a plain-Python reference on random data (ties, weekend records, employees with no logs, mid-month joiners, logs without an employee). It then seeds about 100,000 records and checks that every explain plan has `IXSCAN` and no `COLLSCAN`.

## Interpretations worth knowing

Where the contract left room, I chose the following:

- **Trend range limit.** "Exceeds 92 days" is counted inclusively: a range that would return more than 92 rows is a 422.
- **`moving_avg_7d`** is the mean of the already rounded (4 decimal) `attendance_rate` values in the window, then rounded again.
- **Punch-out order of checks.** The most recent record with `punch_in <= punched_at` is found first. If it is already closed the answer is 409, even when `punched_at` is also more than 24 hours later.
- **PATCH that changes nothing** means `status`, `punch_in` and `punch_out` all stay the same; that is a 422 and no history entry is written.
- **PATCH with a punch time on an `ABSENT` / `LEAVE` record** without also sending a presence status is a 422.
- **PATCH with an explicit `null`** for `status`, `punch_in` or `punch_out` is treated as "not supplied".
- **Rounding.** MongoDB's `$round` rounds half to even, so half-up rounding (R4, R8) is done with `Decimal` in Python on the aggregated numbers, or with a floor-based expression inside the trend pipeline.
- **Custom 422 / 404 / 409 errors** use `{"detail": "<message>"}`; request-shape errors use FastAPI's default validation body.
