{% if target.schema == env_var('DBT_SOURCE_SCHEMA', 'public') %}
  {{ exceptions.raise_compiler_error('Analytics and operational schemas must differ') }}
{% endif %}

with history as (
    select *, coalesce(input_snapshot->>'contract_version' = 'single-transfer-integration-v1', false) as has_supported_snapshot
    from {{ source('operational', 'impact_assessments') }}
), extracted as (
    select
        assessment_id, assessment_key, journey_id, target_transfer_id as transfer_id,
        target_leg_id, evaluated_at, status as assessment_status, reason_code, legacy_status,
        model_version, reference_data_version, assumption_version, evidence_observation_id,
        input_snapshot#>>'{context,journey,journey_name}' as journey_name_at_evaluation,
        has_supported_snapshot,
        case when has_supported_snapshot then input_snapshot#>>'{request,scenario}' end as assessment_scenario,
        case when has_supported_snapshot then input_snapshot#>>'{request,interval_basis}' end as interval_basis,
        case when has_supported_snapshot then input_snapshot#>>'{request,interval_source}' end as interval_source,
        case when has_supported_snapshot then input_snapshot#>>'{engine_input,evidence_quality}' end as evidence_quality_at_evaluation,
        case when has_supported_snapshot then input_snapshot#>>'{result,status}' end as engine_status,
        case when has_supported_snapshot and jsonb_typeof(input_snapshot#>'{quality_issues}') = 'array'
             then input_snapshot->'quality_issues' end as evidence_quality_issues,
        case when input_snapshot#>>'{request,scenario}' = 'synthetic'
               or lower(assumption_version) like '%synthetic%'
               or lower(input_snapshot#>>'{context,journey,journey_name}') like '%synthetic%'
             then true end as synthetic_reference_marker,
        {% for field in ['earliest_arrival', 'latest_arrival', 'next_departure'] %}
        case when has_supported_snapshot and jsonb_typeof(input_snapshot#>'{engine_input,{{ field }}}') = 'string'
             then (input_snapshot#>>'{engine_input,{{ field }}}')::timestamptz end as {{ field }},
        {% endfor %}
        case when has_supported_snapshot and jsonb_typeof(input_snapshot#>'{result,evaluated_at}') = 'string'
             then (input_snapshot#>>'{result,evaluated_at}')::timestamptz end as engine_evaluated_at,
        case when has_supported_snapshot and jsonb_typeof(input_snapshot#>'{result,boarding_deadline}') = 'string'
             then (input_snapshot#>>'{result,boarding_deadline}')::timestamptz end as boarding_deadline,
        case when has_supported_snapshot and jsonb_typeof(input_snapshot#>'{result,minimum_buffer}') = 'number'
             and trunc((input_snapshot#>>'{result,minimum_buffer}')::numeric) = (input_snapshot#>>'{result,minimum_buffer}')::numeric
             then (input_snapshot#>>'{result,minimum_buffer}')::numeric end as minimum_buffer_microseconds,
        case when has_supported_snapshot and jsonb_typeof(input_snapshot#>'{engine_input,boarding_buffer}') = 'number'
             and trunc((input_snapshot#>>'{engine_input,boarding_buffer}')::numeric) = (input_snapshot#>>'{engine_input,boarding_buffer}')::numeric
             then (input_snapshot#>>'{engine_input,boarding_buffer}')::numeric end as boarding_buffer_microseconds
    from history
)
select
    a.*, a.minimum_buffer_microseconds / 1000000::numeric as minimum_buffer_seconds,
    case when engine_evaluated_at is not null then engine_evaluated_at = evaluated_at end as evaluation_clock_consistent,
    (earliest_arrival is not null and latest_arrival is not null and next_departure is not null
     and boarding_buffer_microseconds is not null and boarding_deadline is not null
     and minimum_buffer_microseconds is not null) as timing_fields_complete,
    o.run_id as evidence_run_id, o.source_system as evidence_source_system,
    o.route_uid as evidence_route_uid, o.direction as evidence_direction, o.stop_uid as evidence_stop_uid,
    o.synthetic_fixture_marker as evidence_synthetic_fixture_marker,
    case when assessment_scenario = 'synthetic' then 'synthetic_scenario'
         when evidence_observation_id is not null then 'saved_primary_observation_only'
         else 'no_primary_observation' end as evidence_provenance_scope
from extracted a
left join {{ ref('int_n1_observation_quality') }} o on o.observation_id = a.evidence_observation_id
