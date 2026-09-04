"""Regression tests for `scripts/work_dirs.py` - the canonical pre-bundle work-layout helper
(issue #291 gaps 1/3, implementing issue #234's corrected `_runs/<NNN>-<slug>/` design).

Every test uses `tmp_path` as `repo_root` so this suite never touches the real repo's `_runs/`.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import threading
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
    LAZY_SUBDIRS,
    PATH_CHECK_DIFFERS,
    PATH_CHECK_MATCHES,
    PATH_CHECK_UNRECORDED,
    RUN_LOCATION_INTACT,
    RUN_LOCATION_DUPLICATE,
    RUN_LOCATION_KEY,
    RUN_LOCATION_MOVED,
    RUN_LOCATION_UNVERIFIABLE,
    RUN_PATH_KEY,
    _is_location_independent,  # the one private helper a test reaches for - see its test below
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
    # The six-character reduction is the tested budget for the measured 265 -> 259 path shape.
    assert len(got) <= 54
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
        if name in LAZY_SUBDIRS:
            continue  # issue #481 - proven not to exist yet in a dedicated test below
        assert run.subdir(name).is_dir(), f"canonical subdir {name!r} was not created"
    # every accessor property must agree with subdir()
    assert run.assessment == run.subdir("assessment")
    assert run.assets == run.subdir("assets")
    assert run.bundle == run.subdir("bundle")
    assert run.oracle == run.subdir("oracle")
    assert run.packages == run.subdir("packages")
    assert run.deliverables == run.subdir("deliverables")
    assert run.scratch == run.subdir("scratch")


def test_allocate_run_does_not_eagerly_create_deliverables(tmp_path: Path) -> None:
    """issue #481: `deliverables/` has no writer anywhere in the repo, so an eagerly-created copy
    is an always-empty folder that reads to an operator as a failed or skipped step. It must not
    exist on disk right after allocation, even though `subdir("deliverables")` still resolves a
    path for it (the concept - operator-facing, customer-bound output, separate from `scratch/` -
    is preserved; only the eager, empty folder is not).

    Self-validating (PR review, tip 3145dfc7): asserts a sibling canonical subdir DOES exist in the
    same breath, so a mutation that disables ALL eager subdir creation (not just `deliverables/`)
    cannot leave this test green while everything else fails - it must fail on its own premise.
    """
    run = allocate_run("acme", repo_root=tmp_path)

    assert "deliverables" in CANONICAL_SUBDIRS, "the concept must survive - see issue #322"
    assert run.subdir("scratch").is_dir(), "premise failed: a sibling canonical subdir must exist"
    assert not run.subdir("deliverables").exists(), "deliverables/ must not be created eagerly"
    assert not (run.root / "deliverables").exists()


def test_deliverables_property_creates_the_directory_lazily_on_first_access(tmp_path: Path) -> None:
    """The counterpart to the test above: the directory must still be reachable and writable the
    moment something actually needs it - `allocate_run` just no longer creates it up front."""
    run = allocate_run("acme", repo_root=tmp_path)
    assert not run.subdir("deliverables").exists()

    path = run.deliverables

    assert path == run.subdir("deliverables")
    assert path.is_dir()
    (path / "connections.md").write_text("customer server names go here\n", encoding="utf-8")
    assert (path / "connections.md").is_file()


def test_deliverables_property_access_is_idempotent(tmp_path: Path) -> None:
    """PR review, tip 3145dfc7: a mutation flipping the property's `mkdir(exist_ok=True)` to
    `exist_ok=False` left the full suite green, because nothing exercised a SECOND access. Repeated
    access (e.g. two separate scripts writing to `run.deliverables` in the same run) must never
    raise."""
    run = allocate_run("acme", repo_root=tmp_path)

    first = run.deliverables
    second = run.deliverables  # must not raise FileExistsError

    assert first == second
    assert second.is_dir()


def test_deliverables_property_never_clobbers_existing_customer_output(tmp_path: Path) -> None:
    """PR review, tip 3145dfc7: a mutation that deleted and recreated `deliverables/` on every
    property access left the full suite green, because nothing asserted survival of prior content.
    `deliverables/` holds operator-facing CUSTOMER output (issue #322) - a second access (from a
    second script, or the same script re-reading `run.deliverables`) must never wipe it."""
    run = allocate_run("acme", repo_root=tmp_path)
    written = run.deliverables / "connections.md"
    written.write_text("acme-prod-sql-01.internal\n", encoding="utf-8")

    accessed_again = run.deliverables

    assert accessed_again == written.parent
    assert written.is_file(), "an existing populated deliverables/ must survive re-access"
    assert written.read_text(encoding="utf-8") == "acme-prod-sql-01.internal\n"


def test_other_six_canonical_subdirs_are_unaffected_by_the_lazy_deliverables_change(tmp_path: Path) -> None:
    """Negative control for issue #481: every canonical subdir OTHER than `deliverables/` must
    still be created eagerly by `allocate_run`, exactly as before."""
    run = allocate_run("acme", repo_root=tmp_path)

    eager = [name for name in CANONICAL_SUBDIRS if name not in LAZY_SUBDIRS]
    assert set(eager) == set(CANONICAL_SUBDIRS) - {"deliverables"}
    for name in eager:
        assert run.subdir(name).is_dir(), f"canonical subdir {name!r} must still be created eagerly"


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


def test_allocate_run_does_not_reuse_a_deleted_run_number(tmp_path: Path) -> None:
    first = allocate_run("acme", repo_root=tmp_path)
    shutil.rmtree(first.root)

    second = allocate_run("beta", repo_root=tmp_path)

    assert second.run_number == 2


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
        if self.name == "001" and triggered["n"] == 0:
            triggered["n"] += 1
            original_mkdir(self, parents=True, exist_ok=True)  # another process "wins" the race
        original_mkdir(self, *args, **kwargs)  # real semantics decide whether THIS call raises

    monkeypatch.setattr(pathlib.Path, "mkdir", racy_mkdir)

    run = allocate_run("acme", repo_root=tmp_path)

    assert triggered["n"] == 1, "the simulated race was never triggered - this test proves nothing"
    assert run.run_number == 2
    assert not (runs_root(tmp_path) / "001-acme" / "run.json").exists(), "the raced-away run must not be finalized"


def test_allocate_run_reserves_numbers_atomically_across_distinct_slugs(tmp_path: Path) -> None:
    """issue #513: the full `<NNN>-<slug>` mkdir let simultaneous distinct slugs reuse one number.
    The barrier makes every worker race for its first candidate; without it this is only serial allocation."""
    names = [f"unit-{index}" for index in range(6)]
    barrier = threading.Barrier(len(names))
    runs: list = []
    failures: list[BaseException] = []

    def allocate(name: str) -> None:
        try:
            barrier.wait()
            runs.append(allocate_run(name, repo_root=tmp_path))
        except BaseException as exc:  # pragma: no cover - propagated below with the original exception
            failures.append(exc)

    threads = [threading.Thread(target=allocate, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    numbers = [run.run_number for run in runs]
    assert len(runs) == len(names), "the barrier-synchronised workers did not all allocate"
    assert len(set(numbers)) == len(numbers), "distinct slugs must never reuse a run number"


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
    # ...but `verify_runs` is a GATE and must still see it - see the block at the end of this file.
    assert verify_runs(tmp_path)[1][RUN_LOCATION_UNVERIFIABLE] == 1


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
    # The inference is now CONSULTED on the healthy path - `run`/`unit_key` must reconstruct the
    # directory the manifest sits in (finding 3), so a contradictory number cannot read as healthy.
    # It can still only ever DEMOTE: `test_a_legacy_manifest_with_no_recorded_name_is_unverifiable_
    # never_intact` is the negative control that a matching inference promotes nothing.
    assert check["derived_name_check"] == DERIVED_NAME_MATCHES


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


def test_a_copied_run_is_unverifiable_never_intact_and_the_original_is_untouched(tmp_path: Path) -> None:
    """Finding 2's reproduction, and the negative control is the whole point: the copy moves, the
    original does not, so exactly ONE side of the relationship changes.

    ⚠️ This test previously asserted the OPPOSITE - that a `shutil.copytree`d tree is still
    `INTACT` - on the premise that "a repo that is cloned, moved, or checked out as a `git worktree`
    is legitimately somewhere else". That premise is false and checkable in one command: `_runs/` is
    git-ignored (`git check-ignore -v -- _runs/001-acme/run.json` -> `.gitignore:162:/_*`), so a
    clone and a fresh worktree carry no runs at all. What the old test actually fixtured was a COPY
    of a run into a second location, which is corruption, and it asserted the fail-open verdict."""
    original_repo = tmp_path / "repo-here"
    copied_repo = tmp_path / "some" / "deeper" / "repo-there"
    copied_repo.parent.mkdir(parents=True)
    allocate_run("acme", repo_root=original_repo)
    allocate_run("beta", repo_root=original_repo)

    shutil.copytree(original_repo, copied_repo)

    copied = list_runs(copied_repo)
    assert [_state_of(run) for run in copied] == [RUN_LOCATION_UNVERIFIABLE, RUN_LOCATION_UNVERIFIABLE]
    assert all(run["location_check"]["path_check"] == PATH_CHECK_DIFFERS for run in copied)
    # not `moved` either: a whole-checkout move and a run-only copy are indistinguishable from here
    assert all(_state_of(run) != RUN_LOCATION_MOVED for run in copied)
    # the ORIGINAL is the negative control - the copy moved, it did not
    assert [_state_of(run) for run in list_runs(original_repo)] == [RUN_LOCATION_INTACT, RUN_LOCATION_INTACT]
    # and the absolute paths really did change, or this test proves nothing
    assert all(str(copied_repo) in run["root"] for run in copied)


def test_a_run_moved_alone_to_a_second_runs_root_is_unverifiable_never_intact(tmp_path: Path) -> None:
    """The reviewer's exact reproduction: allocate under `source/`, move that ONE run to
    `moved/_runs/`, and the destination used to report `INTACT 001-acme` exit 0 because the
    basename never changed. A run-only move breaks every absolute self-path embedded under
    `<run>/bundle/`."""
    source = tmp_path / "source"
    destination = tmp_path / "moved"
    runs_root(destination).mkdir(parents=True)
    run = allocate_run("acme", repo_root=source)

    shutil.move(str(run.root), str(runs_root(destination) / "001-acme"))

    check = list_runs(destination)[0]["location_check"]
    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["state"] != RUN_LOCATION_INTACT  # negative control: the defect being fixed
    assert check["path_check"] == PATH_CHECK_DIFFERS
    assert str(source) in check["detail"] and str(destination) in check["detail"]


def test_a_run_still_where_it_was_allocated_reports_path_check_matches(tmp_path: Path) -> None:
    """The positive control for the path half: `intact` must rest on a RECORDED path comparison,
    not on the name having matched."""
    allocate_run("acme", repo_root=tmp_path)

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_INTACT
    assert check["path_check"] == PATH_CHECK_MATCHES


def test_allocate_run_records_the_absolute_path_it_allocated_and_reserves_the_key(tmp_path: Path) -> None:
    """Without a recorded absolute path there is nothing to compare, and a caller must not be able
    to plant a false one - that would forge an `intact` verdict for a moved run."""
    run = allocate_run("acme", repo_root=tmp_path, extra_manifest={RUN_PATH_KEY: r"D:\somewhere\else\001-acme"})

    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    assert Path(manifest[RUN_PATH_KEY]).is_absolute()
    assert Path(manifest[RUN_PATH_KEY]).samefile(run.root)
    assert _state_of(list_runs(tmp_path)[0]) == RUN_LOCATION_INTACT


def test_a_run_with_no_recorded_absolute_path_is_unverifiable_never_intact(tmp_path: Path) -> None:
    """A manifest carrying only the NAME cannot establish `intact`, because a basename is unchanged
    by both a run-only move and a copy."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    del manifest[RUN_PATH_KEY]
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["state"] != RUN_LOCATION_INTACT  # negative control
    assert check["path_check"] == PATH_CHECK_UNRECORDED


@pytest.mark.parametrize("planted", [42, ["a"], {"p": "x"}, "", None])
def test_an_unusable_recorded_absolute_path_is_unverifiable_never_intact(tmp_path: Path, planted: object) -> None:
    """A hand-edited or corrupted `allocated_abs_path` must not be compared as if it were a path."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest[RUN_PATH_KEY] = planted
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["state"] != RUN_LOCATION_INTACT  # negative control


def test_check_run_location_is_pure_and_needs_no_directory_on_disk(tmp_path: Path) -> None:
    """`list_runs` is documented as pure inspection, so its helper must not mutate or touch disk.

    The path comparison uses `abspath`, never `resolve`, for exactly this reason: it normalizes
    separators, `..` and case without a filesystem read."""
    nonexistent = tmp_path / "no" / "such" / "007-acme"
    manifest = {
        "run": 7,
        "unit_key": "acme",
        RUN_LOCATION_KEY: "007-acme",
        RUN_PATH_KEY: str(nonexistent),
    }
    before = json.dumps(manifest, sort_keys=True)

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
    `AttributeError`, which escaped the `json.JSONDecodeError`/`OSError` guard entirely.

    ⚠️ This test used to assert the run was OMITTED, full stop - and `verify_runs` was built on
    `list_runs`, so that omission WAS the fail-open gate (finding 1). The never-raises contract is
    the legitimate half and is kept; the visibility half now belongs to
    `test_verify_runs_never_drops_a_run_directory_it_cannot_assess`, which asserts the same
    directory is `unverifiable` rather than invisible. Inspection may skip; a gate may not."""
    root = runs_root(tmp_path)
    root.mkdir(parents=True)
    (root / "001-half-written").mkdir()
    (root / "001-half-written" / "run.json").write_text(payload, encoding="utf-8")

    runs = list_runs(tmp_path)

    assert isinstance(runs, list) and not runs  # inspection skips it - and never raises
    # ...but the GATE must still see it. This pairing is the fix; asserting only the line above is
    # what let a half-written run count as a clean tree.
    _gate_runs, counts = verify_runs(tmp_path)
    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1


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


_UNASSESSABLE_SHAPES = {
    "missing": None,
    "empty": "",
    "truncated": '{"run": 1, "unit_key": "acme"',
    "malformed": "not json at all",
    "non_object_list": '["001-acme"]',
    "non_object_scalar": "42",
}


def _plant_unassessable_run(tmp_path: Path, shape: str, dir_name: str = "001-acme") -> Path:
    """A canonical `_runs/<NNN>-<slug>/` directory whose manifest cannot be assessed. `missing` is
    not an exotic input: it is exactly what an allocation interrupted between `mkdir` and the
    manifest write leaves behind."""
    run_dir = runs_root(tmp_path) / dir_name
    run_dir.mkdir(parents=True)
    payload = _UNASSESSABLE_SHAPES[shape]
    if payload is not None:
        (run_dir / "run.json").write_text(payload, encoding="utf-8")
    return run_dir


@pytest.mark.parametrize("shape", sorted(_UNASSESSABLE_SHAPES))
def test_verify_runs_never_drops_a_run_directory_it_cannot_assess(tmp_path: Path, shape: str) -> None:
    """Finding 1. `verify_runs` used to be built on `list_runs`, so it inherited inspection's right
    to skip: a run directory that exists but cannot be read contributed ZERO to every bucket, and
    the CLI printed `0 run(s): 0 intact, 0 moved, 0 unverifiable`, exit 0 - the `unverifiable`
    bucket sitting at 0 exactly when something was unverifiable."""
    _plant_unassessable_run(tmp_path, shape)

    runs, counts = verify_runs(tmp_path)

    assert len(runs) == 1, "the gate dropped a run directory it could not assess"
    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1
    assert counts[RUN_LOCATION_INTACT] == 0  # negative control: it must not land in the clean bucket
    assert counts[RUN_LOCATION_MOVED] == 0  # negative control: nor be reported as an established finding
    assert verify_exit_code(counts) == 3


@pytest.mark.parametrize("shape", sorted(_UNASSESSABLE_SHAPES))
def test_the_gate_sees_exactly_the_run_inspection_omits(tmp_path: Path, shape: str) -> None:
    """The relationship the split exists to create, with only ONE side moving: a healthy run is in
    both, an unassessable one is in the gate only. Asserting `len(verify) > len(list)` alone would
    pass on a gate that invented an entry, so both halves are pinned."""
    allocate_run("healthy", repo_root=tmp_path)
    _plant_unassessable_run(tmp_path, shape, dir_name="002-half-written")

    inspected = list_runs(tmp_path)
    gated, counts = verify_runs(tmp_path)

    assert [run["root"] for run in inspected] == [str(runs_root(tmp_path) / "001-healthy")]
    assert [Path(run["root"]).name for run in gated] == ["001-healthy", "002-half-written"]
    assert counts == {
        RUN_LOCATION_INTACT: 1,
        RUN_LOCATION_MOVED: 0,
        RUN_LOCATION_DUPLICATE: 0,
        RUN_LOCATION_UNVERIFIABLE: 1,
    }


def test_a_run_json_that_cannot_be_read_at_all_is_unverifiable_not_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's sixth shape: a manifest held open with Windows `FileShare.None`, i.e. a read
    that raises `OSError`. Forced here through `Path.read_text` so the branch is exercised on any
    platform rather than only where an exclusive share mode exists."""
    run = allocate_run("acme", repo_root=tmp_path)
    real_read_text = Path.read_text

    def _refuse(self: Path, *args: object, **kwargs: object) -> str:
        if self == run.manifest_path:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _refuse)

    _runs, counts = verify_runs(tmp_path)

    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1
    assert counts[RUN_LOCATION_INTACT] == 0  # negative control
    assert verify_exit_code(counts) == 3


def test_a_mixed_run_number_tree_does_not_raise_and_keeps_the_invalid_manifest(tmp_path: Path) -> None:
    """Finding 3. `manifests.sort(key=lambda m: m.get("run", 0))` sorted an unvalidated value, so a
    tree holding both `{"run": 1}` and `{"run": "two"}` raised `TypeError: '<' not supported between
    instances of 'str' and 'int'` - breaking the documented never-raises contract AND preventing any
    `unverifiable` verdict from ever being reached."""
    root = runs_root(tmp_path)
    for name, run_number in (("001-a", 1), ("002-b", "two"), ("003-c", 3)):
        (root / name).mkdir(parents=True)
        (root / name / "run.json").write_text(
            json.dumps({"run": run_number, "unit_key": name[4:], RUN_LOCATION_KEY: name}), encoding="utf-8"
        )

    runs, counts = verify_runs(tmp_path)  # must not raise

    assert [Path(run["root"]).name for run in runs] == ["001-a", "003-c", "002-b"]
    assert counts[RUN_LOCATION_UNVERIFIABLE] == 3  # none of the three recorded an absolute path either
    assert any("'two'" in run["location_check"]["detail"] for run in runs), "the invalid manifest was not retained"
    assert not any(_state_of(run) == RUN_LOCATION_INTACT for run in runs)  # negative control


def test_a_verified_tree_of_freshly_allocated_runs_is_still_clean(tmp_path: Path) -> None:
    """The positive control for the whole gate: making "cannot assess" blocking must not make a
    healthy tree blocking too, or the gate is just an always-red light."""
    allocate_run("acme", repo_root=tmp_path)
    allocate_run("beta", repo_root=tmp_path)

    _runs, counts = verify_runs(tmp_path)

    assert counts == {
        RUN_LOCATION_INTACT: 2,
        RUN_LOCATION_MOVED: 0,
        RUN_LOCATION_DUPLICATE: 0,
        RUN_LOCATION_UNVERIFIABLE: 0,
    }
    assert verify_exit_code(counts) == 0


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

    assert counts == {
        RUN_LOCATION_INTACT: 1,
        RUN_LOCATION_MOVED: 1,
        RUN_LOCATION_DUPLICATE: 0,
        RUN_LOCATION_UNVERIFIABLE: 1,
    }


def test_verify_runs_reports_duplicate_run_numbers_as_non_clean(tmp_path: Path) -> None:
    first = allocate_run("acme", repo_root=tmp_path)
    duplicate = runs_root(tmp_path) / "001-beta"
    shutil.copytree(first.root, duplicate)
    manifest = json.loads((duplicate / "run.json").read_text(encoding="utf-8"))
    manifest.update({"unit_key": "beta", RUN_LOCATION_KEY: duplicate.name, RUN_PATH_KEY: str(duplicate)})
    (duplicate / "run.json").write_text(json.dumps(manifest), encoding="utf-8")

    runs, counts = verify_runs(tmp_path)

    assert [run["location_check"]["state"] for run in runs] == [RUN_LOCATION_DUPLICATE] * 2
    assert counts == {
        RUN_LOCATION_INTACT: 0,
        RUN_LOCATION_MOVED: 0,
        RUN_LOCATION_DUPLICATE: 2,
        RUN_LOCATION_UNVERIFIABLE: 0,
    }
    assert verify_exit_code(counts) == 2

    result = _run_verify_cli(tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "DUPLICATE" in result.stdout


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({RUN_LOCATION_INTACT: 3, RUN_LOCATION_MOVED: 0, RUN_LOCATION_DUPLICATE: 0, RUN_LOCATION_UNVERIFIABLE: 0}, 0),
        ({RUN_LOCATION_INTACT: 0, RUN_LOCATION_MOVED: 0, RUN_LOCATION_DUPLICATE: 0, RUN_LOCATION_UNVERIFIABLE: 0}, 0),
        ({RUN_LOCATION_INTACT: 1, RUN_LOCATION_MOVED: 1, RUN_LOCATION_DUPLICATE: 0, RUN_LOCATION_UNVERIFIABLE: 0}, 1),
        ({RUN_LOCATION_INTACT: 1, RUN_LOCATION_MOVED: 0, RUN_LOCATION_DUPLICATE: 1, RUN_LOCATION_UNVERIFIABLE: 0}, 2),
        ({RUN_LOCATION_INTACT: 1, RUN_LOCATION_MOVED: 0, RUN_LOCATION_DUPLICATE: 0, RUN_LOCATION_UNVERIFIABLE: 1}, 3),
        # an established finding outranks an unanswered question, but NEITHER is a pass
        ({RUN_LOCATION_INTACT: 0, RUN_LOCATION_MOVED: 1, RUN_LOCATION_DUPLICATE: 9, RUN_LOCATION_UNVERIFIABLE: 9}, 1),
    ],
)
def test_verify_exit_code_gates_on_state_never_on_printed_text(counts: dict, expected: int) -> None:
    """0 clean / 1 findings / 3 cannot-establish, matching `check_reference_readiness.py`."""
    assert verify_exit_code(counts) == expected


def _run_verify_cli(repo_root: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "work_dirs.py"), "--verify", "--repo-root", str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
        cwd=None if cwd is None else str(cwd),
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


def test_verify_cli_exits_3_on_a_run_directory_it_cannot_assess(tmp_path: Path) -> None:
    """Finding 1 through the REAL CLI, which is where it was reproduced: a canonical
    `_runs/001-acme/` with no `run.json` printed `0 run(s): 0 intact, 0 moved, 0 unverifiable`,
    exit 0. Judged by exit code, never by the printed text."""
    _plant_unassessable_run(tmp_path, "missing")

    result = _run_verify_cli(tmp_path)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "UNVERIFIABLE" in result.stdout
    assert "0 intact, 0 moved, 0 duplicate, 1 unverifiable" in result.stdout
    assert "1 run(s)" in result.stdout, "the run directory was counted as nothing at all"


def test_verify_cli_exits_3_on_a_run_moved_alone_to_another_root(tmp_path: Path) -> None:
    """Finding 2 through the real CLI. It reported `INTACT 001-acme`, exit 0."""
    source = tmp_path / "source"
    destination = tmp_path / "moved"
    runs_root(destination).mkdir(parents=True)
    run = allocate_run("acme", repo_root=source)
    shutil.move(str(run.root), str(runs_root(destination) / "001-acme"))

    result = _run_verify_cli(destination)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "UNVERIFIABLE" in result.stdout
    assert "INTACT" not in result.stdout  # negative control: the defect being fixed


def test_verify_cli_does_not_crash_on_a_mixed_run_number_tree(tmp_path: Path) -> None:
    """Finding 3 through the real CLI: it exited 1 with an uncaught `TypeError` traceback, which is
    indistinguishable from a legitimate `moved` finding by exit code alone."""
    root = runs_root(tmp_path)
    for name, run_number in (("001-a", 1), ("002-b", "two")):
        (root / name).mkdir(parents=True)
        (root / name / "run.json").write_text(json.dumps({"run": run_number, RUN_LOCATION_KEY: name}), encoding="utf-8")

    result = _run_verify_cli(tmp_path)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "TypeError" not in result.stderr


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


# --------------------------------------------------------------------------------------
# PR #477, review round 2 - the SAME fail-open class at three more sites, and the residual
#
# Round 1 fixed `list_runs` being the gate's only input. Round 2 found the identical defect in the
# path comparison, in directory discovery and in run-number validation - the signature of a class
# narrowed one site at a time. The fix collapses discovery and classification onto ONE path each;
# these tests pin the reproductions AND the collapse, because a passing behaviour test would not
# stop the next guard being bolted on beside them.
# --------------------------------------------------------------------------------------


def _work_dirs_source() -> str:
    return (REPO_ROOT / "scripts" / "work_dirs.py").read_text(encoding="utf-8")


# --- Finding 1: a relative "absolute" path forges `intact` from the verifier's own CWD ---


def test_a_relative_recorded_path_is_unverifiable_not_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`allocated_abs_path` is REQUIRED to be absolute, but the comparison ran it through
    `abspath()`, which completes a relative value against the verifier's current directory. Real
    CLI, measured: recording `_review477_round2_attacks\\relative-path\\_runs\\001-acme` and
    checking from the matching directory printed `INTACT 001-acme`, `1 intact`, exit 0.

    The verdict therefore depended on where the operator was standing - in the one module that
    exists to be CWD-independent (`REPO_ROOT` is resolved from `__file__`, never `Path.cwd()`)."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest[RUN_PATH_KEY] = str(Path("_runs") / "001-acme")  # resolves to the run dir from tmp_path
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # stand exactly where the forged value resolves

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["state"] != RUN_LOCATION_INTACT  # negative control: the defect being fixed
    assert check["path_check"] == PATH_CHECK_UNRECORDED
    assert "not an absolute path" in check["detail"]


def test_verify_cli_exits_3_on_a_relative_recorded_path_even_from_the_matching_cwd(tmp_path: Path) -> None:
    """The reviewer's reproduction end to end, judged by exit code: the CLI is run FROM the
    directory the forged relative value resolves against, which is the only place it could ever
    have looked healthy."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest[RUN_PATH_KEY] = str(Path("_runs") / "001-acme")
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = _run_verify_cli(tmp_path, cwd=tmp_path)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "UNVERIFIABLE" in result.stdout
    assert "INTACT" not in result.stdout
    assert "1 unverifiable" in result.stdout


@pytest.mark.parametrize(
    ("value", "independent"),
    [
        # accepted: these name one place wherever the process is standing
        (str(Path.cwd().resolve()), True),
        (r"C:\runs\001-acme", os.name == "nt"),
        ("//server/share/runs/001-acme", True),
        # rejected: every one of these is completed against the process's CWD or current DRIVE
        ("001-acme", False),
        (str(Path("..") / "_runs" / "001-acme"), False),
        # ⚠️ False on EVERY platform, and the reason differs by platform - which is why the earlier
        # `os.name != "nt"` expectation passed locally and turned Linux CI red (4696 passed, 2
        # failed at 191d153). On Windows these are drive-relative and resolve against the current
        # drive; on POSIX they are ordinary RELATIVE filenames that merely contain a backslash and
        # a colon. Neither names a fixed location anywhere, so the predicate is right in both.
        (r"\_runs\001-acme", False),
        ("C:001-acme", False),
        ("", False),
    ],
)
def test_only_a_location_independent_path_counts_as_a_recorded_location(value: str, independent: bool) -> None:
    """The fact requirement 4 consumes, tested directly - the black-box route can only exercise it
    from a CWD that happens to match, which is exactly why the defect survived review round 1.

    ⚠️ `os.path.isabs` misses exactly ONE of these rows, and only on some interpreters - an earlier
    version of this docstring claimed two, which overstated the justification. Measured on both
    interpreters this repo can run on Windows:

    | value | `ntpath.isabs` on 3.11.10 | on 3.13.2 |
    |---|---|---|
    | `\\_runs\\001-acme` (drive-relative) | **True** - the miss | False |
    | `C:001-acme` (drive-relative with a drive) | False | False |

    So on 3.13 `isabs` alone would have sufficed here; on 3.11 it would have accepted a path that
    resolves against whatever drive the process is on. The two-fact predicate is chosen because it
    does not depend on which CPython runs it - not because `isabs` is wrong about both shapes.
    Both facts are still individually load-bearing: mutations that drop either one kill this test.
    """
    assert _is_location_independent(value) is independent


def test_an_absolute_recorded_path_spelled_awkwardly_is_still_intact(tmp_path: Path) -> None:
    """The negative control for finding 1, and the one that stops the fix being "reject everything":
    an absolute path is still absolute after a `..` hop and a case change, and must stay `intact`."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    awkward = Path(manifest[RUN_PATH_KEY]).parent / "no-such-dir" / ".." / "001-acme"
    manifest[RUN_PATH_KEY] = str(awkward)
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_INTACT
    assert check["path_check"] == PATH_CHECK_MATCHES


# --- Finding 2: discovery silently omitted existing run directories ---


def test_a_run_renamed_out_of_the_number_pattern_with_no_manifest_is_still_discovered(tmp_path: Path) -> None:
    """Discovery reported only children whose CURRENT name matched `<NNN>` or which still held a
    `run.json`, so a real allocated run that lost both matched neither test. Measured on the real
    CLI: `(no run directories found)`, `0 run(s): 0 intact, 0 moved, 0 unverifiable`, exit 0.

    ⚠️ This is NOT the accepted "the run was deleted entirely" residual - the tree is still on
    disk, it just cannot be assessed. Deciding whether a directory is a run is the question
    `check_run_location` exists to answer from evidence, so discovery must not pre-answer it."""
    run = allocate_run("acme", repo_root=tmp_path)
    run.manifest_path.unlink()
    run.root.rename(run.root.parent / "acme-without-number")

    gated, counts = verify_runs(tmp_path)

    assert [Path(entry["root"]).name for entry in gated] == ["acme-without-number"]
    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1
    assert counts[RUN_LOCATION_INTACT] == 0  # negative control
    assert verify_exit_code(counts) == 3
    assert not list_runs(tmp_path)  # inspection may still skip it; only the GATE may not


def test_verify_cli_exits_3_on_a_run_renamed_out_of_the_number_pattern(tmp_path: Path) -> None:
    """Finding 2 through the real CLI, where it was reproduced, judged by exit code."""
    run = allocate_run("acme", repo_root=tmp_path)
    run.manifest_path.unlink()
    run.root.rename(run.root.parent / "acme-without-number")

    result = _run_verify_cli(tmp_path)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "no run directories found" not in result.stdout
    assert "1 run(s)" in result.stdout and "1 unverifiable" in result.stdout


def test_a_hand_made_directory_with_no_number_and_no_manifest_is_discovered(tmp_path: Path) -> None:
    """The same hole reached without a rename: any child directory is a candidate run."""
    root = runs_root(tmp_path)
    (root / "scratch-notes").mkdir(parents=True)

    _gated, counts = verify_runs(tmp_path)

    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1
    assert verify_exit_code(counts) == 3


def test_a_regular_file_beside_the_runs_is_not_a_candidate_run(tmp_path: Path) -> None:
    """The negative control for discovery: unfiltered means every child DIRECTORY, not every child.
    A future `_runs/INDEX.md` must not turn a healthy tree red."""
    allocate_run("acme", repo_root=tmp_path)
    (runs_root(tmp_path) / "INDEX.md").write_text("# runs\n", encoding="utf-8")

    _gated, counts = verify_runs(tmp_path)

    assert counts == {
        RUN_LOCATION_INTACT: 1,
        RUN_LOCATION_MOVED: 0,
        RUN_LOCATION_DUPLICATE: 0,
        RUN_LOCATION_UNVERIFIABLE: 0,
    }
    assert verify_exit_code(counts) == 0


def test_a_runs_root_that_cannot_be_listed_is_unverifiable_not_zero_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second half of finding 2: an `OSError` from enumerating the root was converted into an
    empty list, i.e. `0 run(s): 0 intact, 0 moved, 0 unverifiable`, exit 0 - while the runs
    underneath it still existed and simply could not be discovered."""
    allocate_run("acme", repo_root=tmp_path)
    root = runs_root(tmp_path)
    real_iterdir = Path.iterdir

    def _refuse(self: Path):  # noqa: ANN202 - a generator/iterator, matching Path.iterdir
        if self == root:
            raise PermissionError(5, "Access is denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _refuse)

    gated, counts = verify_runs(tmp_path)

    assert len(gated) == 1, "an unreadable root produced no entry at all"
    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1
    assert counts[RUN_LOCATION_INTACT] == 0  # negative control
    assert verify_exit_code(counts) == 3
    assert "Access is denied" in gated[0]["location_check"]["detail"]


def test_a_runs_root_that_is_a_file_is_unverifiable_not_zero_runs(tmp_path: Path) -> None:
    """`Path.is_dir()` swallows every error and answers False, so "I could not look" and "it is not
    a directory" were the same answer - and both meant exit 0."""
    runs_root(tmp_path).write_text("not a directory", encoding="utf-8")

    _gated, counts = verify_runs(tmp_path)

    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1
    assert verify_exit_code(counts) == 3


def test_a_runs_root_that_was_never_created_is_still_a_clean_zero(tmp_path: Path) -> None:
    """The negative control for the root probe: "nothing has been allocated" is legitimately clean,
    and must stay distinguishable from "the root exists and cannot be read"."""
    _gated, counts = verify_runs(tmp_path)

    assert counts == {
        RUN_LOCATION_INTACT: 0,
        RUN_LOCATION_MOVED: 0,
        RUN_LOCATION_DUPLICATE: 0,
        RUN_LOCATION_UNVERIFIABLE: 0,
    }
    assert verify_exit_code(counts) == 0


# --- Finding 3: a run number contradicting its own directory read as healthy ---


@pytest.mark.parametrize("planted", [2, 0, 10**100])
def test_a_run_number_that_contradicts_its_own_directory_is_unverifiable(tmp_path: Path, planted: int) -> None:
    """The number IS the identity in this convention (issue #234), and nothing checked it against
    the `<NNN>-<slug>` directory it sits in - only that it was a non-negative integer. Changing
    nothing but `"run"` in a freshly allocated `001-acme` reported `1 intact`, exit 0, for both `2`
    and `10**100`, while a float or a null correctly reported `1 unverifiable`, exit 3."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest["run"] = planted
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["state"] != RUN_LOCATION_INTACT  # negative control: the defect being fixed
    assert check["derived_name_check"] == DERIVED_NAME_MISMATCH
    assert "CONTRADICTS" in check["detail"]


def test_a_unit_key_that_contradicts_its_own_directory_is_unverifiable(tmp_path: Path) -> None:
    """The same contradiction reached through the other half of the derived name."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest["unit_key"] = "something-else"
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assert _state_of(list_runs(tmp_path)[0]) == RUN_LOCATION_UNVERIFIABLE


def test_verify_cli_exits_3_on_a_contradictory_run_number(tmp_path: Path) -> None:
    """Finding 3 through the real CLI, judged by exit code."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest["run"] = 2
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = _run_verify_cli(tmp_path)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "INTACT" not in result.stdout


def test_an_unusable_run_number_is_reported_before_the_agreement_check(tmp_path: Path) -> None:
    """Ordering control: `"run": "two"` is not a number at all, so it must be reported as an
    unusable run number rather than as a name disagreement - the requirements are a ladder, and the
    detail has to name the first rung that failed or it points at the wrong repair."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest["run"] = "two"
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    check = list_runs(tmp_path)[0]["location_check"]

    assert check["state"] == RUN_LOCATION_UNVERIFIABLE
    assert check["derived_name_check"] == DERIVED_NAME_NOT_CONSULTED
    assert "not a usable run number" in check["detail"]


def test_a_matching_run_number_is_still_intact(tmp_path: Path) -> None:
    """The negative control for finding 3: demanding agreement must not make agreement impossible.
    Rewriting `"run"` to the value it already had leaves the run `intact`."""
    run = allocate_run("acme", repo_root=tmp_path)
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest["run"] = 1
    run.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assert _state_of(list_runs(tmp_path)[0]) == RUN_LOCATION_INTACT


# --- The residual: a manifest whose bytes make the READER raise, not the checker ---


def test_an_oversized_json_integer_is_unverifiable_not_a_traceback(tmp_path: Path) -> None:
    """A JSON integer over CPython's 4300-digit int/str conversion limit makes `json.loads` raise a
    bare `ValueError`, which is NOT a `json.JSONDecodeError` and escaped the guard: the CLI exited
    1 with a traceback instead of reporting `unverifiable`. Fail-closed, but `_read_run_manifest`
    documents that it never raises, so it must not."""
    root = runs_root(tmp_path)
    (root / "001-acme").mkdir(parents=True)
    (root / "001-acme" / "run.json").write_text('{"run": 1, "big": ' + "9" * 5000 + "}", encoding="utf-8")

    _gated, counts = verify_runs(tmp_path)  # must not raise

    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1
    assert verify_exit_code(counts) == 3

    result = _run_verify_cli(tmp_path)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_a_run_json_that_is_not_utf8_is_unverifiable_not_a_traceback(tmp_path: Path) -> None:
    """The same shape one layer earlier: `read_text` raises `UnicodeDecodeError`, a `ValueError`,
    past a guard that named only `OSError`."""
    root = runs_root(tmp_path)
    (root / "001-acme").mkdir(parents=True)
    (root / "001-acme" / "run.json").write_bytes(b'{"run": 1, "unit_key": "\xff\xfe acme"}')

    _gated, counts = verify_runs(tmp_path)  # must not raise

    assert counts[RUN_LOCATION_UNVERIFIABLE] == 1
    assert verify_exit_code(counts) == 3


# --- The collapse itself: pin the structure, not only the behaviours ---


def _state_keyword_sites(state_constant: str) -> list[str]:
    """Every place in `work_dirs.py` that CONSTRUCTS a verdict with `state=<constant>`, named by
    the enclosing function. Parsed, not grepped: the invariant block quotes these literals in
    prose, and a comment is not a construction site.

    One level of local aliasing is followed, because the reviewer got past the first version of
    this pin with exactly that: `alias = RUN_LOCATION_INTACT` in the same function, then
    `state=alias` at a second construction site.
    """
    tree = ast.parse(_work_dirs_source())
    sites: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        aliases = {state_constant}
        for node in ast.walk(func):
            targets = getattr(node, "targets", None) or ([node.target] if isinstance(node, ast.AnnAssign) else [])
            value = getattr(node, "value", None)
            if isinstance(value, ast.Name) and value.id in aliases:
                aliases.update(target.id for target in targets if isinstance(target, ast.Name))
        for node in ast.walk(func):
            if not isinstance(node, ast.keyword) or node.arg != "state":
                continue
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                sites.append(func.name)
    return sites


def _constant_load_scopes(constant: str) -> list[str]:
    """Every scope that LOADS `constant`, named by the innermost enclosing function/class or
    `<module>`. A construction site elsewhere has to get the value from somewhere, so pinning the
    reference scopes catches an alias defined in a DIFFERENT function, which `_state_keyword_sites`
    cannot see."""
    scopes: list[str] = []

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Name) and child.id == constant and isinstance(child.ctx, ast.Load):
                scopes.append(scope)
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else scope
            walk(child, inner)

    walk(ast.parse(_work_dirs_source()), "<module>")
    return sorted(scopes)


def test_intact_is_constructed_at_exactly_one_place_in_work_dirs() -> None:
    """The stop rule this round was given: do not narrow the class one site at a time. Three more
    local guards would have left a fourth site to find, so `check_run_location` is now the only
    function that can produce a clean verdict, on its last line, after every requirement held.

    Two independent pins, because the first version of this test was demonstrably weak - a
    reviewer asked to attack the pin itself got past it by constructing through `state=alias`:

    1. construction sites, following one level of local aliasing;
    2. the exact set of scopes that so much as LOAD the constant, which catches an alias defined
       in another function and any second reference added anywhere in the module.

    ⚠️ **Known limitation, stated rather than implied: this catches DIRECT construction and simple
    local aliasing only.** A value routed through a container, an f-string, a function's return
    value, or passed in as a parameter would construct `intact` without ever loading the constant
    in that scope, and both pins would pass. Closing that needs dataflow analysis, which is not
    worth it here - the behavioural tests above are what actually prove the verdicts, and this pin
    exists to make an obvious regression noisy, not to be a proof. Production structure was
    confirmed correct by review; this is a proof weakness, not a defect."""
    assert _state_keyword_sites("RUN_LOCATION_INTACT") == ["check_run_location"]
    assert _state_keyword_sites("RUN_LOCATION_MOVED") == ["check_run_location"]

    assert _constant_load_scopes("RUN_LOCATION_INTACT") == [
        "<module>",
        "check_run_location",
        "is_intact",
        "verify_runs",
    ], (
        "a new scope references `intact` - either the classification surface re-opened, or this "
        "allowlist needs a deliberate update naming the new consumer"
    )
    assert _constant_load_scopes("RUN_LOCATION_MOVED") == ["<module>", "check_run_location", "verify_exit_code"]


def test_discovery_applies_no_name_pattern_and_no_manifest_precondition() -> None:
    """Finding 2 pinned structurally. `_discover_run_dirs` filtered on `_RUN_DIR_RE` and on
    `run.json` existing; both are fail-open pre-answers to the question `check_run_location` exists
    to answer. Parsed rather than grepped so a renamed regex or a nested helper cannot slip past.

    The call allowlist is the part that matters, and it is here because the first version of this
    pin was demonstrably weak: a reviewer asked to attack it got past by moving the filtering into
    an external helper, which no `_RUN_DIR_RE`-name check can see. Extracting ANY new call out of
    discovery now fails this test - which is the intended cost, because "discovery consults
    something else before deciding" is exactly the shape being prohibited.

    ⚠️ Limitation: this pins what discovery may CALL, not what those calls do. Adding a filter
    inline with no call (e.g. `if child.name.startswith("0")`) would pass both assertions; the
    behavioural tests above are what catch that."""
    source = _work_dirs_source()
    tree = ast.parse(source)
    discover = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_discover_run_dirs"
    )
    names = {node.id for node in ast.walk(discover) if isinstance(node, ast.Name)}
    calls = {ast.unparse(node.func) for node in ast.walk(discover) if isinstance(node, ast.Call)}
    body = ast.get_source_segment(source, discover) or ""

    assert "_RUN_DIR_RE" not in names, "discovery re-acquired a name-pattern filter"
    assert "run.json" not in body.split('"""')[-1], "discovery re-acquired a 'has a manifest' precondition"
    assert calls == {"os.stat", "stat.S_ISDIR", "sorted", "root.iterdir", "found.append"}, (
        f"discovery calls {sorted(calls)} - it may only stat, list and collect. A new call is how "
        "a filter gets re-introduced from outside, where a name check cannot see it"
    )


def test_the_invariant_is_stated_in_work_dirs_itself() -> None:
    """The prevention half, same reasoning as the rename prohibition: the rule has to live in the
    file the next agent opens, or the next local guard gets added beside the last one."""
    source = _work_dirs_source()

    assert "THE ONE INVARIANT" in source
    assert "only ever returned on positive proof" in source
