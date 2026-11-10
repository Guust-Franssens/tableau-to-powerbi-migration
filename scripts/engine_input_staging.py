"""
purpose: stage ID-named workbook hard links for run_estate.py's interim compact-path layout (#836)
usage:   imported by scripts/run_estate.py
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from typing import Any

import generated_edit_declarations as declarations
from migration_bundle import sha256_file
import unit_ids

RESTORE_SCRIPT_IDENTITY = "scripts/engine_input_staging.py#restore_display_names"


class EngineInputStagingError(RuntimeError):
    """The host cannot establish or safely reconcile the engine's staged input."""


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _safe_regular_file(path: Path, root: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and path.resolve(strict=True).is_relative_to(root.resolve(strict=True))


def _stage_name(record: dict[str, Any], unit_id: str) -> str:
    luid = record.get("luid")
    prefix = f"{luid}_" if isinstance(luid, str) and luid else ""
    return f"{prefix}{unit_id}{Path(record['asset_path']).suffix}"


def _allocation_requests(run_dir: Path, records: list[dict[str, Any]], sweep_path: Path) -> list[unit_ids.UnitRequest]:
    """Build deterministic workbook requests from the engine's selected asset records."""
    sweep_by_path: dict[str, dict[str, Any]] = {}
    try:
        sweep = json.loads(sweep_path.read_text(encoding="utf-8")) if sweep_path.is_file() else []
    except (OSError, ValueError):
        sweep = []
    for row in sweep if isinstance(sweep, list) else []:
        engine_input = row.get("engine_input") if isinstance(row, dict) else None
        path = engine_input.get("path") if isinstance(engine_input, dict) else None
        if isinstance(path, str):
            sweep_by_path[os.path.normcase(os.path.abspath(path))] = row

    requests = []
    for record in records:
        if record["kind"] != "workbook":
            continue
        asset = Path(record["asset_path"]).resolve(strict=True)
        sweep_row = sweep_by_path.get(os.path.normcase(str(asset)), {})
        requests.append(
            unit_ids.UnitRequest(
                key=asset.relative_to(run_dir).as_posix(),
                kind="workbook",
                name=record["name"],
                luid=record.get("luid"),
                site_luid=sweep_row.get("site_luid"),
                project_luid=sweep_row.get("project_luid"),
                project_name=sweep_row.get("project_name"),
                legacy_folder=record["legacy_folder"],
            )
        )
    return requests


def _expected_files(
    input_dir: Path, stage_root: Path, records: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> tuple[dict[Path, Path], list[dict[str, Any]]]:
    """Map each mirrored path to its source, and describe what the engine must select from the mirror."""
    input_base = input_dir if input_dir.is_dir() else input_dir.parent
    ids = {entry["key"]: entry["id"] for entry in entries}
    selected = {Path(record["asset_path"]).resolve(strict=True): record for record in records}
    expected: dict[Path, Path] = {}
    staged_records: list[dict[str, Any]] = []
    for source in sorted(input_dir.rglob("*") if input_dir.is_dir() else [input_dir]):
        if source.is_dir():
            continue
        if not _safe_regular_file(source, input_base):
            raise EngineInputStagingError(f"input contains a non-regular or redirected file: {source}")
        record = selected.get(source)
        if source.suffix.lower() in {".twb", ".twbx"} and record is None:
            continue
        relative = source.relative_to(input_base)
        if record is not None:
            unit_id = ids[source.relative_to(stage_root.parent).as_posix()] if record["kind"] == "workbook" else None
            if unit_id:
                relative = relative.with_name(_stage_name(record, unit_id))
            staged_records.append(
                {
                    "kind": record["kind"],
                    "asset_path": str(stage_root / relative),
                    "legacy_folder": unit_id or record["legacy_folder"],
                    "id": unit_id,
                }
            )
        expected[relative] = source
    return expected, staged_records


def _link_or_replace(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if target.exists() and os.path.samefile(source, target):
            return
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        os.link(source, temporary)
        os.replace(temporary, target)
    except OSError as exc:
        raise EngineInputStagingError(f"cannot hard-link source {source} to destination {target}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _probe_hard_link(stage_root: Path, probe_source: Path) -> None:
    """Refuse before any ID is reserved when this volume cannot hard-link into the run."""
    stage_root.mkdir(parents=True, exist_ok=True)
    probe_target = stage_root / f".link-probe-{secrets.token_hex(4)}"
    try:
        os.link(probe_source, probe_target)
    except OSError as exc:
        raise EngineInputStagingError(
            f"cannot hard-link source {probe_source} to destination {probe_target}: {exc}"
        ) from exc
    finally:
        probe_target.unlink(missing_ok=True)


def _reconcile_stage(stage_root: Path, expected: dict[Path, Path]) -> None:
    """Make the run-owned mirror hold exactly the expected links, then prove each one."""
    for relative, source in expected.items():
        _link_or_replace(source, stage_root / relative)
    for existing in sorted(stage_root.rglob("*"), reverse=True):
        if not existing.is_dir() and existing.relative_to(stage_root) not in expected:
            existing.unlink()
    for existing in sorted((path for path in stage_root.rglob("*") if path.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            existing.rmdir()
    for relative, source in expected.items():
        target = stage_root / relative
        if not target.is_file() or not os.path.samefile(source, target):
            raise EngineInputStagingError(f"staged input does not match source {source} at {target}")


def stage_unit_ids(
    input_dir: Path, run_dir: Path, records: list[dict[str, Any]]
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """Probe hard-link support, allocate workbook IDs, and reconcile a run-owned mirror.

    Returns ``(engine-input root, index entries, staged selection, sha256 per mirrored source)``.
    """
    input_dir = input_dir.resolve(strict=True)
    run_dir = run_dir.resolve(strict=True)
    stage_root = run_dir / "engine-input"
    _probe_hard_link(stage_root, Path(records[0]["asset_path"]).resolve(strict=True))
    requests = _allocation_requests(run_dir, records, input_dir.parent / "parse-sweep.json")
    entries = unit_ids.allocate_unit_ids(run_dir, requests)
    expected, staged_records = _expected_files(input_dir, stage_root, records, entries)
    source_hashes = {str(source): sha256_file(source) for source in expected.values()}
    _reconcile_stage(stage_root, expected)
    return stage_root, entries, staged_records, source_hashes


def _selection_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return record["kind"], os.path.normcase(str(Path(record["asset_path"]).resolve())), record["legacy_folder"]


def verify_engine_selection(records: list[dict[str, Any]], staged: list[dict[str, Any]]) -> bool:
    """Confirm the engine's own enumeration of ``engine-input/`` selects the staged files and folders.

    A workbook must land in a folder named exactly by its ID, so a datasource whose name collides
    with an ID (the engine would dedupe the workbook to ``u0001_2``) is refused here, not after the run.
    """
    return sorted(map(_selection_key, records)) == sorted(map(_selection_key, staged))


def _engine_model_names(engine: Path, names: set[str]) -> dict[str, str]:
    """The engine's own ``_fs_safe`` model base for each original workbook name."""
    adapter = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from migrate_estate import _fs_safe\n"
        "print(json.dumps({name: _fs_safe(name, 'Model') for name in json.loads(sys.stdin.read())}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", adapter, str(engine / "skills" / "tableau-migration" / "scripts")],
        input=json.dumps(sorted(names)),
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        model_names = json.loads(result.stdout) if result.returncode == 0 else None
    except json.JSONDecodeError:
        model_names = None
    if not isinstance(model_names, dict) or set(model_names) != names:
        raise EngineInputStagingError("cannot establish the engine's model display names")
    return model_names


def bound_model(bundle: Path, unit_id: str) -> Path | None:
    """The model folder the unit's ``pbip/`` report binds to through ``byPath``, when readable."""
    report_folder = bundle / "pbip" / unit_id / f"{unit_id}.Report"
    try:
        pbir = json.loads((report_folder / "definition.pbir").read_text(encoding="utf-8"))
        return (report_folder / Path(pbir["datasetReference"]["byPath"]["path"])).resolve()
    except (OSError, KeyError, TypeError, ValueError):
        return None


def consolidated_model_target(bundle: Path, unit_id: str, row: object) -> str | None:
    """The model ``.platform`` a restore may touch: a consolidated row bound to ``<id>.SemanticModel`` (O3)."""
    model_folder = f"{unit_id}.SemanticModel"
    if not isinstance(row, dict) or not row.get("consolidated_datasources"):
        return None
    if bound_model(bundle, unit_id) != (bundle / "pbip" / unit_id / model_folder).resolve():
        return None
    return f"pbip/{unit_id}/{model_folder}/.platform"


def generated_run_id(bundle: Path) -> str | None:
    """The generated-artifact ``run_id`` that binds every restore declaration to one engine run."""
    try:
        manifest = json.loads((bundle / "input_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generated = manifest.get("generated_artifacts") if isinstance(manifest, dict) else None
    run_id = generated.get("run_id") if isinstance(generated, dict) else None
    return run_id if isinstance(run_id, str) and run_id else None


def _restore_targets(
    bundle: Path, report: dict[str, Any], entries: list[dict[str, Any]], engine: Path
) -> list[tuple[str, str, str]]:
    """Plan ``(unit ID, relative .platform, display name)``; a model only with consolidation evidence."""
    rows = {row.get("name"): row for row in report.get("workbooks", []) if isinstance(row, dict)}
    models = {entry["id"]: consolidated_model_target(bundle, entry["id"], rows.get(entry["id"])) for entry in entries}
    consolidated = {entry["source"]["name"] for entry in entries if models[entry["id"]]}
    model_names = _engine_model_names(engine, consolidated) if consolidated else {}
    targets = []
    for entry in entries:
        unit_id, name = entry["id"], entry["source"]["name"]
        targets.append((unit_id, f"pbip/{unit_id}/{unit_id}.Report/.platform", name))
        if models[unit_id]:
            targets.append((unit_id, models[unit_id], model_names[name]))
    return targets


def _restore_one(
    bundle: Path, target: tuple[str, str, str], run_id: str, prior_declarations: list[dict[str, Any]]
) -> None:
    """Restore one ``.platform`` displayName from its ID, or accept this run's declared restore."""
    unit_id, relative, display_name = target
    path = bundle / Path(relative)
    try:
        original = path.read_bytes()
        platform = json.loads(original)
        current_name = platform["metadata"]["displayName"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EngineInputStagingError(f"cannot restore display name in {relative}") from exc
    current_hash = hashlib.sha256(original).hexdigest()
    if current_name == display_name:
        if display_name == unit_id or any(
            prior.get("target") == relative
            and prior.get("script_identity") == RESTORE_SCRIPT_IDENTITY
            and prior.get("run_id") == run_id
            and prior.get("expected_sha256") == current_hash
            for prior in prior_declarations
        ):
            return
        raise EngineInputStagingError(f"restored displayName in {relative} has no matching generated-edit declaration")
    if current_name != unit_id:
        raise EngineInputStagingError(f"unexpected displayName in {relative}; refusing to overwrite it")
    platform["metadata"]["displayName"] = display_name
    _atomic_json(path, platform)
    declarations.append_generated_edit_declaration(
        bundle,
        {
            "version": 1,
            "run_id": run_id,
            "kind": "changed",
            "target": relative,
            "baseline_sha256": current_hash,
            "expected_sha256": sha256_file(path),
            "script_identity": RESTORE_SCRIPT_IDENTITY,
            "script_sha256": sha256_file(Path(__file__)),
            "why": "#836 interim: restore a display name hidden by ID-named engine input",
        },
    )
    declarations.write_replay_registration(bundle, {"target": relative, "script": RESTORE_SCRIPT_IDENTITY})


def restore_display_names(bundle: Path, report: dict[str, Any], entries: list[dict[str, Any]], engine: Path) -> None:
    """Restore only ID-hidden display names in ``pbip/``, declaring every generated edit."""
    run_id = generated_run_id(bundle)
    if run_id is None:
        raise EngineInputStagingError("cannot restore display names without the generated-artifact run_id")
    prior_declarations = declarations.load_generated_edit_declarations(bundle)
    for target in _restore_targets(bundle, report, entries, engine):
        _restore_one(bundle, target, run_id, prior_declarations)
