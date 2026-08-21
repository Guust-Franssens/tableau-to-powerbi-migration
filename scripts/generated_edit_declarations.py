"""
purpose: append and read generated-artifact edit declarations without shared-writer loss.
usage:   imported by declaration writers and check_migration_progress.py
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


def _directory_declarations(path: Path) -> list[dict[str, Any]]:
    """Declarations from the append-only per-record directory."""
    if not path.is_dir():
        return []
    declarations: list[dict[str, Any]] = []
    for declaration_path in sorted(path.glob("*.json")):
        try:
            payload = json.loads(declaration_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("version") == 1:
            declarations.append(payload)
    return declarations


def load_generated_edit_declarations(bundle: Path) -> list[dict[str, Any]]:
    """Load both legacy single-ledger and append-only declaration records."""
    return [
        *_legacy_declarations(bundle / GENERATED_EDIT_DECLARATIONS),
        *_directory_declarations(bundle / GENERATED_EDIT_DECLARATIONS_DIR),
    ]


def _declaration_filename(declaration: dict[str, Any]) -> str:
    """Unique, stable-enough name for one declaration writer."""
    run_id = _SAFE_STEM.sub("_", str(declaration.get("run_id") or "run"))[:60]
    target = _SAFE_STEM.sub("_", str(declaration.get("target") or "target"))[-80:]
    recorded = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{recorded}-{os.getpid()}-{run_id}-{target}-{uuid.uuid4().hex}.json"


def append_generated_edit_declaration(bundle: Path, declaration: dict[str, Any]) -> Path:
    """Persist one declaration without reading or rewriting sibling declarations."""
    declaration_dir = bundle / GENERATED_EDIT_DECLARATIONS_DIR
    declaration_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(declaration)
    payload.setdefault("version", 1)
    payload.setdefault("recorded_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    final_path = declaration_dir / _declaration_filename(payload)
    staging_path = final_path.with_name(final_path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        staging_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(staging_path, final_path)
    finally:
        if staging_path.exists():
            staging_path.unlink()
    return final_path
