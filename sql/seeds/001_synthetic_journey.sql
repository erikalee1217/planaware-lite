-- ALL times, durations and buffers below are SYNTHETIC, not a live itinerary.
-- Identifier provenance: manually verified in prior TDX sampling, 2026-11-17.
-- Intentionally inserts ONE new demo per execution. Apply once to a demo DB.
BEGIN;
DO $$
DECLARE
    demo BIGINT;
    bus_leg BIGINT;
    bus_walk BIGINT;
    metro_leg BIGINT;
    terminal_walk BIGINT;
    coach_leg BIGINT;
BEGIN
    INSERT INTO journeys (journey_name, service_date, target_arrival_at)
    VALUES ('SYNTHETIC DEMO: Taipei to Hsinchu (not a live travel plan)',
            '2026-11-17', '2026-11-17 17:15:00+08')
    RETURNING journey_id INTO demo;

    INSERT INTO journey_legs (
        journey_id, leg_order, transport_mode, planned_departure_at,
        planned_arrival_at, estimated_duration_seconds, duration_assumption_source,
        reference_details
    ) VALUES (
        demo, 2, 'walking', '2026-11-17 14:06:00+08', '2026-11-17 14:10:00+08',
        240, 'SYNTHETIC synthetic-assumptions-v2: bus alighting point to MRT access; not measured',
        '{"origin_name":"捷運萬芳醫院站公車站牌", "destination_name":"萬芳醫院捷運站",
          "assumption_version":"synthetic-assumptions-v2", "reference_data_version":"demo-reference-v2",
          "valid_from":"2026-11-17", "valid_to":"2026-11-17", "times_basis":"synthetic"}'
    ) RETURNING leg_id INTO bus_walk;

    INSERT INTO journey_legs (
        journey_id, leg_order, transport_mode, route_uid, direction,
        origin_stop_uid, destination_stop_uid,
        planned_departure_at, planned_arrival_at, reference_details
    ) VALUES (
        demo, 1, 'bus', 'TPE11023', 0, 'TPE40828', 'TPE40830',
        '2026-11-17 14:00:00+08', '2026-11-17 14:06:00+08',
        '{"identifier_namespace":"TDX Taipei City Bus", "route_name":"298",
          "origin_name":"文山運動中心（興隆）", "destination_name":"捷運萬芳醫院站",
          "identifier_provenance":"verified in prior TDX samples on 2026-11-17",
          "reference_data_version":"demo-reference-v2",
          "assumption_version":"synthetic-assumptions-v2",
          "valid_from":"2026-11-17", "valid_to":"2026-11-17",
          "planned_times_basis":"synthetic demo only; NOT a TDX ETA or timetable"}'
    ) RETURNING leg_id INTO bus_leg;

    INSERT INTO journey_legs (
        journey_id, leg_order, transport_mode, planned_departure_at,
        planned_arrival_at, reference_details
    ) VALUES (
        demo, 3, 'metro', '2026-11-17 14:12:00+08', '2026-11-17 14:42:00+08',
        '{"identifier_namespace":"Taipei Metro public operating station codes; NOT verified TDX IDs",
          "operating_station_path":["BR04", "BR10", "BL15", "BL12"],
          "station_names":["萬芳醫院", "忠孝復興", "台北車站"],
          "transfer":"BR to BL at Zhongxiao Fuxing; internal transfer included in synthetic leg duration",
          "reference_url":"https://web.metro.taipei/pages2026/WebStation/016/8",
          "reference_data_version":"demo-reference-v2",
          "assumption_version":"synthetic-assumptions-v2",
          "valid_from":"2026-11-17", "valid_to":"2026-11-17",
          "planned_times_basis":"synthetic; no real-time MRT observations"}'
    ) RETURNING leg_id INTO metro_leg;

    INSERT INTO journey_legs (
        journey_id, leg_order, transport_mode, planned_departure_at,
        planned_arrival_at, estimated_duration_seconds, duration_assumption_source,
        reference_details
    ) VALUES (
        demo, 4, 'walking', '2026-11-17 14:42:00+08', '2026-11-17 14:57:00+08',
        900, 'SYNTHETIC synthetic-assumptions-v2: MRT platform to separate Taipei Bus Station boarding point; not measured',
        '{"origin_name":"台北車站捷運月台", "destination_name":"臺北轉運站候車點",
          "assumption_version":"synthetic-assumptions-v2", "reference_data_version":"demo-reference-v2",
          "valid_from":"2026-11-17", "valid_to":"2026-11-17", "times_basis":"synthetic"}'
    ) RETURNING leg_id INTO terminal_walk;

    INSERT INTO journey_legs (
        journey_id, leg_order, transport_mode, route_uid, direction,
        origin_stop_uid, destination_stop_uid,
        planned_departure_at, planned_arrival_at, reference_details
    ) VALUES (
        demo, 5, 'intercity_bus', 'THB1822', 0, 'THB237066', 'THB300416',
        '2026-11-17 15:30:00+08', '2026-11-17 17:00:00+08',
        '{"identifier_namespace":"TDX InterCity Bus", "operator_name":"國光客運",
          "origin_name":"臺北轉運站", "destination_name":"新竹轉運站",
          "identifier_provenance":"verified in prior TDX samples on 2026-11-17",
          "reference_data_version":"demo-reference-v2",
          "assumption_version":"synthetic-assumptions-v2",
          "valid_from":"2026-11-17", "valid_to":"2026-11-17",
          "planned_times_basis":"synthetic; 15:30 is NOT a verified published departure"}'
    ) RETURNING leg_id INTO coach_leg;

    INSERT INTO transfer_constraints (
        journey_id, from_leg_id, to_leg_id, min_transfer_minutes, assumption_source
    ) VALUES
        (demo, bus_leg, bus_walk, 0,
         'SYNTHETIC synthetic-assumptions-v2, valid 2026-11-17 only: no extra allowance before explicit walk'),
        (demo, bus_walk, metro_leg, 2,
         'SYNTHETIC synthetic-assumptions-v2, valid 2026-11-17 only: extra access/wait allowance; excludes explicit 4-minute walk'),
        (demo, metro_leg, terminal_walk, 0,
         'SYNTHETIC synthetic-assumptions-v2, valid 2026-11-17 only: no extra allowance before explicit walk'),
        (demo, terminal_walk, coach_leg, 5,
         'SYNTHETIC synthetic-assumptions-v2, valid 2026-11-17 only: additional boarding buffer; excludes explicit 15-minute walk; NOT an operator boarding deadline');
END;
$$;
-- No ingestion runs, ETA observations or assessments are fabricated by this seed.
COMMIT;
