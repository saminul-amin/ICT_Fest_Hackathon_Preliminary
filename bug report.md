# bug report.md

## Summary

This report documents all identified API contract, concurrency, caching, authentication, reporting, and Docker issues addressed for the preliminary-round submission, with each entry mapped to the affected code area and its corresponding fix.

Validation performed after fixes:

- Static compilation completed successfully for the application and test suite.
- Automated API contract regression tests completed with a 100% pass rate.
- Docker Compose configuration, image build, and container startup were verified successfully.
- Containerized regression tests completed with a 100% pass rate.
- Black-box API validation through `localhost:8000` confirmed registration, login, room creation, and booking creation with the expected price calculation.

## Bugs Fixed

### 1. Offset datetimes were not converted to UTC

- Files/lines: `app/timeutils.py:11-21`
- Bug: offset-aware datetimes were made naive by dropping `tzinfo`, so `10:00+02:00` was stored as `10:00 UTC` instead of `08:00 UTC`.
- Why incorrect: the contract requires input datetimes with offsets to be converted to UTC before storage or comparison.
- How fixed: `parse_input_datetime()` now uses `astimezone(timezone.utc)` before removing `tzinfo`; `iso_utc()` also normalizes aware values before rendering.

### 2. Duplicate registration did not return `409 USERNAME_TAKEN`

- Files/lines: `app/routers/auth.py:27-59`
- Bug: registering an existing username in the same organization returned the existing user response.
- Why incorrect: the contract requires duplicate usernames within an org to return `409 USERNAME_TAKEN`.
- How fixed: registration now raises `AppError(409, "USERNAME_TAKEN", ...)` for existing usernames and performs registration inside a lock.

### 3. Access token lifetime was 54,000 seconds instead of 900 seconds

- Files/lines: `app/auth.py:51-63`
- Bug: the access-token lifetime multiplied minutes by 60 and then passed the result as minutes.
- Why incorrect: the contract requires `exp - iat` to equal exactly 900 seconds.
- How fixed: access-token expiration now uses `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)`.

### 4. Logout did not revoke the presented access token

- Files/lines: `app/auth.py:88-116`, `app/routers/auth.py:97-100`
- Bug: logout stored token `jti`, but request validation checked `sub` against the revoked set.
- Why incorrect: a logged-out access token remained usable, violating immediate invalidation.
- How fixed: revoked tokens are checked by `jti`, with a lock around the revoked-token set.

### 5. Refresh tokens were reusable

- Files/lines: `app/auth.py:25-27`, `app/auth.py:96-103`, `app/routers/auth.py:81-94`
- Bug: refresh tokens were decoded but never marked as consumed.
- Why incorrect: the contract requires refresh tokens to be single-use; reuse must return `401`.
- How fixed: used refresh-token `jti` values are tracked under a lock, and reuse is rejected before issuing rotated tokens.

### 6. Booking start time allowed a five-minute grace window

- Files/lines: `app/routers/bookings.py:85-100`, `app/routers/bookings.py:113-115`
- Bug: past starts were only rejected if more than five minutes old.
- Why incorrect: `start_time` must be strictly in the future with no grace window.
- How fixed: booking creation now compares `start <= now` inside the booking write lock and returns `400 INVALID_BOOKING_WINDOW`.

### 7. Booking duration validation missed zero, negative, and below-minimum durations

- Files/lines: `app/routers/bookings.py:85-100`
- Bug: the old validation did not enforce `end_time > start_time` or the minimum one-hour duration.
- Why incorrect: the contract requires `end_time` to be strictly after `start_time` and duration to be a whole number of hours from 1 to 8.
- How fixed: `_duration_hours()` now rejects `end <= start`, fractional-hour durations, durations under 1 hour, and durations over 8 hours.

### 8. Back-to-back bookings were incorrectly rejected

- Files/lines: `app/routers/bookings.py:44-56`
- Bug: conflict detection used inclusive comparisons.
- Why incorrect: the contract defines overlap as `existing.start_time < new.end_time AND new.start_time < existing.end_time`; back-to-back bookings must be allowed.
- How fixed: conflict detection now uses the exact strict-overlap predicate in the database query.

### 9. Double-booking checks were not concurrency-safe

- Files/lines: `app/routers/bookings.py:21`, `app/routers/bookings.py:113-151`
- Bug: conflict check and booking insert happened as separate unprotected operations.
- Why incorrect: concurrent overlapping requests could both pass the check and commit confirmed bookings.
- How fixed: booking creation now runs conflict check, quota check, reference generation, insert, commit, and cache invalidation inside `_booking_lock`.

### 10. Booking quota checks were not concurrency-safe

- Files/lines: `app/routers/bookings.py:59-75`, `app/routers/bookings.py:113-151`
- Bug: quota count and insert were not atomic.
- Why incorrect: concurrent requests could create more than three confirmed bookings in `(now, now + 24h]`.
- How fixed: quota counting and booking creation now happen under the same booking lock.

### 11. Rate limiting was not concurrency-safe

- Files/lines: `app/services/ratelimit.py:10-29`
- Bug: the rolling-window bucket dict/list was updated without synchronization.
- Why incorrect: concurrent requests could overwrite each other's bucket updates and bypass the 20-per-60-seconds limit.
- How fixed: rate-limit updates now run under `_bucket_lock`; all attempts are appended before the limit decision.

### 12. Booking reference codes were not guaranteed unique

- Files/lines: `app/services/reference.py:1-10`, `app/models.py:55`, `app/routers/bookings.py:127-147`
- Bug: reference codes came from an unsynchronized in-memory counter and the database did not enforce uniqueness.
- Why incorrect: concurrent requests or process restarts could reuse a `reference_code`.
- How fixed: codes now use UUID-backed values, `Booking.reference_code` is unique in the database, and booking creation retries on unique conflicts.

### 13. `GET /bookings` pagination and ordering were wrong

- Files/lines: `app/routers/bookings.py:157-177`
- Bug: results were sorted descending, page 1 skipped the first page, and the requested `limit` was ignored.
- Why incorrect: the contract requires ascending `start_time`, ascending `id` ties, `(page - 1) * limit` offset, and caller-provided `limit`.
- How fixed: the query now orders by `start_time.asc(), id.asc()`, offsets by `(page - 1) * limit`, and applies `.limit(limit)`.

### 14. Members could read other members' bookings in the same org

- Files/lines: `app/routers/bookings.py:180-206`
- Bug: booking detail lookup scoped by org but did not enforce member ownership.
- Why incorrect: members may read only their own bookings; another member's booking id must behave as `404 BOOKING_NOT_FOUND`.
- How fixed: non-admin users now receive `404 BOOKING_NOT_FOUND` when the booking owner is different.

### 15. Booking detail returned `created_at` as `start_time`

- Files/lines: `app/routers/bookings.py:197-206`
- Bug: the detail endpoint overwrote serialized `start_time` with `created_at`.
- Why incorrect: the response contract requires the booking's actual `start_time`.
- How fixed: the overwrite was removed; the shared serializer's real booking start time is returned.

### 16. The 48-hour refund boundary was wrong

- Files/lines: `app/routers/bookings.py:230-237`
- Bug: refund logic used floored hours and required `> 48` for a 100% refund.
- Why incorrect: the contract requires `notice >= 48 hours` to receive 100%.
- How fixed: refund tiers now compare the full `timedelta` with `>= timedelta(hours=48)` and `>= timedelta(hours=24)`.

### 17. Less-than-24-hour cancellations refunded 50% instead of 0%

- Files/lines: `app/routers/bookings.py:230-237`
- Bug: the final refund branch returned 50%.
- Why incorrect: the contract requires `notice < 24 hours` to receive 0%.
- How fixed: the final branch now sets `refund_percent = 0`.

### 18. Refund rounding was not half-up and response/log amounts could differ

- Files/lines: `app/services/refunds.py:14-24`, `app/routers/bookings.py:239-257`
- Bug: the response used Python `round()` and the refund log used truncation.
- Why incorrect: the contract requires nearest-cent half-up rounding and the response amount must equal the stored `RefundLog` amount.
- How fixed: refund amount is calculated once with integer half-up arithmetic in `log_refund()`, flushed to the DB, and the cancel response returns that same stored amount.

### 19. Cancellation could create multiple refund logs

- Files/lines: `app/models.py:66`, `app/routers/bookings.py:215-250`, `app/services/refunds.py:22-23`
- Bug: refund insert committed before booking status changed, and `RefundLog.booking_id` was not unique.
- Why incorrect: concurrent cancel requests could create multiple refund logs for one cancelled booking.
- How fixed: cancellation now checks status, inserts refund, changes booking status, and commits under `_booking_lock`; `RefundLog.booking_id` is unique.

### 20. Usage reports could become stale after booking or room changes

- Files/lines: `app/cache.py:1-34`, `app/routers/bookings.py:149-151`, `app/routers/rooms.py:54-58`
- Bug: usage reports were cached and not invalidated on all report-visible changes.
- Why incorrect: `GET /admin/usage-report` must reflect current state immediately.
- How fixed: report/availability cache getters now intentionally return `None`, making reads database-backed; create/cancel/room changes also call invalidation hooks.

### 21. Availability could stay stale after cancellation

- Files/lines: `app/cache.py:25-34`, `app/routers/bookings.py:245-250`, `app/routers/rooms.py:70-101`
- Bug: cancellation did not invalidate cached availability.
- Why incorrect: cancelled bookings must disappear from room availability immediately.
- How fixed: availability caching is disabled for correctness, and cancellation invalidates the room/date hook.

### 22. Room stats were in-memory instead of database-derived

- Files/lines: `app/services/stats.py:1-22`, `app/routers/rooms.py:104-116`
- Bug: stats came from an in-memory counter that reset on process restart and could drift.
- Why incorrect: stats must always equal values derivable from confirmed bookings.
- How fixed: `GET /rooms/{id}/stats` now aggregates confirmed booking count and revenue directly from the database.

### 23. Room stats updates were not concurrency-safe

- Files/lines: `app/services/stats.py:1-22`
- Bug: old read-modify-write counter updates could lose increments or decrements.
- Why incorrect: concurrent activity could make stats disagree with bookings.
- How fixed: incremental in-memory counters were removed from the correctness path; stats are derived from the booking table.

### 24. Admin export could leak cross-org bookings

- Files/lines: `app/services/export.py:23-47`, `app/routers/admin.py:65-72`
- Bug: `include_all=true&room_id=<id>` used a raw room-id lookup without org scoping, and cross-org room ids returned empty CSVs instead of 404.
- Why incorrect: all export paths must be tenant-scoped, and cross-org resource ids must behave as non-existent.
- How fixed: export now validates `room_id` belongs to the admin's org and returns `404 ROOM_NOT_FOUND` otherwise; all exported rows are fetched through org-scoped queries.

### 25. Notification locks could deadlock create/cancel requests

- Files/lines: `app/services/notifications.py:21-32`
- Bug: create acquired email then audit locks, while cancel acquired audit then email locks.
- Why incorrect: concurrent create/cancel requests could deadlock and violate the liveness rule.
- How fixed: both notification paths now acquire locks in the same order, and the simulated slow sleeps were removed.

### 26. Local test setup was incomplete

- Files/lines: `requirements.txt:1-6`
- Bug: the README instructed `pip install -r requirements.txt` followed by `pytest`, but `pytest` was not in requirements.
- Why incorrect: a fresh environment following the documented test path could not run tests.
- How fixed: `pytest==8.2.2` was added to `requirements.txt`.

### 27. Smoke tests did not cover contract behavior

- Files/lines: `tests/test_smoke.py:1-183`
- Bug: the previous test only covered a happy path and missed contract-critical behavior.
- Why incorrect: the most important black-box API bugs could pass local tests.
- How fixed: the test suite now checks auth TTL/logout/refresh reuse, duplicate registration, booking validation, back-to-back bookings, conflict detection, pagination, booking detail, refunds, re-cancel behavior, stats, availability, and cross-org export scoping.

### 28. Docker build context included local test/cache artifacts

- Files/lines: `.dockerignore:1-9`
- Bug: `.dockerignore` did not exclude `.pytest_cache/`, and Docker failed while sending the build context with `Access is denied` for the local `.pytest_cache` directory. The attached PDF was also included in the build context unnecessarily.
- Why incorrect: the PDF requires the grader to build the submitted repository in Docker; local cache artifacts must not make the Docker build fail or bloat the image context.
- How fixed: `.dockerignore` now excludes `.pytest_cache/` and `ICT_Fest_Hackathon_Preliminary.pdf`, after which `docker compose build` completed successfully.
