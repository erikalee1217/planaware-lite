# N1 fixture provenance

`n1_298_observed.json` transcribes two records from an authenticated N1 inspection
on 2026-11-17, in their observed order (alighting, then boarding). It is a curated
historical fixture, not a frozen acquisition or a current API response.
No credentials or HTTP headers are included. Source timestamps and ETA
values are historical evidence; both records have StopStatus=1 (not departed).
They do not identify a common vehicle or establish downstream travel time.

`n1_298_scope.json` is explicit regression/demo configuration, not a restriction
embedded in ingestion logic. Configuration expresses an expected route/direction
and allowed stops; it does not independently verify that a route serves them.

All request/fetch times supplied by automated tests and offline README commands are
**synthetic acquisition metadata**, not reconstructed HTTP timing evidence.
Mutations used for other statuses, missing fields, alternate routes and malformed
responses are synthetic tests, not additional observed TDX examples. No live
coverage is claimed for those cases. Only the two historical records above are
the observed fixture. The offline portfolio demo preserves these records and
combines them with explicitly synthetic journey timings.
Another route requires separate verification of direction, ordered stops,
frequency, identifiers and ETA coverage before use.
