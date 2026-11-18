{% set freshness = var('n1_freshness_seconds') %}
{% if freshness is not integer or freshness <= 0 %}
  {{ exceptions.raise_compiler_error('n1_freshness_seconds must be a positive integer') }}
{% endif %}
{% if target.schema == env_var('DBT_SOURCE_SCHEMA', 'public') %}
  {{ exceptions.raise_compiler_error('Analytics and operational schemas must differ') }}
{% endif %}

with observations as (
    select
        o.observation_id, o.run_id, o.record_index, r.run_key, r.status as run_status,
        o.source_system, o.route_uid, o.direction, o.stop_uid,
        o.source_updated_at, o.fetched_at, o.ingested_at,
        o.estimate_seconds, o.stop_status,
        o.estimate_seconds is not null as eta_present,
        o.source_updated_at is not null as source_time_present,
        extract(epoch from (o.fetched_at - o.source_updated_at)) as source_age_at_fetch_seconds,
        extract(epoch from (o.ingested_at - o.fetched_at)) as retrieval_to_ingestion_seconds,
        case when o.raw_payload->'synthetic_test_only' = 'true'::jsonb then true end as synthetic_fixture_marker,
        case when o.raw_payload ? 'EstimateTime' and jsonb_typeof(o.raw_payload->'EstimateTime') <> 'null'
             then case when jsonb_typeof(o.raw_payload->'EstimateTime') = 'number'
                  then (o.raw_payload->>'EstimateTime')::numeric < 0
                    or trunc((o.raw_payload->>'EstimateTime')::numeric) <> (o.raw_payload->>'EstimateTime')::numeric
                  else true end
             else false end as invalid_raw_eta,
        (o.raw_payload->>'RouteUID' is distinct from o.route_uid
         or o.raw_payload->'Direction' is distinct from to_jsonb(o.direction)
         or o.raw_payload->>'StopUID' is distinct from o.stop_uid
         or o.raw_payload->'StopStatus' is distinct from to_jsonb(o.stop_status)
         or o.raw_payload->'EstimateTime' is distinct from to_jsonb(o.estimate_seconds)) as raw_normalized_mismatch
    from {{ source('operational', 'source_observations') }} o
    join {{ source('operational', 'ingestion_runs') }} r using (run_id)
), classified as (
    select *, case
        when run_status <> 'completed' then 'incomplete_run'
        when stop_status is null then 'missing_status'
        when stop_status not in (0, 1, 2, 3, 4) then 'unsupported_status'
        when stop_status <> 0 then 'non_normal_status'
        when invalid_raw_eta then 'invalid_raw_eta'
        when not eta_present then 'missing_eta'
        when raw_normalized_mismatch then 'raw_normalized_mismatch'
        when not source_time_present then 'missing_source_time'
        when source_updated_at > fetched_at then 'source_after_retrieval'
        when ingested_at < fetched_at then 'ingestion_before_retrieval'
        when source_age_at_fetch_seconds > {{ freshness }} then 'stale_at_retrieval'
        else 'normal_eta_candidate'
    end as quality_category
    from observations
)
select *, quality_category = 'normal_eta_candidate' as is_normal_eta_candidate
from classified
