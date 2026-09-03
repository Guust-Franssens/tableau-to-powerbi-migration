"""Regression tests for `scripts/work_dirs.py` - the canonical pre-bundle work-layout helper
(issue #291 gaps 1/3, implementing issue #234's corrected `_runs/<NNN>-<slug>/` design).

Every test uses `tmp_path` as `repo_root` so this suite never touches the real repo's `_runs/`.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from work_dirs import (
    CANONICAL_SUBDIRS,
    DERIVED_NAME_MATCHES,
    DERIVED_NAME_MISMATCH,
    DERIVED_NAME_NOT_CONSULTED,
    DERIVED_NAME_UNAVAILABLE,
    RUN_LOCATION_INTACT,
    RUN_LOCATION_KEY,
    RUN_LOCATION_MOVED,
    RUN_LOCATION_UNVERIFIABLE,
    allocate_run,
    check_run_location,
    list_runs,
    runs_root,
    sanitize_unit_key,
    verify_exit_code,
    verify_runs,
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
    assert run.packages == run.subdir("packages")
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


def test_packages_is_canonical_and_ordered_after_oracle_before_deliverables() -> None:
    """`packages/` names where `package_unit.py --out` writes (issue #446). Order is also display
    order in the CLI, so it is asserted rather than left to chance."""
    assert "packages" in CANONICAL_SUBDIRS
    assert CANONICAL_SUBDIRS.index("oracle") < CANONICAL_SUBDIRS.index("packages")
    assert CANONICAL_SUBDIRS.index("packages") < CANONICAL_SUBDIRS.index("deliverables")


def test_package_out_must_be_a_child_of_packages_not_packages_itself(tmp_path: Path) -> None:
    """The reason `docs/migration-phases.md` and `docs/operator-runbook.md` write
    `packages/<batch>/` and never a bare `packages/`.

    `package_unit.conflicting_evidence_dirs` refuses an `--out` that sits beside evidence the gates
    also scan, checking `<out>` and `<out>.parent` for `reference/`/`oracle/`/`_oracle/`. A run root
    ALWAYS holds `oracle/` (allocate_run creates every canonical subdir), so a bare
    `--out <run>/packages` is refused - measured, exit 2 with "sits beside evidence the gates also
    scan". One level deeper is accepted. This test is what keeps the documented command runnable.
    """
    from package_unit import conflicting_evidence_dirs  # local: keeps the import cost off collection

    run = allocate_run("acme", repo_root=tmp_path)

    assert run.oracle.is_dir(), "the premise failed: a run root must hold oracle/ for this to prove anything"
    assert conflicting_evidence_dirs(run.packages) == [run.oracle]
    assert conflicting_evidence_dirs(run.packages / "coldrun") == []


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


# --------------------------------------------------------------------------------------
# check_run_location - issue #470: a renumbered run must not read back as a healthy one
#
# Three MUTUALLY EXCLUSIVE states, and every test below asserts WHICH one was reported plus at
# least one negative control, because "it flagged something" is how a vacuous test survives:
#   intact       - recorded name == actual name
#   moved        - recorded name != actual name (renamed/renumbered since allocation)
#   unverifiable - nothing recorded; the question CANNOT be answered, which is not "fine"
# --------------------------------------------------------------------------------------


def _state_of(manifest: dict) -> str:
    return manifest["location_check"]["state"]


def _rename_run_dir(run_dir: Path, new_name: str) -> Path:
    """Do to a run exactly what the customer's reorg did: rename the directory in place."""
    moved = run_dir.parent / new_name
    run_dir.rename(moved)
    return moved


def test_allocate_run_records_the_directory_name_it_allocated(tmp_path: Path) -> None:
    """The whole detection story rests on this key existing; without it every run is unverifiable."""
    run = allocate_run("acme", repo_root=tmp_path)

    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    assert manifest[RUN_LOCATION_KEY] == "001-acme" == run.root.name


def test_allocated_dir_name_is_reserved_against_a_colliding_extra_manifest(tmp_path: Path) -> None:
    """A caller must not be able to plant a false self-path, which would forge an `intact` verdict."""
    run = allocate_run("acme", repo_root=tmp_path, extra_manifest={RUN_LOCATION_KEY: "999-somewhere-else"})

    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    assert manifest[RUN_LOCATION_KEY] == run.root.name
    assert _state_of(list_runs(tmp_path)[0]) == RUN_LOCATION_INTACT


def test_a_freshly_allocated_run_is_intact_and_is_not_moved_or_unverifiable(tmp_path: Path) -> None:
    """The positive control for `intact`, and the negative control for the other two states."""
    allocate_run("acme", repo_root=tmp_path)

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_INTACT
    assert check["state"] != RUN_LOCATION_MOVED  # negative control
    assert check["state"] != RUN_LOCATION_UNVERIFIABLE  # negative control
    assert check["recorded_dir_name"] == check["actual_dir_name"] == "001-acme"
    # the verdict must rest on the RECORD, never on the run/unit_key inference
    assert check["derived_name_check"] == DERIVED_NAME_NOT_CONSULTED


def test_a_renumbered_run_reports_moved_and_names_both_directories(tmp_path: Path) -> None:
    """The customer's actual failure: 14 run directories renumbered in one reorg (issue #470)."""
    run = allocate_run("acme", repo_root=tmp_path)
    _rename_run_dir(run.root, "042-acme")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_MOVED
    assert check["state"] != RUN_LOCATION_INTACT  # negative control: the defect being fixed
    assert check["state"] != RUN_LOCATION_UNVERIFIABLE  # negative control
    assert check["recorded_dir_name"] == "001-acme"
    assert check["actual_dir_name"] == "042-acme"


def test_a_run_renamed_but_keeping_its_number_also_reports_moved(tmp_path: Path) -> None:
    """The slug is decoration, but the recorded name is the whole name - a re-slugged run is still
    a renamed directory, and generated bundle output under it embeds the OLD absolute path."""
    run = allocate_run("acme", repo_root=tmp_path)
    _rename_run_dir(run.root, "001-acme-tidied-up")

    assert _state_of(list_runs(tmp_path)[0]) == RUN_LOCATION_MOVED


def test_a_legacy_manifest_with_no_recorded_name_is_unverifiable_never_intact(tmp_path: Path) -> None:
    """EVERY run allocated before this change lands here - including the live
    `_runs/408-coldrun2-fabric-migration-lab` on the machine that shipped it, measured at
    `state=unverifiable`, `derived_name_check=matches`, CLI exit 3.

    This is the highest-value assertion in the change: a legacy run whose derived name MATCHES is
    still `unverifiable`, because an inference is not a record. Letting it report `intact` would
    rebuild the exact defect issue #470 is about."""
    run = allocate_run("coldrun2-fabric-migration-lab", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    del manifest[RUN_LOCATION_KEY]  # exactly the shape of every pre-change run.json
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["state"] != RUN_LOCATION_INTACT  # negative control: must NOT collapse into clean
    assert check["state"] != RUN_LOCATION_MOVED  # negative control: nor into a false finding
    assert check["recorded_dir_name"] is None
    # the derived hint is reported, but it is subordinate and does not change the state
    assert check["derived_name_check"] == DERIVED_NAME_MATCHES
    assert "INFERENCE" in check["detail"]


def test_a_legacy_manifest_whose_derived_name_mismatches_is_still_only_unverifiable(tmp_path: Path) -> None:
    """A legacy run that LOOKS renumbered (run=1 but sitting at 042-) gets a `mismatch` hint - and
    still only `unverifiable`, because `run`/`unit_key` reconstruct a name via a padding rule that
    could change, which is not the same as having recorded one."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    del manifest[RUN_LOCATION_KEY]
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _rename_run_dir(run.root, "042-acme")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["state"] != RUN_LOCATION_MOVED  # negative control: an inference is not a finding
    assert check["derived_name_check"] == DERIVED_NAME_MISMATCH
    assert "042-acme" in check["detail"] and "001-acme" in check["detail"]


def test_a_manifest_with_neither_a_recorded_nor_a_derivable_name_reports_unavailable(tmp_path: Path) -> None:
    """A hand-written `run.json` carries no `run`/`unit_key` either, so even the hint is unavailable."""
    root = runs_root(tmp_path)
    root.mkdir(parents=True)
    hand_made = root / "003-by-hand"
    hand_made.mkdir()
    (hand_made / "run.json").write_text(json.dumps({"note": "written by an agent, not allocate_run"}), encoding="utf-8")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["derived_name_check"] == DERIVED_NAME_UNAVAILABLE


@pytest.mark.parametrize("planted", [42, ["001-acme"], {"name": "001-acme"}, ""])
def test_an_unusable_recorded_name_is_unverifiable_never_intact_and_never_crashes(
    tmp_path: Path, planted: object
) -> None:
    """A hand-edited or corrupted `allocated_dir_name` must not be compared as if it were a name."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest[RUN_LOCATION_KEY] = planted
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["state"] != RUN_LOCATION_INTACT  # negative control


def test_a_relocated_repository_is_still_intact_not_moved(tmp_path: Path) -> None:
    """The brittleness trap this design exists to avoid: a repo that is cloned, moved, or checked
    out as a `git worktree` is legitimately somewhere else, and its runs are NOT corrupt.

    Recording an ABSOLUTE path would report all of them `moved` here, and a check that fires on
    every clone is one people learn to ignore. Recording the directory NAME does not."""
    original_repo = tmp_path / "repo-here"
    relocated_repo = tmp_path / "some" / "deeper" / "repo-there"
    relocated_repo.parent.mkdir(parents=True)
    allocate_run("acme", repo_root=original_repo)
    allocate_run("beta", repo_root=original_repo)

    shutil.copytree(original_repo, relocated_repo)

    states = [_state_of(run) for run in list_runs(relocated_repo)]
    assert states == [RUN_LOCATION_INTACT, RUN_LOCATION_INTACT]
    # and the absolute paths really did change, or this test proves nothing
    roots = [run["root"] for run in list_runs(relocated_repo)]
    assert all(str(relocated_repo) in root for root in roots)


def test_check_run_location_is_pure_and_needs_no_directory_on_disk(tmp_path: Path) -> None:
    """`list_runs` is documented as pure inspection, so its helper must not mutate or touch disk."""
    manifest = {"run": 7, "unit_key": "acme", RUN_LOCATION_KEY: "007-acme"}
    before = json.dumps(manifest, sort_keys=True)
    nonexistent = tmp_path / "no" / "such" / "007-acme"

    check = check_run_location(manifest, nonexistent)

    assert check.state == RUN_LOCATION_INTACT
    assert check.is_intact
    assert json.dumps(manifest, sort_keys=True) == before, "check_run_location mutated the manifest"
    assert not nonexistent.exists(), "check_run_location touched the filesystem"


# --------------------------------------------------------------------------------------
# list_runs - the "never raises" contract, and derived-vs-recorded precedence
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["[]", '"a string"', "42", "null", "not json at all", ""])
def test_list_runs_never_raises_on_a_run_json_that_is_not_a_manifest(tmp_path: Path, payload: str) -> None:
    """`list_runs` promises it "never raises on a hand-created or half-written run directory". Valid
    JSON that is not an object used to break that promise: `.setdefault` on a list raises
    `AttributeError`, which escaped the `json.JSONDecodeError`/`OSError` guard entirely."""
    root = runs_root(tmp_path)
    root.mkdir(parents=True)
    (root / "001-half-written").mkdir()
    (root / "001-half-written" / "run.json").write_text(payload, encoding="utf-8")

    runs = list_runs(tmp_path)

    assert isinstance(runs, list) and not runs


def test_list_runs_reports_the_actual_location_not_a_stale_recorded_one(tmp_path: Path) -> None:
    """`root` is DERIVED - it must describe where the run is now. A stale `root` planted in the
    manifest must not shadow it, or a status query silently reports a location that is not real."""
    run = allocate_run("acme", repo_root=tmp_path, extra_manifest={"root": r"D:\somewhere\that\never\existed"})

    listed = list_runs(tmp_path)[0]

    assert listed["root"] == str(run.root)


def test_list_runs_attaches_a_location_check_to_every_run(tmp_path: Path) -> None:
    """The state must be reachable from what a caller actually consumes, not only from the helper."""
    allocate_run("acme", repo_root=tmp_path)
    allocate_run("beta", repo_root=tmp_path)

    for listed in list_runs(tmp_path):
        assert listed["location_check"]["state"] in {
            RUN_LOCATION_INTACT,
            RUN_LOCATION_MOVED,
            RUN_LOCATION_UNVERIFIABLE,
        }


# --------------------------------------------------------------------------------------
# verify_runs / verify_exit_code / the --verify CLI
# --------------------------------------------------------------------------------------


def test_verify_runs_counts_each_state_separately(tmp_path: Path) -> None:
    """A mixed tree must not fold three states into two - the counts drive the exit code."""
    allocate_run("intact-one", repo_root=tmp_path)
    to_move = allocate_run("moved-one", repo_root=tmp_path)
    legacy = allocate_run("legacy-one", repo_root=tmp_path)
    _rename_run_dir(to_move.root, "099-moved-one")
    manifest = json.loads(legacy.manifest_path.read_text(encoding="utf-8"))
    del manifest[RUN_LOCATION_KEY]
    legacy.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _runs, counts = verify_runs(tmp_path)

    assert counts == {RUN_LOCATION_INTACT: 1, RUN_LOCATION_MOVED: 1, RUN_LOCATION_UNVERIFIABLE: 1}


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({RUN_LOCATION_INTACT: 3, RUN_LOCATION_MOVED: 0, RUN_LOCATION_UNVERIFIABLE: 0}, 0),
        ({RUN_LOCATION_INTACT: 0, RUN_LOCATION_MOVED: 0, RUN_LOCATION_UNVERIFIABLE: 0}, 0),
        ({RUN_LOCATION_INTACT: 1, RUN_LOCATION_MOVED: 1, RUN_LOCATION_UNVERIFIABLE: 0}, 1),
        ({RUN_LOCATION_INTACT: 1, RUN_LOCATION_MOVED: 0, RUN_LOCATION_UNVERIFIABLE: 1}, 3),
        # an established finding outranks an unanswered question, but NEITHER is a pass
        ({RUN_LOCATION_INTACT: 0, RUN_LOCATION_MOVED: 1, RUN_LOCATION_UNVERIFIABLE: 9}, 1),
    ],
)
def test_verify_exit_code_gates_on_state_never_on_printed_text(counts: dict, expected: int) -> None:
    """0 clean / 1 findings / 3 cannot-establish, matching `check_reference_readiness.py`."""
    assert verify_exit_code(counts) == expected


def _run_verify_cli(repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "work_dirs.py"), "--verify", "--repo-root", str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_verify_cli_exits_0_for_an_all_intact_tree(tmp_path: Path) -> None:
    """Reaches the real CLI in a subprocess, so argparse and `main()` are covered, not just helpers."""
    allocate_run("acme", repo_root=tmp_path)

    result = _run_verify_cli(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "INTACT" in result.stdout


def test_verify_cli_exits_1_on_a_renumbered_run(tmp_path: Path) -> None:
    """Exit 1 is a FINDING, and must be distinguishable from the exit 3 a legacy run produces."""
    run = allocate_run("acme", repo_root=tmp_path)
    _rename_run_dir(run.root, "042-acme")

    result = _run_verify_cli(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MOVED" in result.stdout
    assert "001-acme" in result.stdout and "042-acme" in result.stdout


def test_verify_cli_exits_3_on_a_legacy_run_and_says_it_is_not_intact(tmp_path: Path) -> None:
    """Reproduces the live `_runs/` tree on the machine that shipped this change: three legacy runs,
    all `unverifiable`, CLI exit 3. Exit 3 is `check_reference_readiness.py`'s CANNOT_ESTABLISH -
    not a pass, and distinguishable from the exit 1 a real finding produces."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    del manifest[RUN_LOCATION_KEY]
    run.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_verify_cli(tmp_path)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "UNVERIFIABLE" in result.stdout
    assert "is NOT 'intact'" in result.stdout


def test_verify_cli_needs_no_unit_argument_but_allocation_still_does(tmp_path: Path) -> None:
    """`--verify` allocates nothing, so it must not require a unit - and dropping the unit without
    `--verify` must still be an argparse error rather than a silent no-op allocation."""
    assert _run_verify_cli(tmp_path).returncode == 0
    assert not runs_root(tmp_path).exists(), "--verify must never allocate or create anything"

    bare = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "work_dirs.py"), "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bare.returncode == 2
    assert "--verify" in bare.stderr


# --------------------------------------------------------------------------------------
# The prevention half - the prohibition must live in the artifact an agent actually reads
# --------------------------------------------------------------------------------------


def test_the_rename_prohibition_and_its_reason_are_stated_in_work_dirs_itself(tmp_path: Path) -> None:
    """Issue #470's first gap: the rule lived only in `docs/`, so an agent reading this module
    pattern-matched the naming semantics and renumbered 14 run directories. `tmp_path` is unused;
    this asserts on docstrings, which are the artifact that failed."""
    del tmp_path
    import work_dirs  # pylint: disable=import-outside-toplevel

    module_doc = work_dirs.__doc__ or ""
    allocate_doc = allocate_run.__doc__ or ""

    for phrase in ("never renamed", "absolute self-paths"):
        assert phrase in module_doc.lower(), f"the module docstring no longer states {phrase!r}"
    assert "never be renamed" in allocate_doc.lower()
    assert "absolute self-paths" in allocate_doc.lower()
    # the granularity their agent got wrong: one run per pipeline run, not one per workbook
    assert "not one per workbook" in module_doc.lower()
    assert "not one per workbook" in allocate_doc.lower()
