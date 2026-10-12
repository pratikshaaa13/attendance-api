# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `(department, joined_on)` and `(joined_on)` for headcount and trend; `(department, emp_code)` for the sorted list. `attendance_logs`: unique `(emp_code, date)` (race guard, PATCH lookup, monthly summary); `(date desc, emp_code)` for list, leaderboard and trend; `(status, date desc, emp_code)` for status filters; `(emp_code, punch_in desc)` to find the record to punch out. Rejected: copying `department` onto every log and indexing it. It would speed up the summary but goes stale and needs a second write.

2. **Punch-in race.** Both requests find the employee and compute the same date, then both call `insert_one`. The unique `(emp_code, date)` index lets one through; the other gets a duplicate-key error, which I return as 409. No check-then-insert.

3. **Ties.** `$rank` in `$setWindowFields` gives standard competition ranking, then I keep `rank <= limit`. Both tied people at the cutoff come back, so the list can exceed `limit`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end), looks up each person's logs, and unwinds with `preserveNullAndEmptyArrays`. Someone with no logs stays and counts once.

5. **100x data.** The summary does one `$lookup` per employee. I would keep monthly per-employee totals in a small collection, updated on punch and correction, and run analytics from that.
