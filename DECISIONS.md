# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, 409 on duplicates); `(department, emp_code)` for the filtered, sorted list and department lookups; `joined_on` for headcount. `attendance_logs`: unique `(emp_code, date)` for the punch-in guarantee and per-employee reads; `(date desc, emp_code)` for the list order and month ranges; `(status, date desc, emp_code)` for the status filter; `(emp_code, punch_in desc)` for punch-out. Rejected: `(department, joined_on)`, because the department prefix of the existing index already covers it.

2. **Punch-in race.** Both requests compute the same attendance date and both call `insert_one`. There is no read first. The unique index accepts one insert; the other raises `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$setWindowFields` with `$rank` gives 1, 2, 2, 4. I filter `rank <= limit` afterwards, so both tied employees are returned, ordered by `emp_code`. The response can exceed `limit`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and `$lookup`s their logs. `$unwind` keeps employees with no logs, so each adds 1 to headcount and 0 to everything else.

5. **100x data.** Maintain a pre-aggregated per-employee-per-month collection, updated on every write, and read analytics from it instead of scanning raw logs. Also replace skip/limit with keyset pagination.
