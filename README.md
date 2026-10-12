# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or put both variables in a local .env (see .env.example)
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Needs MongoDB 6.0 or newer (the analytics use `$setWindowFields`, `$densify` and `$rank`). Indexes are created at
startup; there are no manual steps. Interactive docs: `http://localhost:8000/docs`.

## Status
Everything in the assignment is implemented: Part A fixes (REVIEW.md), all endpoints in the contract, the explain
endpoint, DECISIONS.md. Nothing is intentionally missing.

## Choices where the contract is open to interpretation
- **Trend range limit:** a range of more than 92 calendar days (both ends counted) is a 422.
- **Moving average:** the mean of the *reported* (4-decimal) `attendance_rate` values in the window.
- **PATCH "changes nothing":** judged on `status`, `punch_in` and `punch_out` only.
- **PATCH concurrency:** optimistic compare-and-set on the record state and history length, with retries; no history entry is ever lost.
- **Punch-out when the latest record is already closed:** 409, even if `punched_at` would be valid.

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest tests -q            # offline: rules, endpoints, analytics vs an independent oracle
```
The offline tests do not need MongoDB. They use `mongomock` for CRUD and a small aggregation interpreter
(`tests/minimongo.py`) for the pipelines, so they check the pipeline *logic*, not MongoDB itself. To check against a
real server (parallel requests, explain plans, ~100k records), use a **scratch database** (the script wipes it):
```bash
python scripts/e2e_check.py --base http://localhost:8000 --big
```
