# Starter Review

| #   | Where                            | Defect and symptom                                                                                                                                       | Fix                                                                                                            |
| --- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 1   | `health`                         | Reported ready without contacting MongoDB, so an unavailable database still appeared healthy.                                                            | Ping MongoDB and return 503 on a PyMongo failure.                                                              |
| 2   | `EmployeeIn`, `create_employee`  | Employee inputs lacked the documented formats and bounds; check-then-insert alone could race, and a duplicate-key race was not translated to 409.        | Validate the request, create a unique `emp_code` index, and map duplicate-key errors to 409.                   |
| 3   | Employee create/list responses   | `created_at` was a naive BSON datetime in one path and an ISO datetime in list results, rather than epoch milliseconds.                                  | Store UTC and serialize `created_at` to integer epoch milliseconds in both responses.                          |
| 4   | `compute_late_minutes`, punch-in | Overnight punches between shift end and the next shift start could be compared to the previous day's shift start and reported thousands of minutes late. | Use `shift_end` to distinguish previous-shift punches from same-day pre-shift punches.                         |
| 5   | Punch-in                         | A check-then-insert race could create duplicate attendance without a database constraint; the response also exposed an unnecessary `id`.                 | Add a unique (`emp_code`, `date`) index, handle duplicate-key errors, and return only contract fields.         |
| 6   | `list_attendance`                | Loaded and paged the whole collection in Python and returned internal Mongo IDs.                                                                         | Count and page in MongoDB, exclude `_id`, and normalize required response fields.                              |
| 7   | Remaining contract routes        | Punch-out, regularization, analytics, and explain behavior were absent from the starter.                                                                 | Implemented the routes described in `openapi.yaml`, with atomic updates for punch-out and history concurrency. |
| 8   | Attendance query validation      | Invalid date strings, reversed ranges, and unsupported statuses were not rejected consistently.                                                          | Validate date formats/order and constrain status values.                                                       |

## Checks

Focused execution checks pass for the late grace boundary (10 minutes versus 10 minutes and 1 second), overtime below
and at 30 minutes, rounded work hours around 4.50, overnight attendance dates/lateness/overtime, regularization audit
deltas, and employee duplicate/readiness behavior. Live Swagger checks exercised attendance flows and analytics. MongoDB
explain checks found `IXSCAN` and no `COLLSCAN` on the five primary query surfaces.

## Reviewed, Not a Defect

- `compute_work_hours` uses decimal half-up rounding, matching R4.
- `compute_overtime` measures from the scheduled shift end and applies the 30-minute minimum, including next-day end
  times for overnight shifts.
- Missing `half_day` and `history` values in seeded documents are valid storage defaults; API responses normalize them.

## Known Limitations and Test Data

MongoDB Atlas rejected `explain` for the collectionless `$documents` pipeline used to densify trend dates. The trend
explain endpoint therefore explains its indexed attendance-range lookup and employee join; it does not explain the
entire gap-filling pipeline. A configured local database also contains two additional `2026-10-15` test records for
`EMP0001` and `EMP0006`; the `EMP0001` record has an audit entry by `copilot-test`. They are not part of the sample
baseline. The sample seed script deletes and reloads both collections, so use it only on a disposable database.
