# Decisions

1. **Indexes.** `employees.emp_code` is unique for employee identity and lookups; `(department, joined_on)` and `joined_on` support department/month and daily headcount filters. Unique `(emp_code, date)` enforces one attendance record per employee per day. Attendance indexes support date-sorted listing, employee punch-in lookup, status/date filters, date-range analytics, and lateness aggregation. I did not add a standalone status index because the compound status/date index serves that filter with less redundant storage.

2. **Punch-in race.** Both requests may pass the initial employee/date check, but both attempt the insert. The unique compound index permits only one record. The loser receives `DuplicateKeyError`, which the API converts to 409; the winner returns 201.

3. **Ties.** The leaderboard uses MongoDB `$rank`, then filters by `rank <= limit`. Employees tied at the cutoff share the rank and all are returned; the next rank is skipped.

4. **Headcount.** Department summary starts from eligible employees and looks up attendance. Preserving employees with no matching logs keeps them in the final per-department headcount.

5. **At 100x scale.** I would profile the largest aggregations on representative data and consider maintained monthly summaries to avoid repeatedly scanning raw attendance for analytics.
