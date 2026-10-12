# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or an Atlas URI
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
`MONGO_URI` / `MONGO_DB` come from the environment (a local `.env` also works; real environment variables win).
Indexes are created at startup, idempotently. Needs MongoDB 6.0+ (`$setWindowFields`, `$lookup` with
`localField` + `pipeline`, `$dateAdd`).

## Notes
- Instants are stored as BSON dates (read back timezone-aware), exposed as epoch milliseconds, and truncated to whole
  seconds (R1).
- Rounding is exact half-up (R4, R8) using integer / `Fraction` arithmetic, because Python's `round()` and Mongo's
  `$round` both round half to even. Inside pipelines, sums are carried as integers (present days x2, work hours x100,
  rates x10000) so nothing drifts.
- Analytics read the stored derived fields; they never recompute them from punch times.
- Trend: the days are generated inside the database (`$range` + `$dateAdd`), not in a Python loop. `moving_avg_7d`
  averages the already-rounded (4 dp) `attendance_rate` values shown in the response.
- Logs dated before an employee's `joined_on` still count toward that month's totals (the contract only moves the
  working-day count).
- Concurrency: punch-in relies on the unique `(emp_code, date)` index; punch-out and PATCH use a compare-and-set
  update (PATCH retries a few times, then returns 409).

## Known gaps
- See REVIEW.md for the status of Part A.
