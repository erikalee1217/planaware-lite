# Single-transfer risk rules

`planaware.risk.evaluate_transfer(TransferInput)` is a pure function with no API,
database or wall-clock access. All test timestamps and intervals are synthetic.

Run independently: `python3 -m unittest discover -s tests -p test_risk.py -v`.

## Contract

Input is a frozen dataclass containing positive integer `journey_id` and
`transfer_id`, nullable `earliest_arrival`, `latest_arrival`, `next_departure` and
`boarding_buffer`, explicit `evidence_quality`, and required `evaluated_at`.
Times are timezone-aware datetimes; the buffer is a nonnegative timedelta.
Both arrival bounds already include walking and prior transfer durations.
The evaluator adds none of those durations again.

Output is a frozen `TransferAssessment`: the identities, status, reason code,
UTC evaluation timestamp, UTC boarding deadline and exact minimum buffer.
The deadline and buffer are NULL/None for insufficient evidence.
Minimum buffer means `boarding_deadline - latest_arrival`: remaining slack after
the required boarding allowance, not total time before departure. It can be
negative and remains a timedelta with microsecond precision.

## Ordered policy

`boarding_deadline = next_departure - boarding_buffer`

| Condition | Status | Reason |
| --- | --- | --- |
| Evidence missing / unusable / stale | insufficient_evidence | evidence_missing / evidence_unusable / evidence_stale |
| Either arrival bound missing | insufficient_evidence | missing_arrival_interval |
| Departure missing | insufficient_evidence | missing_departure |
| Boarding buffer missing | insufficient_evidence | missing_boarding_buffer |
| Earliest arrival >= deadline | connection_at_risk | deadline_missed |
| Latest arrival >= deadline | connection_at_risk | deadline_overlap |
| Latest arrival < deadline | connection_feasible | within_buffer |

An interval touching or crossing the deadline is at risk, including an exact
zero-buffer boundary. A point interval is valid. No rounding precedes comparison.

Structural validation runs before policy: wrong types, invalid identities or
quality codes, negative buffers, naive times and reversed known bounds raise
`RiskInputError` (a ValueError). Missing optional inputs are evidence gaps rather
than structural errors. If calculation is possible but the deadline falls outside
the supported datetime range, that calculation also raises `RiskInputError`.

## Evidence and time assumptions

Upstream must explicitly classify evidence as usable, missing, unusable or stale,
and supply arrival bounds at the next boarding location. Unknown timing
assumptions must remain missing, not become zero. Usable means those bounds and
the boarding allowance have an adequate basis; this module does not validate
source freshness, schedules, routing, reference provenance or confidence levels.

All arithmetic and comparisons use UTC instants, including mixed offsets and
daylight-saving folds. Callers must resolve ambiguous/nonexistent local wall
times before constructing inputs. Evaluation time is reproducibility metadata;
it does not establish passenger progress or independently expire evidence.

Results are conditional timing assessments, not guaranteed boarding outcomes.
`deadline_missed` describes the supplied interval relative to the deadline, not
proof that a passenger actually missed a service.
The committed N1 fixture contains `StopStatus=1` (not departed) records; it does
not supply a usable downstream arrival interval here.

The evaluator remains a pure function. `planaware.assessment` implements context
validation and assessment persistence separately, mapping `connection_feasible`
to database `on_track`, `connection_at_risk` to `at_risk`, and preserving
`insufficient_evidence`. It retains the original engine result in the saved input
snapshot. The `deadline_missed` reason still maps to `at_risk`, not database
`missed_deadline`. Multi-leg time propagation is not implemented.
