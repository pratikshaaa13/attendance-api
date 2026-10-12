# REVIEW.md

Defects found in the starter `app/main.py` (line numbers are from the starter). Each one lists where it is, what is wrong,
how you would notice it, and how I fixed it.

## Defects

**1. MongoClient has no time zone (L22)**
- Wrong: no `tz_aware=True`, so dates from the database have no time zone and get mixed with IST dates.
- Notice: punch-out crashes with "can't subtract offset-naive and offset-aware datetimes" (500).
- Fix: use `tz_aware=True`. All times are UTC in code and converted to IST only when a rule needs it.

**2. Punch-in time uses the server's time zone (`punch_in`, L102)**
- Wrong: `datetime.fromtimestamp(ms/1000)` uses the server's time zone and keeps fractions of a second.
- Notice: on a UTC server, a 10:05 IST punch is stored as 04:35.
- Fix: convert epoch ms to UTC and cut to whole seconds (`ms_to_dt`).

**3. Wrong attendance date (`punch_in`, L103)**
- Wrong: `date` is the server's date, not the IST date. Overnight shifts are not handled.
- Notice: a punch at 00:30 IST on 7 July gets date 6 July.
- Fix: `attendance_date()` uses the IST date and moves an early punch of an overnight shift to the previous day.

**4. Late minutes are wrong (`compute_late_minutes`, L33-37)**
- Wrong: the shift start is built on the wrong date and zone, and the grace check uses whole minutes.
- Notice: 09:40:01 gives 0 (should be 10). A 10:05 IST punch gives 0 (should be 35).
- Fix: work in seconds. Late only if more than 600 seconds, then `seconds // 60`. Shift start comes from `shift_window()`.

**5. Work hours rounding (`compute_work_hours`, L40-41)**
- Wrong: Python `round()` rounds halves to even. The rule says half-up.
- Notice: 4050 seconds = 1.125 hours gives 1.12 (should be 1.13).
- Fix: exact `Fraction` maths with half-up rounding.

**6. Overtime is wrong (`compute_overtime`, L44-47)**
- Wrong: there is no 30-minute minimum, and an overnight shift end is placed on the wrong day.
- Notice: out at 18:45 for an 18:30 shift gives 15 (should be 0). A 22:00-06:00 shift, out 06:40, gives 1480 (should be 40).
- Fix: `shift_window()` puts an overnight end on the next day. Overtime is 0 below 30 minutes.

**7. Half day is never calculated (missing)**
- Wrong: `half_day` is never worked out after punch-out.
- Notice: short days are never marked as half day.
- Fix: calculated on the rounded work hours. Below 4.50 means half day.

**8. Health check always says ok (`health`, L72-74)**
- Wrong: it returns ok even when MongoDB is down.
- Notice: stop MongoDB and `/health` still says ok.
- Fix: ping MongoDB and return 503 if it fails.

**9. Duplicate employees possible (`create_employee`, L79-83)**
- Wrong: it checks first, then inserts, and there is no unique index.
- Notice: parallel creates with the same `emp_code` can all succeed.
- Fix: unique index on `emp_code`. A duplicate-key error becomes 409.

**10. `created_at` has the wrong type (`create_employee`, L82)**
- Wrong: it is local naive time, and it is returned as a text date, not epoch milliseconds.
- Notice: `created_at` comes back as an ISO string.
- Fix: store a UTC date and return epoch milliseconds.

**11. No input validation (`EmployeeIn`, `PunchInIn`, L53-66)**
- Wrong: `emp_code`, email, shift times, `joined_on`, status and `punched_at` are not checked.
- Notice: `shift_start="25:99"`, `status="banana"`, or `punched_at` in seconds returns 201.
- Fix: Pydantic rules for each field. Bad input returns 422.

**12. `punched_at` of 0 is ignored (`punch_in`, L102)**
- Wrong: `if body.punched_at` treats 0 as "not given".
- Notice: `punched_at: 0` silently uses the current time.
- Fix: check `is not None`. The value must be in the valid epoch-ms range, so 0 returns 422.

**13. Unknown employee crashes (`punch_in`, L101 and L113)**
- Wrong: `find_one` returns `None`, then `emp["shift_start"]` fails.
- Notice: punch in with an unknown `emp_code` returns 500.
- Fix: look up the employee first and return 404.

**14. Duplicate punch-ins possible (`punch_in`, L104-105)**
- Wrong: it checks first, then inserts, and there is no unique index.
- Notice: parallel identical punch-ins create more than one record.
- Fix: unique `(emp_code, date)` index. A duplicate-key error becomes 409.

**15. Response leaks `id` and returns text dates (`punch_in` L119-121, `list_attendance` L149)**
- Wrong: it returns an `id` made from Mongo's `_id`, and dates as text.
- Notice: the response has `"id": "6650..."`.
- Fix: return only the contract fields, with epoch milliseconds.

**16. Page numbering is off by one (`list_employees`, L93)**
- Wrong: `skip = page * page_size` skips the first page.
- Notice: `?page=1` never shows the first 20 employees.
- Fix: `skip = (page - 1) * page_size`.

**17. `total` ignores the filter (`list_employees`, L94)**
- Wrong: it counts all employees, not just the filtered ones.
- Notice: `?department=HR` returns 3 items but `total` is 300.
- Fix: count with the same filter.

**18. No sort and no page checks (`list_employees`, L95)**
- Wrong: results have no order, and `page` and `page_size` are not checked.
- Notice: `page=0` gives 500. `page_size=100000` returns everything.
- Fix: sort by `emp_code`. `page >= 1` and `page_size` from 1 to 100.

**19. Attendance list loads everything into memory (`list_attendance`, L144-147)**
- Wrong: it reads every record, then sorts and slices in Python. Ties are not sorted by `emp_code`.
- Notice: slow at 100,000 records, and same-date rows come back in random order.
- Fix: MongoDB does the sort, skip and limit: date descending, then `emp_code`. Indexes support it.

**20. Attendance filters are not checked (`list_attendance`, L125-143)**
- Wrong: status and dates are not validated. Old records without `history` or `half_day` are returned as they are.
- Notice: `?status=NOPE` returns 200 with nothing. `date_from` after `date_to` is accepted.
- Fix: validate status and dates (422). A missing `history` becomes `[]` and a missing `half_day` becomes `false`.

**21. No indexes and no database error handling (whole file)**
- Wrong: no indexes are created, and database errors are not handled.
- Notice: every query scans the whole collection, and a MongoDB outage gives 500.
- Fix: create all indexes at startup. Database errors return 503.

## Checked and fine

- `load_dotenv()`: it does not override real environment variables, which is what the contract asks for.
- `IST = UTC+05:30`: the offset is correct.
- Status code 201 on create and punch-in: matches the contract.
- `date` as a text string, and the starting values `history: []`, `half_day: False`, `overtime_minutes: 0`: match DATA_MODEL.md.
- `{"_id": 0}` in `list_employees` and `doc.pop("_id")` after insert: needed, because `_id` must not be returned. (The
  `id` added back later is defect 15.)
