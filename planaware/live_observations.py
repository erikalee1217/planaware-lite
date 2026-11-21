"""Explicit pilot acquisition and recovery through the V0.1 N1 pipeline."""

from pathlib import Path
import os
import re
from uuid import uuid4

from .catalog import load_catalog
from .journeys import JourneyStore, _validate_snapshot_scope
from .live import PipelineError, fetch_once
from .n1 import FrozenSnapshot, N1Scope, _read_json


LIVE_RECORD_LIMIT = 5
_ARTIFACT_NAME = re.compile(r"[0-9a-f]{32}\.json")


def live_credentials_available() -> bool:
    return all(os.environ.get(key, "").strip() for key in ("TDX_CLIENT_ID", "TDX_CLIENT_SECRET"))


def _check_directory(directory: Path):
    if directory.parent.is_symlink() or directory.is_symlink():
        raise PipelineError("Live snapshot storage is unavailable")


def list_live_artifacts(directory: Path) -> list[str]:
    _check_directory(directory)
    try:
        return sorted(path.name for path in directory.iterdir()
                      if _ARTIFACT_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file())
    except FileNotFoundError:
        return []
    except OSError:
        raise PipelineError("Live snapshot storage is unavailable") from None


def _read_live_artifact(path: Path) -> FrozenSnapshot:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError
        snapshot = FrozenSnapshot.from_json(path.read_text(encoding="utf-8"))
        _validate_snapshot_scope(snapshot)
        if not snapshot.run_key.startswith("tdx-n1:") or any(
            row["raw_payload"].get("synthetic_test_only") is True for row in _read_json(snapshot.records_json)
        ):
            raise ValueError
        return snapshot
    except (OSError, ValueError):
        raise PipelineError("The saved live artifact could not be read or validated") from None


def acquire_live_boarding(store: JourneyStore, directory: Path, *, confirmed: bool) -> tuple[int, str]:
    if not live_credentials_available():
        raise PipelineError("TDX credentials are unavailable")
    if not confirmed:
        raise PipelineError("Confirm TDX access and available quota before fetching")
    _check_directory(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise PipelineError("Live snapshot storage is unavailable") from None
    path = directory / f"{uuid4().hex}.json"
    route = load_catalog().route
    scope = N1Scope(route.route_uid, route.direction, (route.stops[0].stop_uid,))
    try:
        report = fetch_once(
            scope=scope, city="Taipei", max_records=LIVE_RECORD_LIMIT, output=path,
            execute_sql=store.execute_snapshot_sql, confirm_free_quota=confirmed,
        )
        snapshot = _read_live_artifact(path)
    except (OSError, PipelineError):
        if path.is_file():
            raise PipelineError("A frozen snapshot was saved. Use Replay saved snapshot to recover without fetching again.") from None
        raise PipelineError("Live acquisition failed. No new snapshot was saved and the request was not retried.") from None
    return report["database_run_id"], snapshot.fetched_at


def replay_live_boarding(store: JourneyStore, directory: Path, name: str) -> tuple[int, str]:
    _check_directory(directory)
    if not _ARTIFACT_NAME.fullmatch(name):
        raise PipelineError("Choose a saved live artifact")
    snapshot = _read_live_artifact(directory / name)
    return store.import_snapshot(snapshot), snapshot.fetched_at
