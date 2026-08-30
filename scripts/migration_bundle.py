"""
purpose: expose the small migration-bundle contract shared by parser specs and engine bundles.
usage:   import migration_bundle; migration_bundle.load_bundle(Path("migration-spec.json"))
         import migration_bundle; migration_bundle.load_bundle(Path("engine-output-dir"))

The contract is intentionally smaller than `migration-spec.json`: migration directory, declared data
sources, and published-datasource binding keys. Engine output is not expanded into a fake spec; if a
field is absent from `report.json`/`handover/*.json`, callers see the absence and can fail closed.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from connection_target import FLAT_FILE, LIVE_SOURCE, powerbi_target
from engine_source import engine_provenance

ENGINE_RECEIPT = "engine-output-receipt.json"
ENGINE_OUTPUT_DIRS = frozenset({"pbip", "reports", "semantic_models", "data"})
ARTIFACT_SUFFIXES = frozenset(
    {".tmdl", ".pbism", ".pbir", ".pbip", ".csv", ".tsv", ".parquet", ".hyper", ".xlsx", ".xls", ".dat"}
)


@dataclass(frozen=True)
class MigrationBundle:
    """The non-fabricated fields gate tools need from either migration tier."""

    path: Path
    migration_dir: Path
    kind: str
    data_sources: list[dict[str, Any]]
    published_datasource_keys: list[str]
    unknown_published_datasources: list[str]

    @property
    def label(self) -> str:
        """Human-readable bundle location for logs."""
        return str(self.path)


def load_bundle(path: Path) -> MigrationBundle:
    """Load either a parser `migration-spec.json`, a migration directory, or an engine bundle."""
    resolved = path.resolve()
    if resolved.is_file():
        return _from_spec(resolved)
    if not resolved.is_dir():
        raise FileNotFoundError(f"no migration spec or bundle at {path}")
    spec = resolved / "migration-spec.json"
    if spec.is_file():
        return _from_spec(spec)
    return _from_engine(resolved)


def _from_spec(spec_path: Path) -> MigrationBundle:
    """Read the agent-first parser contract without changing its shape."""
    spec = _read_json(spec_path)
    sources = _dedupe_dicts([s for s in spec.get("data_sources", []) if isinstance(s, dict)])
    return MigrationBundle(
        path=spec_path,
        migration_dir=spec_path.parent,
        kind="migration-spec",
        data_sources=sources,
        published_datasource_keys=_published_keys_from_sources(sources),
        unknown_published_datasources=[],
    )


def _from_engine(bundle_dir: Path) -> MigrationBundle:
    """Read the engine bundle's native files; never synthesize a parser spec."""
    report_path = bundle_dir / "report.json"
    if not report_path.is_file():
        raise FileNotFoundError(
            f"{bundle_dir} is not a migration-spec directory and has no report.json engine bundle marker"
        )
    documents = [_read_json(report_path)]
    handover_dir = bundle_dir / "handover"
    if handover_dir.is_dir():
        documents.extend(_read_json(path) for path in sorted(handover_dir.glob("*.json")))

    sources = _dedupe_dicts(_find_data_sources(documents))
    keys, unknown = _find_binding_signals(documents)
    return MigrationBundle(
        path=bundle_dir,
        migration_dir=bundle_dir,
        kind="engine-bundle",
        data_sources=sources,
        published_datasource_keys=_dedupe_strings([*_published_keys_from_sources(sources), *keys]),
        unknown_published_datasources=unknown,
    )


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _find_data_sources(value: Any) -> list[dict[str, Any]]:
    """Recursively collect explicit datasource objects from engine documents."""
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_find_data_sources(item))
        return found
    if not isinstance(value, dict):
        return found

    for key, item in value.items():
        if key in {"data_sources", "datasources"} and isinstance(item, list):
            found.extend(source for source in item if _looks_like_data_source(source))
        elif key == "embedded_datasources" and isinstance(item, list):
            found.extend(_engine_embedded_source(source) for source in item if isinstance(source, dict))
        else:
            found.extend(_find_data_sources(item))
    if _looks_like_data_source(value):
        found.append(value)
    return found


def _looks_like_data_source(value: Any) -> bool:
    """True only for source-shaped dicts, so arbitrary nested JSON is not promoted."""
    if not isinstance(value, dict):
        return False
    connection = value.get("connection")
    if not isinstance(connection, dict):
        return False
    return any(key in value for key in ("name", "tables", "fields", "published_datasource"))


def _engine_embedded_source(source: dict[str, Any]) -> dict[str, Any]:
    """Convert real engine `embedded_datasources` telemetry into the shared source contract."""
    name = source.get("caption") or source.get("label") or "embedded datasource"
    connections = source.get("connections") if isinstance(source.get("connections"), list) else []
    if connections:
        legs = [_engine_connection(leg) for leg in connections if isinstance(leg, dict)]
        targets = {leg.get("powerbi_target") for leg in legs}
        connection = {
            "class": "federated",
            "connections": legs,
            "powerbi_target": LIVE_SOURCE if LIVE_SOURCE in targets else FLAT_FILE,
        }
    else:
        connection = _engine_connection(source)
    return {
        "name": name,
        "connection": connection,
        "tables": [],
        "fields": [],
    }


def _engine_connection(connection: dict[str, Any]) -> dict[str, Any]:
    """Map engine `connection_class` to the existing connection classifier field names."""
    mapped = {
        "class": connection.get("connection_class") or "unknown",
        "server": connection.get("server"),
        "database": connection.get("database"),
        "warehouse": connection.get("warehouse"),
        "schema": connection.get("schema"),
        "auth_method": connection.get("auth_method"),
    }
    cleaned = {key: value for key, value in mapped.items() if value not in (None, "")}
    target, reason = powerbi_target(str(cleaned.get("class") or ""), "live")
    cleaned["powerbi_target"] = target
    cleaned["powerbi_target_reason"] = reason
    return cleaned


def _published_keys_from_sources(sources: list[dict[str, Any]]) -> list[str]:
    keys = []
    for source in sources:
        published = source.get("published_datasource")
        if isinstance(published, dict) and published.get("key"):
            keys.append(str(published["key"]))
    return _dedupe_strings(keys)


def _find_binding_signals(value: Any) -> tuple[list[str], list[str]]:
    """Find engine published-datasource signals, preserving unknown-key cases."""
    keys: list[str] = []
    unknown: list[str] = []
    if isinstance(value, list):
        for item in value:
            child_keys, child_unknown = _find_binding_signals(item)
            keys.extend(child_keys)
            unknown.extend(child_unknown)
        return _dedupe_strings(keys), _dedupe_strings(unknown)
    if not isinstance(value, dict):
        return [], []

    signal = value.get("binding_signal")
    if isinstance(signal, dict) and (signal.get("kind") == "published" or signal.get("published_ds_name")):
        signal_keys = _key_strings(signal)
        keys.extend(signal_keys)
        if not signal_keys and signal.get("published_ds_name"):
            unknown.append(str(signal["published_ds_name"]))
    for item in value.values():
        child_keys, child_unknown = _find_binding_signals(item)
        keys.extend(child_keys)
        unknown.extend(child_unknown)
    return _dedupe_strings(keys), _dedupe_strings(unknown)


def _key_strings(value: Any) -> list[str]:
    """Extract explicit published-datasource key fields from a binding signal."""
    keys: list[str] = []
    if isinstance(value, list):
        for item in value:
            keys.extend(_key_strings(item))
        return keys
    if not isinstance(value, dict):
        return keys
    for key, item in value.items():
        if key in {"key", "dedup_key", "published_datasource_key"} and isinstance(item, str) and item.strip():
            keys.append(item)
        elif key == "published_datasource" and isinstance(item, dict):
            keys.extend(_key_strings(item))
        elif isinstance(item, dict | list):
            keys.extend(_key_strings(item))
    return keys


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        marker = json.dumps(item, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def sha256_file(path: Path) -> str:
    """Hash a file without loading large extracts into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_report_definition_json(path: Path) -> bool:
    """Whether `path` is PBIR report-definition JSON, not arbitrary metadata JSON."""
    parts = path.parts
    return path.suffix.lower() == ".json" and "definition" in parts and any(part.endswith(".Report") for part in parts)


def is_engine_artifact(path: Path) -> bool:
    """Files that constitute native engine output for receipt and gate verification."""
    return path.suffix.lower() in ARTIFACT_SUFFIXES or is_report_definition_json(path)


def engine_artifact_records(bundle_dir: Path) -> list[dict[str, Any]]:
    """List native engine artifacts with size and hash for the provenance receipt."""
    records: list[dict[str, Any]] = []
    for root_name in sorted(ENGINE_OUTPUT_DIRS):
        root = bundle_dir / root_name
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file() and is_engine_artifact(p)):
            records.append(
                {
                    "path": path.relative_to(bundle_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return records


def write_engine_receipt(bundle_dir: Path, engine: Path | None = None) -> Path:
    """Write the exact engine-output receipt consumed by `credential_gate.py verify`.

    Also the bundle's answer to *"what built me?"*: `engine` records the resolved engine root, its
    `VERSION` and whether it was the canonical plugin. Before issue #107 that was a manual note in a
    run log, so a bundle produced by 2.113.0 (deprecated Bing maps, a dropped worksheet) and one
    produced by 2.126.0 (azureMap + heat layer) were indistinguishable after the fact.
    """
    receipt = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": engine_provenance(engine),
        "report_sha256": sha256_file(bundle_dir / "report.json") if (bundle_dir / "report.json").is_file() else None,
        "input_manifest_sha256": sha256_file(bundle_dir / "input_manifest.json")
        if (bundle_dir / "input_manifest.json").is_file()
        else None,
        "artifacts": engine_artifact_records(bundle_dir),
    }
    path = bundle_dir / ENGINE_RECEIPT
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return path
