-- Historical grain and original clocks must survive; no best/latest deduplication.
select o.observation_id
from {{ source('operational', 'source_observations') }} o
left join {{ ref('int_n1_observation_quality') }} q using (observation_id)
where q.observation_id is null
   or q.source_updated_at is distinct from o.source_updated_at
   or q.fetched_at is distinct from o.fetched_at
   or q.ingested_at is distinct from o.ingested_at
union all
select observation_id
from {{ ref('int_n1_observation_quality') }}
where (is_normal_eta_candidate and (
       stop_status is distinct from 0 or estimate_seconds is null or estimate_seconds < 0
       or source_updated_at is null or source_age_at_fetch_seconds < 0
       or source_age_at_fetch_seconds > {{ var('n1_freshness_seconds') }}
       or run_status <> 'completed' or invalid_raw_eta or raw_normalized_mismatch))
   or (source_updated_at is null and source_age_at_fetch_seconds is not null)
