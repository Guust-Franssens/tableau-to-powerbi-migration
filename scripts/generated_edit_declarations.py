"""
purpose: append and read generated-artifact edit declarations, and the replay-script registrations
         that make those replay scripts discoverable (issue #259), without shared-writer loss.
usage:   imported by declaration writers, check_migration_progress.py, and check_replay_manifest.py
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENERATED_EDIT_DECLARATIONS = Path("_build") / "generated-edit-declarations.json"
GENERATED_EDIT_DECLARATIONS_DIR = Path("_build") / "generated-edit-declarations"

# One registration per replay script, EVERY time it is run through `declare_generated_edit.py` -
# including the idempotent `DECLARE: NO_CHANGE` case a drift declaration never records. That gap is
# exactly what issue #259 measured: a script that made no change on this run is still a script that
# exists and must stay discoverable, so registration is unconditional where a drift declaration is not.
REPLAY_MANIFEST_DIR = Path("_build") / "replay-manifest"

_SAFE_STEM = re.compile(r"[^A-Za-z0-9_.-]+")


def _legacy_declarations(path: Path) -> list[dict[str, Any]]:
    """Declarations from the original single-file ledger, for existing bundles."""
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return []
    declarations = payload.get("declarations")
    return [entry for entry in declarations if isinstance(entry, dict)] if isinstance(declarations, list) else []


def _record_filename(record: dict[str, Any], stem_key: str) -> str:
    """Unique, stable-enough name for one append-only record writer."""
    run_id = _SAFE_STEM.sub("_", str(record.get("run_id") or "run"))[:60]
    stem = _SAFE_STEM.sub("_", str(record.get(stem_key) or stem_key))[-80:]
    recorded = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{recorded}-{os.getpid()}-{run_id}-{stem}-{uuid.uuid4().hex}.json"


def _append_record(directory: Path, record: dict[str, Any], stem_key: str) -> Path:
    """Persist one record without reading or rewriting sibling records in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload.setdefault("version", 1)
    payload.setdefault("recorded_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    final_path = directory / _record_filename(payload, stem_key)
    staging_path = final_path.with_name(final_path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        staging_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(staging_path, final_path)
    finally:
        if staging_path.exists():
            staging_path.unlink()
    return final_path


def append_generated_edit_declaration(bundle: Path, declaration: dict[str, Any]) -> Path:
    """Persist one declaration without reading or rewriting sibling declarations."""
    return _append_record(bundle / GENERATED_EDIT_DECLARATIONS_DIR, declaration, "target")


def _directory_records(path: Path) -> list[dict[str, Any]]:
    """Version-1 JSON records from one append-only directory, corrupt files skipped."""
    if not path.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for record_path in sorted(path.glob("*.json")):
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("version") == 1:
            records.append(payload)
    return records


def append_replay_registration(bundle: Path, registration: dict[str, Any]) -> Path:
    """Persist one replay-script registration (issue #259), one file per registering run.

    A registration answers "does this replay script exist and is it discoverable", which is a
    different question from a drift declaration's "did this run's edit match its declared hash" -
    so it is written unconditionally, even on the ``DECLARE: NO_CHANGE`` path a declaration skips.
    """
    return _append_record(bundle / REPLAY_MANIFEST_DIR, registration, "script_identity")


def load_replay_registrations(bundle: Path) -> list[dict[str, Any]]:
    """Every replay-script registration recorded for this bundle, corrupt records skipped."""
    return _directory_records(bundle / REPLAY_MANIFEST_DIR)


def load_generated_edit_declarations(bundle: Path) -> list[dict[str, Any]]:
    """Load both legacy single-ledger and append-only declaration records."""
    return [
        *_legacy_declarations(bundle / GENERATED_EDIT_DECLARATIONS),
        *_directory_records(bundle / GENERATED_EDIT_DECLARATIONS_DIR),
    ]
