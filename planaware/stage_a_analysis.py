"""Read Stage A captures offline and summarize only candidate evidence."""
import argparse
from collections import Counter
from datetime import timedelta
from pathlib import Path

from .n1 import dump_stable_json, parse_offset_timestamp, parse_strict_json
from .stage_a import DIRECTORY, FORMAT, STOPS

LABELS = ("vehicle_matched", "trip_candidate", "ambiguous", "unmatched")
WINDOW = timedelta(minutes=5)
ETA_TOLERANCE = timedelta(seconds=90)


def plate(row):
    value = row.get("plate")
    return value.strip().upper() if isinstance(value, str) else ""


def clock(value):
    try:
        return parse_offset_timestamp(value, "observation time")
    except ValueError:
        return None


def usable_time(row, field):
    observed = clock(row.get(field))
    fetched = clock(row.get("fetched_at"))
    if observed and fetched and timedelta(0) <= fetched - observed <= WINDOW:
        return observed
    return None


def arrivals(rows):
    by_stop = {stop: {} for stop in STOPS}
    for row in rows:
        if row["endpoint"] == "A2" and type(row.get("event_type")) is int and row["event_type"] == 1:
            observed = usable_time(row, "gps_time")
            if observed and row.get("stop_uid") in by_stop:
                starts = by_stop[row["stop_uid"]].setdefault(observed, set())
                start = clock(row.get("trip_start_time"))
                kind = row.get("trip_start_type")
                if type(kind) is int and kind == 0 and start and start <= observed:
                    starts.add(start)
    return by_stop


def supports_arrival(row, event):
    observed = usable_time(row, "source_updated_at")
    eta = row.get("estimate_seconds")
    return (observed is not None and type(eta) is int and 0 <= eta <= WINDOW.total_seconds()
            and type(row.get("stop_status")) is int and row["stop_status"] == 0
            and observed <= event <= observed + WINDOW
            and abs((event - observed) - timedelta(seconds=eta)) <= ETA_TOLERANCE)


def classify(row, peers):
    observed = usable_time(row, "source_updated_at")
    if not plate(row) or observed is None:
        return "unmatched"
    related = []
    for other in peers:
        if other is row or plate(other) != plate(row):
            continue
        is_n1 = other["endpoint"] == "N1"
        if is_n1 and other["stop_uid"] == row["stop_uid"]:
            continue
        at = usable_time(other, "source_updated_at" if is_n1 else "gps_time")
        if at is not None and abs(at - observed) <= WINDOW:
            related.append(other)
    if not related:
        return "unmatched"
    events = arrivals(related)
    if any(len(times) > 1 for times in events.values()):
        return "ambiguous"
    if all(events.values()):
        board, alight = (next(iter(events[stop])) for stop in STOPS)
        board_starts, alight_starts = events[STOPS[0]][board], events[STOPS[1]][alight]
        if len(board_starts | alight_starts) > 1:
            return "ambiguous"
        target = board if row["stop_uid"] == STOPS[0] else alight
        other_target = alight if row["stop_uid"] == STOPS[0] else board
        if (board_starts and board_starts == alight_starts
            and timedelta(0) < alight - board <= WINDOW and supports_arrival(row, target)
            and any(other["endpoint"] == "N1" and supports_arrival(other, other_target) for other in related)):
            return "trip_candidate"
        return "unmatched" if alight <= board else "vehicle_matched"
    return "vehicle_matched"


def analyze(paths):
    groups = {}
    coverage = {}
    outcomes = Counter()
    seen = set()
    for path in sorted(set(paths)):
        capture = parse_strict_json(path.read_text(encoding="utf-8"))
        endpoint = capture.get("endpoint")
        if capture.get("format") != FORMAT or endpoint not in ("N1", "A1", "A2"):
            raise ValueError("invalid Stage A capture")
        provenance = "synthetic" if capture["synthetic"] else "live"
        identity = (provenance, capture["session_id"], capture["round"], endpoint)
        if identity in seen:
            raise ValueError("duplicate Stage A capture")
        seen.add(identity)
        outcomes[(provenance, endpoint, capture["result"])] += 1
        if capture["result"] not in ("ok", "empty"):
            continue
        coverage.setdefault((provenance, endpoint), Counter(records=0, with_plate=0))
        for record in capture["records"]:
            coverage[provenance, endpoint].update(records=1, with_plate=int(bool(plate(record))))
            groups.setdefault(identity[:2], []).append(
                dict(record, endpoint=endpoint, fetched_at=capture["fetched_at"])
            )
    matches = {}
    for (provenance, _), rows in groups.items():
        counts = matches.setdefault(provenance, Counter(dict.fromkeys(LABELS, 0)))
        counts.update(classify(row, rows) for row in rows if row["endpoint"] == "N1")
    return {
        "coverage": [
            {"provenance": provenance, "endpoint": endpoint, **counts,
             "plate_fraction": counts["with_plate"] / counts["records"] if counts["records"] else None}
            for (provenance, endpoint), counts in sorted(coverage.items())
        ],
        "capture_outcomes": [
            {"provenance": provenance, "endpoint": endpoint, "result": result, "count": count}
            for (provenance, endpoint, result), count in sorted(outcomes.items())
        ],
        "n1_row_matches": matches,
        "n1_unique_pair_fraction": {
            provenance: (counts["vehicle_matched"] + counts["trip_candidate"]) / sum(counts.values())
            for provenance, counts in matches.items() if sum(counts.values())
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=DIRECTORY)
    args = parser.parse_args(argv)
    paths = list(args.directory.glob("*.capture.json"))
    if not paths:
        parser.error("no Stage A captures found")
    try:
        print(dump_stable_json(analyze(paths)))
    except (OSError, ValueError, KeyError, TypeError):
        parser.error("unable to read or validate Stage A captures")


if __name__ == "__main__":
    main()
