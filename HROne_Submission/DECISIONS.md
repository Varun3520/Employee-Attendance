# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` and a unique compound index on `attendance_logs (emp_code, date)`. They protect employee identity, make duplicate punch-ins race-safe, and support employee/date lookups. Other indexes support date-sorted attendance listing, status/date filters, the latest punch-in query, monthly late ranking, and department/join-date filters. I considered a standalone `late_minutes` index but rejected it because the leaderboard always filters by month; a date-leading compound index is more useful.

2. **Punch-in race.** Two requests can both pass the employee lookup and compute the same date. Both try to insert `(emp_code, date)`. MongoDB's unique index permits only one insert. That request returns 201; the other raises `DuplicateKeyError` and returns 409. Uniqueness does not rely on a pre-check.

3. **Ties.** The leaderboard uses MongoDB `$rank` on `total_late_minutes`, then keeps every row with `rank <= limit`. Employees tied at the cutoff are all returned, ordered by `emp_code` within the tie.

4. **Headcount.** The department summary starts from employees who joined by month-end and preserves employees with no matching logs during lookup/unwind, so they still count once.

5. **100x data.** I would measure execution plans and latency, then materialize daily department metrics if cross-collection trend queries became too expensive.
