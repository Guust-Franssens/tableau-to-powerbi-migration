"""Regression tests for `scripts/work_dirs.py` - the canonical pre-bundle work-layout helper
(issue #291 gaps 1/3, implementing issue #234's corrected `_runs/<NNN>-<slug>/` design).

Every test uses `tmp_path` as `repo_root` so this suite never touches the real repo's `_runs/`.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from work_dirs import (
    CANONICAL_SUBDIRS,
    allocate_run,
    list_runs,
    runs_root,
    sanitize_unit_key,
)


# --------------------------------------------------------------------------------------
# sanitize_unit_key - awkward characters must never crash or produce an unsafe/empty slug
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Enterprise Dashboards", "enterprise-dashboards"),
        ("Sales / Q1 Report", "sales-q1-report"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("Depot Genève", "depot-geneve"),
        # `<name>` rather than a bare word: the absolute-user-path gate
        # (`set_data_folder.py --check`) exempts syntactically unambiguous placeholders but NOT bare
        # words, because `bad`, `user` and `username` are all real registrable account names. This
        # repo is public and the gate caught the earlier literal in CI.
        ("C:\\Users\\<name>\\path", "c-users-name-path"),
        ("../../etc/passwd", "etc-passwd"),
        ("****!!!!####", "unit"),
        ("", "unit"),
    ],
)
def test_sanitize_unit_key_handles_awkward_characters(raw: str, expected: str) -> None:
    assert sanitize_unit_key(raw) == expected


def test_sanitize_unit_key_never_returns_empty_or_bare_punctuation() -> None:
    for raw in ("", "   ", "---", "\u00a0\u2028", "!!!"):
        got = sanitize_unit_key(raw)
        assert got, f"sanitize_unit_key({raw!r}) returned an empty slug"
        assert got.strip("-") == got, f"sanitize_unit_key({raw!r}) left a leading/trailing '-': {got!r}"


def test_sanitize_unit_key_caps_length() -> None:
    got = sanitize_unit_key("x" * 500)
    assert len(got) <= 60
    assert got  # capping must never produce an empty result


def test_sanitize_unit_key_is_never_load_bearing_for_identity() -> None:
    """Two different display names that sanitize to the SAME slug must still get distinct runs -
    the slug is decoration, the run number is identity (issue #234, rule 2)."""
    assert sanitize_unit_key("Finance") == sanitize_unit_key("finance!!!")


# --------------------------------------------------------------------------------------
# allocate_run - numbering, atomicity, canonical subdirs, manifest
# --------------------------------------------------------------------------------------


def test_allocate_run_starts_at_one_and_creates_canonical_subdirs(tmp_path: Path) -> None:
    run = allocate_run("Enterprise Dashboards", repo_root=tmp_path)

    assert run.run_number == 1
    assert run.root == runs_root(tmp_path) / "001-enterprise-dashboards"
    assert run.root.is_dir()
    for name in CANONICAL_SUBDIRS:
        assert run.subdir(name).is_dir(), f"canonical subdir {name!r} was not created"
    # every accessor property must agree with subdir()
    assert run.assessment == run.subdir("assessment")
    assert run.assets == run.subdir("assets")
    assert run.bundle == run.subdir("bundle")
    assert run.oracle == run.subdir("oracle")
    assert run.deliverables == run.subdir("deliverables")
    assert run.scratch == run.subdir("scratch")


def test_allocate_run_numbering_is_global_across_units_not_per_unit(tmp_path: Path) -> None:
    """issue #234's own worked example numbers 001-entdash, 002-shipping consecutively across
    two DIFFERENT units - numbering is one global sequence under `_runs/`, never per-unit."""
    first = allocate_run("entdash", repo_root=tmp_path)
    second = allocate_run("shipping", repo_root=tmp_path)
    third = allocate_run("entdash", repo_root=tmp_path)  # same unit again

    assert (first.run_number, second.run_number, third.run_number) == (1, 2, 3)


def test_allocate_run_never_renumbers_or_reuses_a_number(tmp_path: Path) -> None:
    run_a = allocate_run("acme", repo_root=tmp_path)
    run_b = allocate_run("acme", repo_root=tmp_path)
    assert run_a.run_number != run_b.run_number
    assert run_a.root.is_dir()  # the first run's directory must still exist, untouched


def test_allocate_run_retries_past_a_pre_existing_collision(tmp_path: Path) -> None:
    """Simulates a collision: pre-create the directory the next allocation would naturally pick,
    and assert `allocate_run` skips over it rather than raising or overwriting (issue #234 AC1)."""
    root = runs_root(tmp_path)
    root.mkdir(parents=True)
    (root / "001-manual").mkdir()  # occupies run 1 without going through allocate_run at all

    run = allocate_run("acme", repo_root=tmp_path)

    assert run.run_number == 2, "allocate_run must skip an externally-occupied run number"
    assert run.root.name == "002-acme"


def test_allocate_run_retries_on_a_true_concurrent_mkdir_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates two processes computing the SAME candidate number simultaneously: a split second
    before THIS call's `mkdir` runs, another process creates the same directory for real. The
    collision-by-pre-existing-directory test above never exercises this at all (the pre-scan
    resolves that case before any `mkdir` call happens) - this one forces the real race.

    Delegates to the REAL `Path.mkdir` throughout (never fabricates the raise itself), so this is
    sensitive to `exist_ok`: if `allocate_run` ever changed to `exist_ok=True`, this directory would
    silently NOT raise on the second call and the mutation would go uncaught - which is exactly what
    happened during mutation testing until this test was rewritten to stop injecting the exception
    directly."""
    import pathlib

    original_mkdir = pathlib.Path.mkdir
    triggered = {"n": 0}

    def racy_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "001-acme" and triggered["n"] == 0:
            triggered["n"] += 1
            original_mkdir(self, parents=True, exist_ok=True)  # another process "wins" the race
        original_mkdir(self, *args, **kwargs)  # real semantics decide whether THIS call raises

    monkeypatch.setattr(pathlib.Path, "mkdir", racy_mkdir)

    run = allocate_run("acme", repo_root=tmp_path)

    assert triggered["n"] == 1, "the simulated race was never triggered - this test proves nothing"
    assert run.run_number == 2
    assert not (runs_root(tmp_path) / "001-acme" / "run.json").exists(), "the raced-away run must not be finalized"


def test_allocate_run_writes_a_readable_manifest(tmp_path: Path) -> None:
    run = allocate_run("acme", repo_root=tmp_path, extra_manifest={"scope": {"kind": "workbook"}})

    assert run.manifest_path.is_file()
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    assert manifest["run"] == 1
    assert manifest["unit_key"] == "acme"
    assert manifest["status"] == "active"
    assert "created" in manifest
    assert manifest["scope"] == {"kind": "workbook"}


def test_allocate_run_reserved_keys_win_over_a_colliding_extra_manifest(tmp_path: Path) -> None:
    """A caller passing extra_manifest={"run": 999} must never override the real run number -
    reserved identity fields are applied AFTER the caller's dict, not merged as equals."""
    run = allocate_run("acme", repo_root=tmp_path, extra_manifest={"run": 999, "status": "bogus"})
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    assert manifest["run"] == run.run_number == 1
    assert manifest["status"] == "active"


def test_subdir_rejects_a_noncanonical_name(tmp_path: Path) -> None:
    run = allocate_run("acme", repo_root=tmp_path)
    with pytest.raises(ValueError):
        run.subdir("not-a-real-subdir")


# --------------------------------------------------------------------------------------
# list_runs - read-back
# --------------------------------------------------------------------------------------


def test_list_runs_returns_manifests_sorted_by_run_number(tmp_path: Path) -> None:
    allocate_run("zzz-last-alphabetically", repo_root=tmp_path)
    allocate_run("aaa-first-alphabetically", repo_root=tmp_path)

    runs = list_runs(tmp_path)

    assert [r["run"] for r in runs] == [1, 2]
    assert runs[0]["unit_key"] == "zzz-last-alphabetically"


def test_list_runs_on_empty_or_missing_runs_root_is_empty_not_an_error(tmp_path: Path) -> None:
    assert not list_runs(tmp_path)  # `_runs/` was never created under this tmp_path

    root = runs_root(tmp_path)
    root.mkdir()
    assert not list_runs(tmp_path)  # exists but empty


def test_list_runs_skips_a_run_directory_with_no_manifest(tmp_path: Path) -> None:
    root = runs_root(tmp_path)
    root.mkdir(parents=True)
    (root / "001-half-written").mkdir()  # no run.json inside

    assert not list_runs(tmp_path)


# --------------------------------------------------------------------------------------
# runs_root - CWD independence
# --------------------------------------------------------------------------------------


def test_runs_root_is_not_cwd_dependent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The original bug this module fixes: a script resolved a relative path against the CWD, not
    the repo, and wrote a stray root at whatever directory an agent happened to invoke it from.
    `runs_root` must return the SAME path regardless of the process's current directory."""
    elsewhere = tmp_path / "some" / "unrelated" / "cwd"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)

    assert runs_root(tmp_path) == tmp_path / "_runs"
