"""Package-entry integrity verifier controls (issue #562).

Tests that ``package_contract.verify_package_entry`` correctly identifies:
- clean workbook and datasource packages
- malformed / non-object / duplicate-key manifests
- malformed contents maps
- missing / changed required roles
- traversal / absolute / case-alias paths
- contradictory LUID
- manifest deleted with legacy source present
- foreign oracle files
- symlinks (where platform permits)
- intended fabric edits are non-clean in entry mode
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import package_contract as pc  # noqa: E402  # pylint: disable=wrong-import-position
from test_check_reference_readiness import (  # noqa: E402  # pylint: disable=wrong-import-position
    write_engine_report,
    write_oracle,
    write_png,
    write_report,
    write_workbook,
)

UNIT = "Book"
WB_LUID = "11111111-2222-3333-4444-555555555555"
OTHER_LUID = "99999999-8888-7777-6666-555555555555"


# ---------------------------------------------------------------------------
# Fixture helpers — build a minimal valid package
# ---------------------------------------------------------------------------

def _view(name: str, luid: str, *, workbook_luid: str = WB_LUID) -> dict:
    return {
        "view_name": name,
        "view_luid": luid,
        "content_url": f"Book/sheets/{name}",
        "view_type": "worksheet",
        "workbook_luid": workbook_luid,
        "workbook_name": "Book",
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_package(tmp_path: Path, *, kind: str = "workbook") -> Path:
    """Build a minimal valid package and return its root directory."""
    pkg = tmp_path / "packages" / UNIT
    pkg.mkdir(parents=True)

    # -- scaffold files --
    report_json = {"workbooks": [{"name": UNIT}], "datasources": []}
    if kind == "datasource":
        report_json = {"workbooks": [], "datasources": [{"name": UNIT}]}
    (pkg / "report.json").write_text(json.dumps(report_json), encoding="utf-8")

    (pkg / "README.md").write_text("# Package\n", encoding="utf-8")
    (pkg / "handover.md").write_text("# Handover\n", encoding="utf-8")
    (pkg / "source-provenance.json").write_text(json.dumps({
        "inputs": [{
            "input": {"file": f"{WB_LUID}_{UNIT}.twb", "sha256": "a" * 64},
            "origin": {"workbook_luid": WB_LUID, "workbook_name": "Book", "match": "sha256"},
        }],
    }), encoding="utf-8")
    (pkg / "engine-output-receipt.json").write_text(
        json.dumps({"engine": {"version": "2.0.0"}}), encoding="utf-8",
    )
    (pkg / "migration-spec.schema.json").write_text("{}", encoding="utf-8")

    if kind == "workbook":
        # Asset
        assets = pkg / "assets"
        assets.mkdir()
        source = assets / f"{WB_LUID}_{UNIT}.twb"
        source.write_text("<workbook />", encoding="utf-8")

        # Handover slice
        handover = pkg / "handover"
        handover.mkdir()
        (handover / f"{UNIT}.json").write_text(json.dumps({"unit": UNIT}), encoding="utf-8")

        # Migration spec
        (pkg / "migration-spec.json").write_text(json.dumps({"unit": UNIT}), encoding="utf-8")

        # Fabric (report + model)
        report_dir = pkg / "fabric" / f"{UNIT}.Report"
        report_dir.mkdir(parents=True)
        (report_dir / "definition.pbir").write_text(
            json.dumps({"datasetReference": {"byPath": {"path": f"../{UNIT}.SemanticModel"}}}),
            encoding="utf-8",
        )
        model_dir = pkg / "fabric" / f"{UNIT}.SemanticModel"
        model_dir.mkdir(parents=True)
        (model_dir / "definition" / "tables").mkdir(parents=True)
        (model_dir / "model.bim").write_text("{}", encoding="utf-8")

    elif kind == "datasource":
        # Fabric model only
        model_dir = pkg / "fabric" / f"{UNIT}.SemanticModel"
        model_dir.mkdir(parents=True)
        (model_dir / "model.bim").write_text("{}", encoding="utf-8")

    # -- write manifest --
    _write_manifest(pkg, kind=kind)
    return pkg


def _write_manifest(pkg: Path, *, kind: str = "workbook", extra_manifest: dict | None = None) -> None:
    """Compute contents and write a valid manifest."""
    files_map: dict[str, str] = {}
    for path in sorted(pkg.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(pkg).as_posix()
        if rel == pc.MANIFEST_NAME:
            continue
        files_map[rel] = _sha256(path)

    manifest: dict = {
        "unit": UNIT,
        "kind": kind,
        "engine": "2.0.0",
        "packaged": True,
        "self_contained": True,
        "artifacts": {
            "migration_spec": "migration-spec.json" if kind == "workbook" else None,
            "migration_spec_schema": "migration-spec.schema.json",
            "asset": f"assets/{WB_LUID}_{UNIT}.twb" if kind == "workbook" else None,
            "asset_route": "sha256" if kind == "workbook" else None,
            "report": f"fabric/{UNIT}.Report" if kind == "workbook" else None,
            "model": f"fabric/{UNIT}.SemanticModel",
            "handover": f"handover/{UNIT}.json" if kind == "workbook" else None,
        },
        "model_binding": {
            "kind": "byPath",
            "resolves_in_package": True,
            "path": f"../{UNIT}.SemanticModel",
        } if kind == "workbook" else {},
        "workbook_identity": {
            "luid": WB_LUID,
            "match": "sha256",
            "workbook_name": "Book",
            "reason": None,
        } if kind == "workbook" else {},
        "data_sources": {},
        "oracle": {"objects": []},
        "notes": [],
        "contents": {"files": files_map},
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    (pkg / pc.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Clean packages
# ---------------------------------------------------------------------------

class TestCleanPackage:
    """A freshly produced package passes the entry verifier exactly."""

    def test_clean_workbook(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path, kind="workbook")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_CLEAN, result.findings
        assert result.kind == "workbook"
        assert result.source is not None
        assert result.findings == []

    def test_clean_datasource(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path, kind="datasource")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_CLEAN, result.findings
        assert result.kind == "datasource"
        assert result.findings == []


# ---------------------------------------------------------------------------
# Malformed manifest
# ---------------------------------------------------------------------------

class TestMalformedManifest:
    """Manifest that cannot be parsed or has wrong shape."""

    def test_missing_manifest(self, tmp_path: Path) -> None:
        pkg = tmp_path / "packages" / UNIT
        pkg.mkdir(parents=True)
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_UNASSESSABLE
        assert any("no package-manifest.json" in f for f in result.findings)

    def test_not_json(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        (pkg / pc.MANIFEST_NAME).write_text("not json {{{", encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_UNASSESSABLE
        assert any("not valid JSON" in f for f in result.findings)

    def test_non_object(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        (pkg / pc.MANIFEST_NAME).write_text("[1, 2, 3]", encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_UNASSESSABLE
        assert any("expected object" in f for f in result.findings)

    def test_duplicate_keys(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        (pkg / pc.MANIFEST_NAME).write_text('{"kind": "workbook", "kind": "datasource"}', encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_UNASSESSABLE
        assert any("duplicate key" in f for f in result.findings)


# ---------------------------------------------------------------------------
# Malformed contents
# ---------------------------------------------------------------------------

class TestMalformedContents:
    """Contents map that is missing or has wrong shape."""

    def test_no_contents(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        del manifest["contents"]
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_UNASSESSABLE

    def test_contents_files_not_object(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["contents"]["files"] = "not a dict"
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_UNASSESSABLE


# ---------------------------------------------------------------------------
# Missing / changed roles
# ---------------------------------------------------------------------------

class TestMissingChangedRoles:
    """Required files that are absent or have changed bytes."""

    def test_missing_required_scaffold(self, tmp_path: Path) -> None:
        """Deleting a required scaffold file is a finding."""
        pkg = _make_package(tmp_path)
        (pkg / "README.md").unlink()
        _write_manifest(pkg)  # re-write to reflect the deletion
        result = pc.verify_package_entry(pkg)
        assert result.state != pc.STATE_CLEAN
        assert any("README.md" in f for f in result.findings)

    def test_changed_byte(self, tmp_path: Path) -> None:
        """Changing one byte in a declared file is a hash-mismatch finding."""
        pkg = _make_package(tmp_path)
        target_file = pkg / "README.md"
        target_file.write_text("# Changed\n", encoding="utf-8")
        # Do NOT rewrite manifest — the old hash should mismatch
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("hash mismatch" in f for f in result.findings)

    def test_removed_asset_with_file_still_present(self, tmp_path: Path) -> None:
        """Asset role removed from manifest but file still on disk → undeclared file."""
        pkg = _make_package(tmp_path)
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        asset_key = manifest["artifacts"]["asset"]
        del manifest["contents"]["files"][asset_key]
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("undeclared file" in f for f in result.findings)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

class TestPathSafety:
    """Traversal, absolute, and case-alias paths are rejected."""

    def test_traversal_path(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["contents"]["files"]["../escape.txt"] = "a" * 64
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("traversal" in f for f in result.findings)

    def test_absolute_path(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["contents"]["files"]["/etc/passwd"] = "a" * 64
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("absolute path" in f for f in result.findings)

    def test_case_alias(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["contents"]["files"]["Readme.md"] = "a" * 64
        # README.md already exists as a key
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("case-alias" in f.lower() or "collision" in f.lower() for f in result.findings)


# ---------------------------------------------------------------------------
# Contradictory LUID
# ---------------------------------------------------------------------------

class TestContradictoryLUID:
    def test_filename_luid_vs_manifest_luid(self, tmp_path: Path) -> None:
        """Asset filename declares one LUID, manifest identity says another."""
        pkg = _make_package(tmp_path)
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        # Change the workbook_identity luid to mismatch the filename
        manifest["workbook_identity"]["luid"] = OTHER_LUID
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("contradictory LUID" in f or "LUID" in f for f in result.findings)


# ---------------------------------------------------------------------------
# Manifest deleted with legacy source present
# ---------------------------------------------------------------------------

class TestManifestDeletedLegacy:
    def test_manifest_deleted_package_shaped(self, tmp_path: Path) -> None:
        """Manifest deleted from a packages/<Unit>/ dir → unassessable, not silently ok."""
        pkg = _make_package(tmp_path)
        (pkg / pc.MANIFEST_NAME).unlink()
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_UNASSESSABLE
        assert any("no package-manifest.json" in f for f in result.findings)


# ---------------------------------------------------------------------------
# Foreign oracle file
# ---------------------------------------------------------------------------

class TestForeignOracle:
    def test_extra_oracle_file(self, tmp_path: Path) -> None:
        """An oracle file not referenced by any manifest object is foreign."""
        pkg = _make_package(tmp_path)
        oracle_dir = pkg / "oracle"
        oracle_dir.mkdir(exist_ok=True)
        foreign = oracle_dir / "foreign-render.png"
        write_png(foreign)
        # Add to contents but not to oracle objects
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["contents"]["files"]["oracle/foreign-render.png"] = _sha256(foreign)
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("foreign oracle" in f for f in result.findings)


# ---------------------------------------------------------------------------
# Symlink
# ---------------------------------------------------------------------------

class TestSymlink:
    def test_file_symlink(self, tmp_path: Path) -> None:
        """A symlink in place of a declared file is non-clean."""
        pkg = _make_package(tmp_path)
        readme = pkg / "README.md"
        target = tmp_path / "real_readme.md"
        target.write_text("# Real\n", encoding="utf-8")
        readme.unlink()
        try:
            readme.symlink_to(target)
        except OSError:
            pytest.skip("cannot create symlinks on this platform")
        _write_manifest(pkg)
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("symlink" in f for f in result.findings)

    @pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
    def test_directory_junction(self, tmp_path: Path) -> None:  # pragma: no cover
        """A junction substituted for a real directory is detected."""
        pkg = _make_package(tmp_path)
        real = tmp_path / "real_fabric"
        real.mkdir()
        (real / "dummy.txt").write_text("x", encoding="utf-8")
        fabric = pkg / "fabric"
        import shutil
        shutil.rmtree(fabric)
        # Create junction
        import subprocess
        subprocess.run(["cmd", "/c", "mklink", "/J", str(fabric), str(real)], check=True)
        _write_manifest(pkg)
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS


# ---------------------------------------------------------------------------
# Fabric edit in entry mode
# ---------------------------------------------------------------------------

class TestFabricEditInEntryMode:
    def test_intended_fabric_edit_is_non_clean(self, tmp_path: Path) -> None:
        """After packaging, editing a fabric file must be non-clean at entry time."""
        pkg = _make_package(tmp_path)
        # Verify clean first
        assert pc.verify_package_entry(pkg).state == pc.STATE_CLEAN

        # Simulate an agent edit to a fabric file
        for path in pkg.rglob("*"):
            if path.is_file() and "fabric" in path.relative_to(pkg).parts:
                path.write_text("edited content", encoding="utf-8")
                break
        # Do NOT rewrite manifest — the edit should be detected
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("hash mismatch" in f for f in result.findings)


# ---------------------------------------------------------------------------
# Unclassified kind
# ---------------------------------------------------------------------------

class TestUnclassifiedKind:
    def test_unclassified_kind(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        manifest = json.loads((pkg / pc.MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["kind"] = "unknown_thing"
        (pkg / pc.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        result = pc.verify_package_entry(pkg)
        assert result.state == pc.STATE_FINDINGS
        assert any("unclassified" in f for f in result.findings)


# ---------------------------------------------------------------------------
# Integration with check_reference_readiness
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_non_clean_package_blocked_by_scan(self, tmp_path: Path) -> None:
        """check_reference_readiness.scan blocks a non-clean package target."""
        import check_reference_readiness as crr  # pylint: disable=import-outside-toplevel

        pkg = _make_package(tmp_path)
        # Corrupt a file to make it non-clean
        (pkg / "README.md").write_text("# Corrupted\n", encoding="utf-8")
        report = crr.scan(pkg)
        assert report["status"] == crr.STATUS_CANNOT_ESTABLISH

    def test_clean_package_proceeds(self, tmp_path: Path) -> None:
        """check_reference_readiness.scan does not block a clean package."""
        import check_reference_readiness as crr  # pylint: disable=import-outside-toplevel

        pkg = _make_package(tmp_path)
        report = crr.scan(pkg)
        # A clean package should proceed to the normal scan — it may still have findings about
        # reference readiness, but it should NOT be blocked by the entry verifier.
        assert report["status"] != crr.STATUS_CANNOT_ESTABLISH or (
            "package-entry integrity" not in report.get("units", [{}])[0].get("detail", "")
        )
