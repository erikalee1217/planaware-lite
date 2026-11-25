"""Bounded, local-only artifact collection for the Taipei 212 experiment."""
import argparse
from datetime import datetime, timezone
import fcntl
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from .live import MAX_BYTES, TOKEN_URL, NoRedirect, PipelineError, _bounded_url, save_atomic
from .n1 import N1Scope, dump_stable_json, freeze_n1_fixture, parse_strict_json

STOPS = ("TPE10600", "TPE10606")
SCOPE = N1Scope("TPE10912", 0, STOPS)
DIRECTORY = Path(__file__).resolve().parents[1] / "work" / "tdx-stage-a"
ENABLE = "PLANAWARE_STAGE_A_LIVE"
ROW_LIMIT = 25
LIMITS = {"oauth": 1, "data": 6}
FORMAT = "planaware-stage-a-v1"


class StopCollection(Exception):
    """Fixed diagnostics only."""


def request_specs():
    n1 = _bounded_url(SCOPE, "Taipei", ROW_LIMIT)
    a1_url = n1.partition("?")[0].replace("EstimatedTimeOfArrival", "RealTimeByFrequency")
    query = urlencode({"$filter": "RouteUID eq 'TPE10912' and Direction eq 0",
                       "$top": ROW_LIMIT + 1, "$format": "JSON"})
    return {
        "N1": n1,
        "A1": f"{a1_url}?{query}",
        "A2": n1.replace("EstimatedTimeOfArrival", "RealTimeNearStop"),
    }


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def publish(path, body):
    """Publish bounded bytes without replacing an existing capture."""
    with tempfile.NamedTemporaryFile(dir=path.parent) as temporary:
        os.fchmod(temporary.fileno(), 0o400)
        temporary.write(body)
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, path)


def write_json(path, value):
    publish(path, (dump_stable_json(value) + "\n").encode())


def spend(ledger, kind):
    ledger.seek(0)
    attempts = ledger.readlines()
    if any(line not in ("oauth\n", "data\n") for line in attempts):
        raise StopCollection("invalid ledger")
    if any(attempts.count(key + "\n") > cap for key, cap in LIMITS.items()):
        raise StopCollection("invalid ledger")
    if attempts.count(kind + "\n") >= LIMITS[kind]:
        raise StopCollection(f"{kind} budget exhausted")
    ledger.write(kind + "\n")
    ledger.flush()
    os.fsync(ledger.fileno())


def http_client():
    opener = build_opener(ProxyHandler({}), NoRedirect())
    allowed_urls = {TOKEN_URL, *request_specs().values()}

    def request(url, *, headers, body, limit):
        if url not in allowed_urls:
            raise StopCollection("unexpected Stage A URL")
        try:
            with opener.open(Request(url, data=body, headers=headers), timeout=15) as response:
                return response.status, response.headers, response.read(limit + 1)
        except HTTPError as error:
            with error:
                return error.code, {}, b""

    return request


def attempt(http, ledger, kind, url, *, headers, body, limit, metadata):
    spend(ledger, kind)
    metadata["requested_at"] = utc_now()
    try:
        status, response_headers, response = http(url, headers=headers, body=body, limit=limit)
        metadata["http_status"] = status if type(status) is int else None
        if status != 200:
            raise StopCollection(f"HTTP {status}" if type(status) is int else "invalid HTTP result")
        if len(response) > limit:
            raise StopCollection("response exceeds byte limit")
        headers_lower = {key.lower(): value for key, value in response_headers.items()}
        length = headers_lower.get("content-length")
        if (headers_lower.get("content-encoding", "identity").lower() != "identity"
            or (length is not None and length != str(len(response)))
            or re.search(r'rel\s*=\s*["\']?next\b', headers_lower.get("link", ""), re.I)
            or any(key in headers_lower for key in ("content-range", "x-next-page", "x-continuation-token"))):
            raise StopCollection("visible truncation or unsupported encoding")
        return response
    except (TimeoutError, URLError, OSError, HTTPException):
        raise StopCollection("timeout or transport failure") from None
    except StopCollection:
        raise
    except Exception:
        raise StopCollection("invalid HTTP response or transport failure") from None
    finally:
        metadata["fetched_at"] = utc_now()


def contains_credentials(value, secrets):
    if isinstance(value, dict):
        return any(
            re.sub(r"[^a-z0-9]", "", key.lower()) in
            {"authorization", "proxyauthorization", "accesstoken", "refreshtoken", "clientid", "clientsecret", "token"}
            or contains_credentials(key, secrets) or contains_credentials(item, secrets)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_credentials(item, secrets) for item in value)
    return isinstance(value, str) and any(secret in value for secret in secrets)


FIELDS = {
    "route_uid": "RouteUID", "subroute_uid": "SubRouteUID", "direction": "Direction",
    "stop_uid": "StopUID", "stop_sequence": "StopSequence", "plate": "PlateNumb",
    "estimate_seconds": "EstimateTime", "stop_status": "StopStatus", "event_type": "A2EventType",
    "position": "BusPosition", "gps_time": "GPSTime", "source_updated_at": "SrcUpdateTime",
    "source_received_at": "SrcRecTime", "transmitted_at": "TransTime",
    "tdx_updated_at": "UpdateTime",
    "trip_start_time": "TripStartTime", "trip_start_type": "TripStartTimeType",
}


def normalize(endpoint, rows):
    if isinstance(rows, dict) and any(
        "next" in key.lower() or "truncat" in key.lower() or "continuation" in key.lower()
        for key in rows
    ):
        raise StopCollection("visible truncation")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise StopCollection("expected a JSON array of records")
    if len(rows) > ROW_LIMIT:
        raise StopCollection("record cap exceeded")
    for row in rows:
        stop = row.get("StopUID")
        if (row.get("RouteUID") != SCOPE.route_uid or type(row.get("Direction")) is not int
            or row["Direction"] != SCOPE.direction
            or row.get("SubRouteUID") not in (None, "", SCOPE.route_uid)
            or (endpoint != "A1" and stop not in STOPS)):
            raise StopCollection("out-of-scope record")
    return [{field: row.get(source) for field, source in FIELDS.items()} for row in rows]


def collect_round(directory, ledger, http, token, secrets, session_id, round_number, synthetic):
    for endpoint, url in request_specs().items():
        stem = directory / f"{session_id}-{round_number}-{endpoint}"
        metadata = {"format": FORMAT, "session_id": session_id, "round": round_number,
                    "endpoint": endpoint, "url": url, "synthetic": synthetic, "records": []}
        try:
            response = attempt(http, ledger, "data", url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Encoding": "identity",
            }, body=None, limit=MAX_BYTES, metadata=metadata)
            raw = response.decode("utf-8")
            rows = parse_strict_json(raw)
            if any(secret.encode() in response for secret in secrets) or contains_credentials(rows, secrets):
                raise StopCollection("credential-like content in response")
            records = normalize(endpoint, rows)
            snapshot = None
            if endpoint == "N1":
                snapshot = freeze_n1_fixture(raw, scope=SCOPE, requested_at=metadata["requested_at"],
                                             fetched_at=metadata["fetched_at"], run_key=f"stage-a:{stem.name}")
            raw_path = stem.with_suffix(".raw.json")
            publish(raw_path, response)
            metadata["raw_file"] = raw_path.name
            if snapshot is not None:
                snapshot_path = stem.with_suffix(".n1.json")
                save_atomic(snapshot, snapshot_path)
                metadata["snapshot_file"] = snapshot_path.name
            metadata["records"] = records
            metadata["result"] = "ok" if rows else "empty"
        except (StopCollection, ValueError, RecursionError, PipelineError) as error:
            metadata["result"] = str(error) if isinstance(error, StopCollection) else "invalid response or snapshot"
            raise StopCollection(metadata["result"]) from None
        except OSError:
            metadata["result"] = "artifact write failed"
            raise StopCollection(metadata["result"]) from None
        finally:
            write_json(stem.with_suffix(".capture.json"), metadata)


def collect(directory, http, credentials, *, second_round_after=None, pause=time.sleep, synthetic=True):
    """One locked session. Failed or interrupted attempts still spend budget."""
    if second_round_after is not None and not 1 <= second_round_after <= 600:
        raise StopCollection("second round delay must be 1–600 seconds")
    if directory.is_symlink() or directory.parent.is_symlink():
        raise StopCollection("unsafe artifact directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger_path = directory / "requests.ledger"
    fd = os.open(ledger_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8", newline="") as ledger:
        if not stat.S_ISREG(os.fstat(ledger.fileno()).st_mode):
            raise StopCollection("invalid ledger file")
        try:
            fcntl.flock(ledger, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopCollection("another Stage A session is active") from None

        session_id = uuid4().hex
        audit = {"format": FORMAT, "session_id": session_id, "synthetic": synthetic, "oauth": {}}
        try:
            client_id, client_secret = credentials()
            if not client_id or not client_secret:
                raise StopCollection("missing TDX credentials")
            response = attempt(http, ledger, "oauth", TOKEN_URL, headers={
                "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
            }, body=urlencode({"grant_type": "client_credentials", "client_id": client_id,
                               "client_secret": client_secret}).encode(), limit=16384, metadata=audit["oauth"])
            oauth = parse_strict_json(response.decode("utf-8"))
            token = oauth.get("access_token") if isinstance(oauth, dict) else None
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
                raise StopCollection("invalid bearer token response")
            token_type = oauth.get("token_type")
            if not isinstance(token_type, str) or token_type.lower() != "bearer":
                raise StopCollection("invalid bearer token response")
            secrets = tuple({client_id, client_secret, token})
            collect_round(directory, ledger, http, token, secrets, session_id, 1, synthetic)
            if second_round_after is not None:
                expiry = oauth.get("expires_in")
                if type(expiry) is not int or expiry <= second_round_after + 60:
                    raise StopCollection("insufficient token lifetime for second round")
                pause(second_round_after)
                collect_round(directory, ledger, http, token, secrets, session_id, 2, synthetic)
            audit["stop_reason"] = "planned requests completed"
        except (StopCollection, ValueError, RecursionError, KeyboardInterrupt) as error:
            audit["stop_reason"] = str(error) if isinstance(error, StopCollection) else "cancelled or invalid response"
            raise StopCollection(audit["stop_reason"]) from None
        finally:
            write_json(directory / f"{session_id}.session.json", audit)
    return audit


def main(argv=None, *, http=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute-live", action="store_true")
    parser.add_argument("--second-round-after", type=int, choices=range(1, 601), metavar="SECONDS",
                        help="explicitly schedule one second round 1–600 seconds later")
    args = parser.parse_args(argv)
    if not args.execute_live:
        print(json.dumps({
            "mode": "dry-run", "requests": request_specs(),
            "planned_data_requests": 6 if args.second_round_after else 3,
            "second_round_after_seconds": args.second_round_after,
            "lifetime_caps": LIMITS,
            "stop_on": ["non-200 (including 429/5xx)", "timeout/transport failure", "invalid response",
                        "out-of-scope", "oversize/truncation", "budget exhausted", "operator interruption", "planned completion"],
            "nothing_sent": True,
        }, indent=2))
        return 0
    if os.environ.get(ENABLE) != "1":
        print(f"Live execution requires --execute-live and {ENABLE}=1.")
        return 2
    print(f"Stage A ledger: {DIRECTORY / 'requests.ledger'}", flush=True)
    try:
        audit = collect(DIRECTORY, http if http is not None else http_client(), lambda: (
            os.environ.get("TDX_CLIENT_ID"), os.environ.get("TDX_CLIENT_SECRET"),
        ), second_round_after=args.second_round_after, synthetic=http is not None)
    except StopCollection as error:
        print(str(error))
        return 2
    except Exception:
        # Unexpected library errors may contain request or response details.
        print("Stage A stopped: local or transport error")
        return 2
    print(audit["stop_reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
