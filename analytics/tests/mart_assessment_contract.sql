select a.assessment_id
from {{ source('operational', 'impact_assessments') }} a
left join {{ ref('mart_transfer_assessments') }} m using (assessment_id)
where m.assessment_id is null
   or m.evaluated_at is distinct from a.evaluated_at
   or m.assessment_status is distinct from a.status
   or (not m.has_supported_snapshot and (
       m.boarding_deadline is not null or m.minimum_buffer_microseconds is not null))
union all
select assessment_id
from {{ ref('mart_transfer_assessments') }}
where has_supported_snapshot and (
       engine_evaluated_at is null or not evaluation_clock_consistent
       or (engine_status = 'connection_feasible' and assessment_status <> 'on_track')
       or (engine_status = 'connection_at_risk' and assessment_status <> 'at_risk')
       or (engine_status = 'insufficient_evidence' and assessment_status <> 'insufficient_evidence')
       or (assessment_status = 'on_track' and (
           not timing_fields_complete or minimum_buffer_microseconds <= 0
           or evidence_quality_at_evaluation is distinct from 'usable'))
       or (assessment_status = 'insufficient_evidence' and (
           boarding_deadline is not null or minimum_buffer_microseconds is not null)))
