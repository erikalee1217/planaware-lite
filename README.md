# PlanAware Lite

## Overview

A delay on the first leg of a journey can affect every connection that follows.
PlanAware Lite explores this with a reference trip: Bus 298 → Taipei MRT →
intercity bus 1822 to Hsinchu.

Live TDX data has been ingested successfully, but the demo's feasible/at-risk
results are based on synthetic arrival intervals. The project evaluates one
transfer from explicit evidence and assumptions; it does not predict the interval.

## Features & Architecture

- Validates bus snapshots, stores observation history and supports frozen replay.
- Persists deterministic transfer assessments with evidence and version metadata.
- Produces read-only text/JSON briefs and two dbt analytical views.
- Provides an offline demo using committed fixtures and an isolated database.

~~~text
N1 snapshot → validation → frozen artifact → PostgreSQL observations
Saved journey + explicit interval + evidence → assessment history → brief
PostgreSQL history → dbt observation-quality and assessment views
~~~

## Quick Start

Use Python 3.12 (verified), Docker, and an already-installed postgres:14 image.
Install dependencies once from the repository root:

~~~sh
python3 -m venv .venv
.venv/bin/python -m pip install -r analytics/requirements.txt
~~~

Then run the complete offline demo:

~~~sh
.venv/bin/python analytics/validate.py --demo
~~~

After prerequisites are installed, the demo needs no TDX credentials, API requests
or image pulls. It creates and removes its own database and saves results under a
new work/demo-* directory, including text/JSON briefs and a build summary.

| Demo case | Result | Minimum buffer |
| --- | --- | --- |
| Synthetic arrival 15:23–15:24; assumed latest arrival 15:25 | on_track | +1 min |
| Synthetic arrival 15:24–15:26; same assumed deadline | at_risk | -1 min |
| Historical N1 observation without a justified downstream interval | insufficient_evidence | NULL |

The source-backed case uses the committed historical `StopStatus=1` (not departed)
sample with synthetic acquisition metadata and journey timings. Generated IDs and
keys may differ between runs, while scenario outcomes stay fixed.

## How It Works

Frozen snapshots allow failed database writes to be retried without fetching the
source again. Identical canonical assessment inputs return the same saved result;
conflicting retries are rejected.

Source update, TDX update, fetch and ingestion clocks remain separate. Unknown
values remain NULL; walking time is not counted again in boarding buffers.
JSONB preserves source values rather than original response bytes.

An arrival interval touching or crossing the boarding deadline is at risk.
See [transfer risk rules](docs/transfer-risk-rules.md). Briefs explain saved
decisions without recalculating them; dbt describes history without changing risk.

## Development & Testing

The operational application uses Python 3.10+ and psql. Tests use isolated
PostgreSQL, with all HTTP mocked:

~~~sh
PG_TEST_BACKEND=docker PG_TEST_IMAGE=postgres:14 python3 -m unittest discover -s tests -v
~~~

For local PostgreSQL binaries instead, run python3 -m unittest discover -s tests -v;
set PG_TEST_BINDIR if needed. Additional analytics validation deliberately breaks
and restores a timing fixture to verify its test:

~~~sh
.venv/bin/python analytics/validate.py
~~~

<details>
<summary>Development usage: local database, assessment JSON and individual CLIs</summary>

Run from the repository root. Direct database access uses PGHOST, PGPORT, PGUSER
and PGPASSFILE. Create a dedicated database; apply migrations and the optional
synthetic seed once, never rerun setup against existing or unrelated data:

~~~sh
createdb planaware_demo
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/migrations/001_schema.sql
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/migrations/002_snapshot_write.sql
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/migrations/003_journey_timing_scenarios.sql
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/migrations/004_assessment_write.sql
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/seeds/001_synthetic_journey.sql
~~~

Find journey/transfer IDs rather than assuming them. For the example below,
choose the seed's walking-to-intercity-bus transfer:

~~~sh
psql -X -d planaware_demo -c "SELECT j.journey_id,t.transfer_id,l.transport_mode,l.planned_departure_at FROM journeys j JOIN transfer_constraints t USING(journey_id) JOIN journey_legs l ON l.leg_id=t.to_leg_id WHERE l.transport_mode <> 'walking' ORDER BY j.journey_id,l.leg_order"
mkdir -p work
~~~

Save the following as work/assessment.json, replacing example IDs 1/4 with the
query results. These intervals and timings are synthetic, valid only for the
seed's example date. Bounds already describe arrival at the boarding location.

~~~json
{
  "journey_id": 1,
  "transfer_id": 4,
  "evaluated_at": "2026-11-17T14:30:00+08:00",
  "reference_version": "demo-reference-v2",
  "assumption_version": "synthetic-assumptions-v2",
  "scenario": "synthetic",
  "earliest_arrival": "2026-11-17T15:23:00+08:00",
  "latest_arrival": "2026-11-17T15:24:00+08:00",
  "interval_basis": "synthetic",
  "interval_source": "Synthetic target-boarding-location interval; includes walking",
  "buffer_semantics": "boarding_only"
}
~~~

Missing bounds may be null. For source-backed inputs, set scenario to source_backed
and supply observation_ids, evidence_leg_id and evidence_stop_uid. A supplied
downstream interval also requires interval_basis=validated and interval_source;
this is an explicit caller-provided basis, not a prediction model in this project.
Reference/assumption versions and validity must match the saved legs. The default
freshness threshold is 120 seconds, configurable with freshness_seconds.

~~~sh
python3 -m planaware.assessment assess work/assessment.json --database planaware_demo
python3 -m planaware.brief --assessment-id ASSESSMENT_ID --database planaware_demo
psql -X -A -t -q -d planaware_demo -c "SELECT row_to_json(a) FROM impact_assessments a WHERE assessment_id=ASSESSMENT_ID" > work/saved-assessments.json
python3 -m planaware.brief --input work/saved-assessments.json --format json
~~~

Replace ASSESSMENT_ID with the returned ID. File-based briefs need full exported
rows, including input_snapshot, rather than condensed demo summaries.

To freeze and replay the historical fixture, use a new output file:

~~~sh
python3 -m planaware.n1 freeze tests/fixtures/n1_298_observed.json \
  --config tests/fixtures/n1_298_scope.json \
  --requested-at 2026-11-17T19:42:00+08:00 \
  --fetched-at 2026-11-17T19:42:06+08:00 --output work/n1-fixture.json
python3 -m planaware.n1 replay work/n1-fixture.json --database planaware_demo
~~~

These request/fetch times are synthetic. Live fetching is optional and separate:
privately set TDX_CLIENT_ID/TDX_CLIENT_SECRET, check entitlement/quota/cost, then:

~~~sh
python3 -m planaware.n1 fetch-once --config tests/fixtures/n1_298_scope.json \
  --city Taipei --max-records 2 --timeout 15 --output work/n1-live.json \
  --database planaware_demo --confirm-free-quota
~~~

The flag records the operator's quota check. .env files are not loaded
automatically. A new fetch uses a fresh artifact/key; database retry uses replay.

For dbt against an existing migrated database, set DBT_USER, DBT_DATABASE and,
if needed, DBT_HOST/DBT_PORT and DBT_ENV_SECRET_PASSWORD privately. The account
needs source SELECT and analytics schema/view permissions:

~~~sh
.venv/bin/dbt build --project-dir analytics --profiles-dir analytics
~~~

Views default to planaware_analytics, separate from public. DBT_SCHEMA and
DBT_SOURCE_SCHEMA are configurable. The observation view's n1_freshness_seconds
variable controls retrieval-time quality; it does not establish current usability.

</details>

## Limitations

- Scope is limited to single-transfer evaluation from caller-supplied intervals.
  Route planning, multi-leg delay propagation, ETA prediction, and continuous
  monitoring are out of scope.
- Feasible and at-risk demo results use synthetic arrival intervals. An
  `on_track` result is conditional on the supplied evidence and assumptions.
  The default 120-second evidence freshness threshold is configurable and
  illustrative.
- `postgres:14` is a mutable image tag, and `analytics/requirements.txt` pins
  direct dependencies only. The local PostgreSQL test backend has
  been verified on macOS.
- Historical fixture provenance is documented in
  [tests/fixtures/README.md](tests/fixtures/README.md).
