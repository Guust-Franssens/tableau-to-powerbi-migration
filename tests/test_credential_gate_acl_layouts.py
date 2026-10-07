"""Real Windows layout/clear controls for #724, confined to pytest-owned directories.

Run: pytest -q tests/test_credential_gate_acl_layouts.py --basetemp _credential_gate_acl_tests
ACL cleanup is independent of the product, including when a mutation makes a test fail.
"""

# These controls intentionally exercise the gate's existing private readback and syscall seams.
# pylint: disable=protected-access

from __future__ import annotations

import csv
import json
import logging
import os
import platform
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import credential_gate as gate  # noqa: E402  # pylint: disable=wrong-import-position

SOURCE = "fixture-source"
RIGHTS = frozenset({"WD", "AD", "WA"})
WINDOWS_REASON = "write-deny enforcement is an icacls ACL; the marker-only path cannot block a write"


class AclLab:
    """An independent ACL oracle/teardown, never using the product's resolver or clear helper."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(exist_ok=True)
        process = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15, check=False
        )
        assert process.returncode == 0, "test identity discovery failed"
        name, sid = next(csv.reader(process.stdout.strip().splitlines()))
        self.principal = f"*{sid}"
        self.identities = {name.casefold(), sid.casefold(), name.rsplit("\\", 1)[-1].casefold()}

    def command(self, path: Path, *arguments: str) -> str:
        """Do not expose account names, SIDs or raw ACL listings in test output."""
        assert path.is_relative_to(self.root), "ACL operations must stay inside this test's root"
        process = subprocess.run(
            ["icacls", str(path), *arguments], capture_output=True, text=True, timeout=30, check=False
        )
        assert process.returncode == 0, "test-owned ACL operation failed"
        return process.stdout

    def denies(self, path: Path) -> list[tuple[frozenset[str], frozenset[str]]]:
        """Read current-account ACEs independently, with fixed expected rights."""
        entries = []
        for line in self.command(path).splitlines():
            match = re.search(r":((?:\([^)]*\))+)$", line)
            if not match:
                continue
            identity = line[: match.start()].removeprefix(str(path)).strip().casefold()
            if identity not in self.identities:
                continue
            groups = re.findall(r"\(([^)]*)\)", match.group(1).upper())
            if "DENY" in groups:
                entries.append((frozenset(groups[:-1]), frozenset(groups[-1].split(","))))
        return entries

    def deny(self, path: Path) -> None:
        """Install only a fixture deny; no literal account identity is stored in the test."""
        self.command(path, "/deny", f"{self.principal}:(OI)(CI)(WD,AD,WA)")

    def remove(self, path: Path) -> None:
        """Remove one fixture anchor, not descendants or inherited ACEs."""
        self.command(path, "/remove:d", self.principal)

    def cleanup(self) -> None:
        """Restore inherited defaults only within the disposable tree, then assert no deny remains."""
        for current, directories, files in os.walk(self.root, followlinks=False):
            for name in directories + files:
                path = Path(current) / name
                if path.is_symlink():
                    path.unlink()
                elif getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    path.rmdir()
            directories[:] = [name for name in directories if (Path(current) / name).exists()]
        self.command(self.root, "/inheritance:e", "/T", "/C", "/Q")
        self.command(self.root, "/reset", "/T", "/C", "/Q")
        assert "(DENY)" not in self.command(self.root, "/T").upper(), "test cleanup left a deny ACE"


@pytest.fixture(scope="session", autouse=True)
def acl_session_cleanup(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Also clean test-owned denies from unchanged gate modules run in this same pytest session."""
    yield
    if sys.platform == "win32":
        AclLab(tmp_path_factory.getbasetemp()).cleanup()


@pytest.fixture(name="acl_lab")
def _acl_lab_fixture(tmp_path: Path) -> Iterator[AclLab]:
    """Even assertion failures and deliberate product mutations must leave no fixture ACEs."""
    if platform.system() != "Windows":
        pytest.skip(WINDOWS_REASON)
    lab = AclLab(tmp_path / "acl")
    try:
        yield lab
    finally:
        lab.cleanup()


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture bytes", encoding="utf-8")
    return path


def _native(root: Path, kinds: tuple[str, ...] = ("SemanticModel", "Report")) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for kind in kinds:
        name, definition = (
            ("DifferentModel", "model.tmdl") if kind == "SemanticModel" else ("OtherReport", "report.json")
        )
        files.append(_file(root / f"{name}.{kind}" / "definition" / definition))
    return files


def _bundle(root: Path) -> tuple[Path, list[Path]]:
    files = _native(root / "pbip" / "Unit")
    (root / "report.json").write_text("{}", encoding="utf-8")
    (root / "input_manifest.json").write_text("{}", encoding="utf-8")
    return root, files


def _unit(root: Path, kinds: tuple[str, ...] = ("SemanticModel", "Report")) -> tuple[Path, list[Path]]:
    files = _native(root, kinds)
    (root / "migration-spec.json").write_text("{}", encoding="utf-8")
    return root, files


def _denied(files: list[Path]) -> None:
    for path in files:
        original = path.read_bytes()
        with pytest.raises(PermissionError, match="denied"):
            path.write_text("must not land", encoding="utf-8")
        assert path.read_bytes() == original, "a blocked existing artifact changed"
        with pytest.raises(PermissionError, match="denied"):
            (path.parent / "new-file.txt").write_text("must not land", encoding="utf-8")
        with pytest.raises(PermissionError, match="denied"):
            (path.parent / "new-directory").mkdir()


def _writable(files: list[Path]) -> None:
    for path in files:
        try:
            path.write_text("post-clear bytes", encoding="utf-8")
        except PermissionError:
            pytest.fail("post-clear artifact writes must succeed", pytrace=False)
        assert path.read_text(encoding="utf-8") == "post-clear bytes", "clear did not restore artifact writes"


def _earned_clear(root: Path) -> int:
    return gate.clear_block(root, "fixture DATA_OK", earned=True, sources=[SOURCE])


def _probe_write(root: Path) -> None:
    model = root / "_probe" / "Probe.SemanticModel"
    for relative in ("definition/model.tmdl", ".pbi/cache.abf"):
        try:
            path = _file(model / relative)
        except PermissionError:
            pytest.fail("correct-root probe must remain writable", pytrace=False)
        assert path.read_text(encoding="utf-8") == "fixture bytes", "correct-root probe must remain writable"


def test_bundle_denies_existing_and_new_artifacts(acl_lab: AclLab) -> None:
    """Existing emitted bytes, new descendants and post-clear writes are independent syscall oracles."""
    root, files = _bundle(acl_lab.root / "bundle")
    assert gate.apply_block(root, [SOURCE]) == 0
    _denied(files)
    assert any({"OI", "CI", "DENY"} <= flags and RIGHTS <= rights for flags, rights in acl_lab.denies(root / "pbip"))
    assert not (root / "fabric").exists(), "bundle arm must not invent fabric"
    assert gate.status(root) == 1
    assert _earned_clear(root) == 0
    _writable(files)
    assert gate.inspect_physical_barrier(root) == ("clear", "physical_clear")
    assert gate.status(root) == 0


@pytest.mark.parametrize("kinds", [("SemanticModel", "Report"), ("SemanticModel",), ("Report",)])
def test_native_unit_denies_each_artifact_directory(acl_lab: AclLab, kinds: tuple[str, ...]) -> None:
    """Unequal names and model/report-only units share the same kernel-write invariant."""
    root, files = _unit(acl_lab.root / "unit", kinds)
    assert gate.apply_block(root, [SOURCE]) == 0
    _denied(files)
    for path in files:
        assert any("I" not in flags and RIGHTS <= rights for flags, rights in acl_lab.denies(path.parents[1]))
    assert not (root / "fabric").exists(), "native unit arm must not invent fabric"
    assert _earned_clear(root) == 0
    _writable(files)
    assert gate.inspect_physical_barrier(root) == ("clear", "physical_clear")


@pytest.mark.parametrize("layout", ["parser", "package", "mixed-package", "promoted"])
def test_fabric_layouts_remain_supported(acl_lab: AclLab, layout: str) -> None:
    """Parser/package identity outranks the engine-shaped report marker without changing fabric."""
    root = acl_lab.root / layout
    files = _native(root / "fabric")
    if layout != "promoted":
        (root / "migration-spec.json").write_text("{}", encoding="utf-8")
    if "package" in layout:
        (root / "package-manifest.json").write_text("{}", encoding="utf-8")
    if layout == "mixed-package":
        (root / "report.json").write_text("{}", encoding="utf-8")
    assert gate.apply_block(root, [SOURCE], force_scope=layout == "promoted") == 0
    _denied(files)
    assert gate.denied_dirs(root, create=False) == [root / "fabric"]
    assert not (root / "pbip").exists(), "a package's report.json must not invent pbip"
    assert _earned_clear(root) == 0
    _writable(files)


@pytest.mark.parametrize("layout", ["bundle", "unit", "parser"])
def test_correct_root_probe_remains_writable(acl_lab: AclLab, layout: str) -> None:
    """The active root's sandbox, including its cache, must not inherit this gate's deny."""
    root = acl_lab.root / layout
    if layout == "bundle":
        _bundle(root)
    elif layout == "unit":
        _unit(root)
    else:
        root.mkdir()
        (root / "migration-spec.json").write_text("{}", encoding="utf-8")
    assert gate.apply_block(root, [SOURCE]) == 0
    _probe_write(root)
    assert _earned_clear(root) == 0


@pytest.mark.parametrize("first", ["outer", "inner"])
def test_nested_clears_preserve_the_other_gate(acl_lab: AclLab, first: str) -> None:
    """Both physical removals are required; only outer-first asserts durable earned clearance."""
    outer, files = _bundle(acl_lab.root / "bundle")
    inner = outer / "pbip" / "Unit"
    (inner / "migration-spec.json").write_text("{}", encoding="utf-8")
    assert gate.apply_block(inner, [SOURCE]) == 0
    assert gate.apply_block(outer, [SOURCE]) == 0
    _probe_write(outer)
    _denied(files)
    if first == "outer":
        assert _earned_clear(outer) == 0
        _denied(files)
        assert any("I" not in flags for flags, _rights in acl_lab.denies(files[0].parents[1]))
        _probe_write(inner)
        assert _earned_clear(inner) == 0
        assert gate._clear_was_earned(outer) == gate._clear_was_earned(inner) == "probe-cleared"
        assert gate.verify(outer) == gate.verify(inner) == 0
    else:
        # This order asserts physical preservation only: the outer deny can prevent inner audit writes.
        _earned_clear(inner)
        _denied(files)
        assert acl_lab.denies(outer / "pbip"), "inner clear removed the outer gate"
        assert all("I" in flags for flags, _rights in acl_lab.denies(files[0].parents[1]))
        assert _earned_clear(outer) == 0
    _writable(files)


def test_enforcement_requires_all_targets_but_residue_needs_any(
    acl_lab: AclLab, caplog: pytest.LogCaptureFixture
) -> None:
    """Empty artifact directories isolate coverage from the separate artifact/provenance violation."""
    root, files = _unit(acl_lab.root / "unit")
    for path in files:
        path.unlink()
    assert gate.apply_block(root, [SOURCE]) == 0
    assert gate.verify(root) == 0, "a completely enforced empty unit must verify clean"
    acl_lab.remove(files[1].parents[1])
    caplog.clear()
    assert gate.verify(root) == 1, "one denied model cannot stand in for an unprotected report"
    assert "ENFORCEMENT REMOVED" in caplog.text
    assert gate._has_deny_ace(root) is True, "the any-fold must retain one residual deny"
    (root / gate.MARKER).unlink()
    assert gate.inspect_physical_barrier(root) == ("blocked", "physical_acl_blocked")
    assert gate.status(root) == 1
    acl_lab.remove(files[0].parents[1])
    assert gate.inspect_physical_barrier(root) == ("clear", "physical_clear")
    assert gate.status(root) == 0


def test_failed_clear_cannot_announce_success(
    acl_lab: AclLab, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A successful process without an ACL effect cannot launder a residual physical barrier."""
    root, files = _bundle(acl_lab.root / "bundle")
    assert gate.apply_block(root, [SOURCE]) == 0
    real = gate._icacls
    monkeypatch.setattr(gate, "_icacls", lambda args: (0, "") if "/remove:d" in args else real(args))
    caplog.clear()
    assert _earned_clear(root) != 0, "unchanged ACLs after a successful command are not a clear"
    assert "credential gate CLEARED" not in caplog.text
    (root / gate.MARKER).unlink()
    assert gate.inspect_physical_barrier(root) == ("blocked", "physical_acl_blocked")
    _denied(files)


@pytest.mark.parametrize("location", ["anchor", "descendant"])
@pytest.mark.parametrize("fault", ["protected", "explicit-grant"])
def test_unsupported_acl_refuses_before_claiming_enforcement(
    acl_lab: AclLab, caplog: pytest.LogCaptureFixture, location: str, fault: str
) -> None:
    """Cannot-establish must neither change the existing permissions nor claim physical protection."""
    root, files = _bundle(acl_lab.root / "bundle")
    target = root / "pbip" if location == "anchor" else files[0]
    if fault == "protected":
        acl_lab.command(target, "/inheritance:d")
    else:
        acl_lab.command(target, "/grant", f"{acl_lab.principal}:(WD,AD,WA)")
    before = acl_lab.command(target)
    caplog.set_level(logging.INFO, logger="credential_gate")
    assert gate.apply_block(root, [SOURCE]) != 0, "unsupported ACLs cannot establish coverage"
    assert "CANNOT ESTABLISH" in caplog.text and "ENFORCED" not in caplog.text
    assert not (root / gate.MARKER).exists(), "refusal must not claim writes_blocked"
    assert acl_lab.command(target) == before, "refusal must not repair or remove an existing ACL"
    _writable(files)


def test_rearm_is_idempotent_and_partial_apply_can_retry(
    acl_lab: AclLab, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A partial arm retains its stop; retry uses, rather than duplicates, its completed anchors."""
    root, files = _unit(acl_lab.root / "unit")
    real = gate._icacls
    fail_target = files[1].parents[1]
    with monkeypatch.context() as patch:
        patch.setattr(
            gate, "_icacls", lambda args: (5, "") if args[0] == str(fail_target) and "/deny" in args else real(args)
        )
        caplog.set_level(logging.INFO, logger="credential_gate")
        assert gate.apply_block(root, [SOURCE]) != 0
        assert "ENFORCED" not in caplog.text
        assert json.loads((root / gate.MARKER).read_text(encoding="utf-8"))["writes_blocked"] is False
        _denied(files[:1])
        _writable(files[1:])
    assert gate.apply_block(root, [SOURCE]) == 0
    assert gate.apply_block(root, [SOURCE]) == 0
    _denied(files)
    assert all(len([ace for ace in acl_lab.denies(path.parents[1]) if "I" not in ace[0]]) == 1 for path in files)
    assert _earned_clear(root) == 0
    _writable(files)


def _history(root: Path) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": "block",
        "detail": 'sources_json=["fixture-source"]',
        "user": "fixture-actor",
        "scope": str(root.resolve()),
        "sources": [SOURCE],
    }
    (root / gate.AUDIT).write_text(json.dumps(record) + "\n", encoding="utf-8")


@pytest.mark.parametrize("attributed", [True, False])
def test_empty_legacy_fabric_cleanup_requires_same_root_history(acl_lab: AclLab, attributed: bool) -> None:
    """An exact old empty-fabric deny needs this root's history and never authorizes deletion."""
    root, files = _bundle(acl_lab.root / "bundle")
    legacy = root / "fabric"
    legacy.mkdir()
    acl_lab.deny(legacy)
    if attributed:
        _history(root)
    assert gate.inspect_physical_barrier(root) == ("blocked", "physical_acl_blocked")
    result = gate.clear_block(root, "fixture legacy cleanup")
    assert (result == 0) is attributed, "a legacy deny needs same-root attribution before removal"
    assert legacy.is_dir() and not list(legacy.iterdir()), "legacy cleanup must not delete or populate fabric"
    assert bool(acl_lab.denies(legacy)) is not attributed
    if attributed:
        assert gate.inspect_physical_barrier(root) == ("clear", "physical_clear")
        _writable(files)


@pytest.mark.parametrize("fault", ["populated", "different-mask"])
def test_legacy_cleanup_refuses_ambiguous_denies(acl_lab: AclLab, fault: str) -> None:
    """Populated or differently permissioned legacy targets cannot be swept away."""
    root, _files = _bundle(acl_lab.root / "bundle")
    legacy = root / "fabric"
    legacy.mkdir()
    _history(root)
    if fault == "populated":
        _file(legacy / "not-disposable.txt")
        acl_lab.deny(legacy)
    else:
        acl_lab.command(legacy, "/deny", f"{acl_lab.principal}:(OI)(CI)(WD)")
    before = acl_lab.command(legacy)
    assert gate.clear_block(root, "ambiguous legacy") != 0
    assert acl_lab.command(legacy) == before, "ambiguous legacy permissions must remain untouched"
    assert gate.inspect_physical_barrier(root)[0] != "clear"


def test_native_unit_preserves_empty_legacy_fabric(acl_lab: AclLab) -> None:
    """An old empty husk is tolerated but never mistaken for coverage of native artifacts."""
    root, files = _unit(acl_lab.root / "unit")
    (root / "fabric").mkdir()
    assert gate.apply_block(root, [SOURCE]) == 0
    _denied(files)
    assert not acl_lab.denies(root / "fabric"), "legacy fabric is not native-unit enforcement coverage"
    assert _earned_clear(root) == 0
    assert (root / "fabric").is_dir()
    _writable(files)


def test_ambiguous_populated_layout_refuses_without_changes(acl_lab: AclLab) -> None:
    """Do not select one populated working tree and quietly leave the other writable."""
    root, files = _unit(acl_lab.root / "unit")
    _file(root / "fabric" / "conflicting.txt")
    assert gate.apply_block(root, [SOURCE]) != 0
    assert not (root / gate.MARKER).exists()
    assert not (root / "pbip").exists()
    _writable(files)


@pytest.mark.parametrize("location", ["anchor", "descendant"])
def test_reparse_target_refuses_without_denying_its_destination(acl_lab: AclLab, location: str) -> None:
    """The read-only safety walk must not carry an anchor's permission mutation through a junction."""
    from test_package_filesystem import link_directory  # pylint: disable=import-outside-toplevel

    root, _files = _bundle(acl_lab.root / "bundle")
    outside = _file(acl_lab.root / "outside" / "untouched.txt")
    target = root / "pbip"
    if location == "anchor":
        target.rename(root / "retained-fixture")
    else:
        target = target / "Unit" / "linked-directory"
    link_directory(target, outside.parent)
    assert gate.apply_block(root, [SOURCE]) != 0
    assert not (root / gate.MARKER).exists()
    assert not (root / "fabric").exists()
    assert not acl_lab.denies(outside.parent), "a rejected link must never carry the deny to its destination"
    _writable([outside])


@pytest.mark.parametrize("identity,anchor", [("migration-spec.json", "fabric"), ("engine-output-receipt.json", "pbip")])
def test_empty_recognized_layout_creates_only_its_anchor(acl_lab: AclLab, identity: str, anchor: str) -> None:
    """The approved receipt-only/parser clarification preserves the unchanged scope controls."""
    root = acl_lab.root / "empty"
    root.mkdir()
    (root / identity).write_text("{}", encoding="utf-8")
    assert gate.apply_block(root, [SOURCE]) == 0
    assert {path.name for path in root.iterdir() if path.is_dir()} == {anchor, "_probe"}
    assert _earned_clear(root) == 0


@pytest.mark.parametrize("identity", [None, "migration-spec.json", "engine-output-receipt.json"])
def test_clear_and_read_never_create_directories(tmp_path: Path, identity: str | None) -> None:
    """Missing targets and probes remain missing after every read/clear consumer."""
    if identity:
        (tmp_path / identity).write_text("{}", encoding="utf-8")
    assert gate.inspect_physical_barrier(tmp_path) == ("clear", "physical_clear")
    assert gate.status(tmp_path) == 0
    assert gate.verify(tmp_path) == 0
    assert gate.clear_block(tmp_path, "empty teardown") == 0
    assert not any(path.is_dir() for path in tmp_path.iterdir()), "read/clear must not create a target or probe"


def test_acl_query_failure_is_not_clear(acl_lab: AclLab, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed query yields path-free cannot-establish, never a clean physical verdict."""
    root, _files = _bundle(acl_lab.root / "bundle")
    _history(root)
    monkeypatch.setattr(gate, "_icacls", lambda _args: (5, "private-canary"))
    result = gate.inspect_physical_barrier(root)
    assert result == ("cannot_establish", "physical_acl_query_failed")
    assert "private-canary" not in repr(result) and str(root) not in repr(result)
    assert gate.status(root) == gate.verify(root) == 3
    assert gate.clear_block(root, "failed read") != 0


def test_non_windows_keeps_marker_only_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Windows the original marker-only workflow never invokes the Windows ACL boundary."""
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate, "_icacls", lambda _args: pytest.fail("marker-only must not invoke Windows ACLs"))
    (tmp_path / "migration-spec.json").write_text("{}", encoding="utf-8")
    assert gate.apply_block(tmp_path, [SOURCE]) == 0
    _probe_write(tmp_path)
    records = [json.loads(line) for line in (tmp_path / gate.AUDIT).read_text(encoding="utf-8").splitlines()]
    assert records[-1]["action"] == "block-marker-only"
    assert not (tmp_path / "fabric").exists()
    assert _earned_clear(tmp_path) == 0
