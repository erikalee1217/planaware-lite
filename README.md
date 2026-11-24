# PlanAware Lite

Say you're taking a bus to catch a high-speed train. Give PlanAware a time range
for when you'll reach the boarding point, when the train leaves, and how much
time you need to board. It checks whether the connection looks feasible. If the
evidence is too thin to make that call, it returns `insufficient_evidence`.

Start with the [Bus 298 offline CLI/dbt demo](#bus-298-offline-clidbt-demo).
It walks through snapshot replay, assessments, briefs, and analytics for a trip
via Taipei MRT and intercity bus 1822.

For an interactive example, try the
[Bus 212 local Streamlit pilot](#bus-212-local-streamlit-pilot). It manages
drafts, boarding observations, and assessment history for Bus 212 → walk →
THSR at Nangang.

## Bus 298 offline CLI/dbt demo

Use Python 3.12 (verified), Docker, and an already-installed postgres:14 image.
Install dependencies once from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r analytics/requirements.txt
```

Then run the complete offline demo:

```sh
.venv/bin/python analytics/validate.py --demo
```

The demo replays historical observations from TDX, Taiwan's transport data
platform. N1 is the bus arrival estimate feed PlanAware uses.

Once you've installed the prerequisites, the demo runs without TDX credentials
or image pulls. It creates and removes its own database and writes text/JSON
briefs and a build summary to a new `work/demo-*` directory.

| Demo case | Result | Minimum buffer |
| --- | --- | --- |
| Synthetic arrival 15:23–15:24; assumed latest arrival 15:25 | on_track | +1 min |
| Synthetic arrival 15:24–15:26; same assumed deadline | at_risk | -1 min |
| The demo has a historical bus observation, but too little evidence to estimate when you'll reach the next boarding point. | insufficient_evidence | NULL |

Scenario outcomes stay fixed across runs. Generated IDs and keys may vary.

## Bus 212 local Streamlit pilot

Install the UI extra from the repository root and run Streamlit on loopback.
For standalone `JourneyStore` use, install the PostgreSQL adapter with
`python -m pip install '.[journey]'`.

Before launch, set `PLANAWARE_PILOT_DSN` privately in the app environment.
Use an explicit host or socket directory and a dedicated database named
`planaware_pilot` or `planaware_pilot_*`. The name check helps avoid connecting
to the wrong database. Use a separate role and database permissions to isolate
the pilot, and apply the existing schema before launch.

```sh
python -m pip install '.[ui]'
python -m streamlit run app.py --server.address 127.0.0.1
```

### Journey management and assessment

Select a saved trip from **Saved journey**, or choose **Create new journey**.
Enter walking time, a THSR train number and departure time, and the boarding
buffer. The date applies to both the trip and the train departure. You can leave
timings blank and edit endpoints or source notes in the advanced section.
**Save journey with assumptions** records inputs for the service date.

For an assessment, enter the earliest and latest arrival times at the THSR
boarding point and an evaluation time, all on the saved service date in Taipei
time. Include the bus ride and walk in the arrival window. The optional source
note appears in the saved brief and assessment history. Use **Offline Demo** to
import and inspect the bundled synthetic boarding observation without TDX
credentials.

### Optional live Bus 212 evidence

Only explicit live acquisition contacts TDX.

Set `TDX_CLIENT_ID` and `TDX_CLIENT_SECRET` privately in the launch environment.
Give the pilot role the ingestion function's run and observation permissions.
Open **Live TDX N1 (optional)**, confirm TDX access and quota, and select
**Fetch live TDX N1 once**. The app queries only the pilot boarding stop.
If TDX returns more than five records, the app rejects the response without a
second request.

The evidence card evaluates at fetch time by default. You can set a different
evaluation time in Taipei time. After a failed database write, use
**Replay saved snapshot** to recover from the saved artifact. Keep the app and
its recovery files local.

## Architecture and data contracts

```text
N1 snapshot → validation → frozen artifact → PostgreSQL observations
Saved journey + explicit interval + evidence → assessment history → brief
PostgreSQL history → dbt observation-quality and assessment views
```

- **Snapshots and storage.** PlanAware validates snapshots and stores
  observation history. It keeps source update, TDX update, fetch, and ingestion
  clocks separate. PostgreSQL JSONB preserves source values. The original
  response bytes are outside the storage contract. Unknown values stay `NULL`.
- **Assessment and reports.** Assessments retain evidence and version metadata.
  Identical canonical inputs return the same saved assessment. PlanAware
  rejects conflicting retries. Boarding buffers exclude walking time already
  included in the arrival bounds. An interval that touches or crosses the
  boarding deadline is at risk under the
  [transfer risk rules](docs/transfer-risk-rules.md). Read-only text and JSON
  briefs render saved decisions. Two dbt views read observation and assessment
  history.
- **Journey drafts.** `JourneyStore` creates, reads, and saves pilot drafts.
  Saves require the edit token from the last read. A material edit to an
  assessed journey creates a new draft and preserves the original journey and
  its assessments. Missing walk and train timings stay `NULL`. An unknown
  boarding allowance leaves the draft without a transfer row because the table
  requires a value and source. Supply any missing inputs before assessment.
  Streamlit writes only on explicit actions.
- **Assessment locking.** `JourneyStore.assess_journey(spec)` locks the journey
  before reading its context and holds the lock through persistence in the same
  transaction, so it can run alongside `save_journey`.

## Development and CLI reference

The CLI uses Python 3.10+ and psql. The optional Streamlit UI uses psycopg.
Install the UI extra to include the pilot tests. Tests use isolated PostgreSQL
and mock TDX requests:

```sh
PLANAWARE_JOURNEY_TEST=docker PG_TEST_BACKEND=docker PG_TEST_IMAGE=postgres:14 \
  python3 -m unittest discover -s tests -v
```

`PLANAWARE_JOURNEY_TEST=docker` enables the isolated Docker journey integration
tests. For the local PostgreSQL backend, run
`python3 -m unittest discover -s tests -v` and set `PG_TEST_BINDIR` if needed.
Additional analytics validation deliberately breaks and restores a timing fixture:

```sh
.venv/bin/python analytics/validate.py
```

<details>
<summary>Development usage: local database, assessment JSON and individual CLIs</summary>

Run from the repository root. Direct database access uses PGHOST, PGPORT, PGUSER
and PGPASSFILE. Run this setup once on a new dedicated database. The seed is
optional:

```sh
createdb planaware_demo
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/migrations/001_schema.sql
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/migrations/002_snapshot_write.sql
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/migrations/003_journey_timing_scenarios.sql
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/migrations/004_assessment_write.sql
psql -X -v ON_ERROR_STOP=1 -d planaware_demo -f sql/seeds/001_synthetic_journey.sql
```

Look up the seed's walking-to-intercity-bus transfer:

```sh
psql -X -d planaware_demo -c "SELECT j.journey_id,t.transfer_id,l.transport_mode,l.planned_departure_at FROM journeys j JOIN transfer_constraints t USING(journey_id) JOIN journey_legs l ON l.leg_id=t.to_leg_id WHERE l.transport_mode <> 'walking' ORDER BY j.journey_id,l.leg_order"
mkdir -p work
```

Save the following as work/assessment.json, replacing example IDs 1/4 with the
query results. Use the seed's example date. Bounds describe arrival at the
boarding location.

```json
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
```

Missing bounds may be `null`. For source-backed inputs, set `scenario` to
`source_backed` and supply `observation_ids`, `evidence_leg_id`, and
`evidence_stop_uid`. Caller-supplied downstream bounds require
`interval_basis=validated` and `interval_source`. Match reference and assumption
versions and validity to the saved legs.

```sh
python3 -m planaware.assessment assess work/assessment.json --database planaware_demo
python3 -m planaware.brief --assessment-id ASSESSMENT_ID --database planaware_demo
psql -X -A -t -q -d planaware_demo -c "SELECT row_to_json(a) FROM impact_assessments a WHERE assessment_id=ASSESSMENT_ID" > work/saved-assessments.json
python3 -m planaware.brief --input work/saved-assessments.json --format json
```

Replace ASSESSMENT_ID with the returned ID. File-based briefs need full exported
rows, including `input_snapshot`.

To freeze and replay the historical fixture, use a new output file:

```sh
python3 -m planaware.n1 freeze tests/fixtures/n1_298_observed.json \
  --config tests/fixtures/n1_298_scope.json \
  --requested-at 2026-11-17T19:42:00+08:00 \
  --fetched-at 2026-11-17T19:42:06+08:00 --output work/n1-fixture.json
python3 -m planaware.n1 replay work/n1-fixture.json --database planaware_demo
```

Set TDX_CLIENT_ID/TDX_CLIENT_SECRET privately in the CLI environment. The CLI
does not load `.env` files automatically. For optional live acquisition, check
entitlement, quota, and cost, and use a fresh output file.
`--confirm-free-quota` records the quota check:

```sh
python3 -m planaware.n1 fetch-once --config tests/fixtures/n1_298_scope.json \
  --city Taipei --max-records 2 --timeout 15 --output work/n1-live.json \
  --database planaware_demo --confirm-free-quota
```

For dbt against an existing migrated database, set DBT_USER, DBT_DATABASE and,
if needed, DBT_HOST/DBT_PORT and DBT_ENV_SECRET_PASSWORD privately. The account
needs source SELECT and analytics schema/view permissions:

```sh
.venv/bin/dbt build --project-dir analytics --profiles-dir analytics
```

Views default to planaware_analytics, separate from public. DBT_SCHEMA and
DBT_SOURCE_SCHEMA are configurable.

</details>

## Limitations

- Assessments cover one transfer using caller-supplied arrival bounds, and an
  `on_track` result depends on those inputs and assumptions. A boarding-stop ETA
  cannot establish downstream bounds, and the project leaves route planning,
  multi-leg delay propagation, ETA prediction, and continuous monitoring out of scope.
- The Bus 298 demo replays a [historical TDX fixture](tests/fixtures/README.md)
  with `StopStatus=1` (not departed) and synthetic request clocks, fetch clocks,
  and journey timings. Its feasible and at-risk outcomes use synthetic arrival bounds.
- The default 120-second assessment freshness threshold is illustrative and
  configurable with `freshness_seconds` in CLI inputs. dbt's
  `n1_freshness_seconds` measures quality at retrieval, so it cannot establish
  current usability.
- Each live fetch creates a new artifact and key, while replay uses the original
  clocks without another TDX request. Manually deleting unneeded recovery files
  from the Git-ignored `work/live-snapshots/` directory leaves persisted PostgreSQL
  rows intact.
- Avoid legacy CLI assessments while editing pilot drafts because the CLI does
  not share the pilot lock.
- The `postgres:14` tag can change, and analytics pins only direct dependencies.
  The local PostgreSQL test backend has been verified on macOS.
