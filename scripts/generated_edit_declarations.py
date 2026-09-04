"""
purpose: append and read generated-artifact edit declarations, and write the NAVIGATIONAL
         replay-script manifest entry beside each declared edit (issue #259), without
         shared-writer loss.
usage:   imported by declaration writers and check_migration_progress.py

The replay-manifest entry this module writes (`write_replay_registration`/`load_replay_registrations`)
is an INDEX FOR DISCOVERABILITY ONLY. It is not proof that the named script still exists on disk,
that it matches any digest, that it belongs to a particular package or run, or that it covers every
generated edit in the bundle - it only records what `declare_generated_edit.py` was told at the time
it ran. Nothing in this repository treats it as an authoritative gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENERATED_EDIT_DECLARATIONS = Path("_build") / "generated-edit-declarations.json"
GENERATED_EDIT_DECLARATIONS_DIR = Path("_build") / "generated-edit-declarations"

# The navigational replay-script index (issue #259): one row per declared target, so a replay
# script stays findable even after an idempotent `DECLARE: NO_CHANGE` re-run that writes no drift
# declaration. Keyed by a collision-safe digest of TARGET rather than a unique-per-run name, so a
# repeated declaration of the same edit overwrites its own row instead of accumulating contradictory
# duplicates, while two distinct targets that sanitize to the same readable stem never collide.
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


def _sanitized_key(value: str) -> str:
    """A filesystem-safe, COLLISION-SAFE, stable key for one navigational manifest row.

    A readable sanitized stem alone is not enough: distinct targets ``A/B.tmdl`` and ``A_B.tmdl``
    both sanitize to ``A_B.tmdl`` and would silently overwrite one another. A stable short digest of
    the exact, UNSANITIZED ``value`` is appended before the stem is truncated, so two different
    target identities always land on two different filenames while the same target identity always
    lands back on the same one (idempotent re-declaration still overwrites its own row).
    """
    stem = _SAFE_STEM.sub("_", value).strip("_")[-160:] or "record"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{stem}-{digest}"


def write_replay_registration(bundle: Path, registration: dict[str, Any]) -> Path:
    """Idempotently write one navigational replay-manifest row, keyed by its declared ``target``.

    Unlike the append-only declaration directory, this index intentionally OVERWRITES: re-declaring
    the same target (e.g. an idempotent `DECLARE: NO_CHANGE` re-run) must update the existing row
    rather than accumulate a second, contradictory one for the same declared edit.
    """
    directory = bundle / REPLAY_MANIFEST_DIR
    directory.mkdir(parents=True, exist_ok=True)
    payload = dict(registration)
    payload.setdefault("version", 1)
    payload.setdefault("recorded_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    target_key = str(payload.get("target") or "record")
    final_path = directory / f"{_sanitized_key(target_key)}.json"
    staging_path = final_path.with_name(final_path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        staging_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(staging_path, final_path)
    finally:
        if staging_path.exists():
            staging_path.unlink()
    return final_path


def load_replay_registrations(bundle: Path) -> list[dict[str, Any]]:
    """Every navigational replay-manifest row recorded for this bundle, corrupt records skipped."""
    return _directory_records(bundle / REPLAY_MANIFEST_DIR)


def load_generated_edit_declarations(bundle: Path) -> list[dict[str, Any]]:
    """Load both legacy single-ledger and append-only declaration records."""
    return [
        *_legacy_declarations(bundle / GENERATED_EDIT_DECLARATIONS),
        *_directory_records(bundle / GENERATED_EDIT_DECLARATIONS_DIR),
    ]
