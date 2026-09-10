"""Mutation controls for the package filesystem/manifest integrity slice (issue #562, S1).

    python tests/mutation_package_filesystem.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest, exactly as
``tests/mutation_reference_readiness.py`` does, and it **imports** the shared scoring machinery from
``tests/mutation_harness.py`` rather than forking it. That harness's own docstring records why the
scoring is the load-bearing part: its first run reported 22/22 caught because an injected plugin's
import error exits non-zero before any test runs, and a naive verdict scored that as a detection.

Each entry names the ANCHOR that must catch it and the CONTROLS that must survive it. Without the
controls, "caught" cannot be told apart from "the mutation broke everything", which is the failure
mode that makes a mutation score meaningless.

⚠️ **Every anchor asserts a NAMED guard, never merely a fail-closed refusal.** That is deliberate and
it is what these controls exist to prove. Several of the mutations below leave the package non-clean
for a *different* reason - delete the duplicate-key hook and the manifest still parses to an empty
file map, so every real file reads as undeclared; delete the alias normalization and the second
spelling reads as a missing file. A test that asserted only "not clean" would call both of those
caught while the guard it claims to cover had been removed. The anchors therefore assert the specific
code, and these mutations are what proves that assertion is not decorative.

The no-op control is not decoration either: if a cosmetic reword of a diagnostic string is CAUGHT,
the suite is asserting on incidental wording rather than on behaviour.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_harness import (  # noqa: E402  # pylint: disable=wrong-import-position
    PY,
    ROOT,
    observed_mutation,
    run,
    sanitized_env,
    session_ended_abnormally,
    session_is_trustworthy,
)

#: Anchors are RESOLVED across these suites rather than hard-coded, and a name found in zero or in
#: more than one is a hard error - a stale anchor is how a mutation harness goes green while proving
#: nothing (see `mutation_reference_readiness.py`, which measured four such entries in one run).
TARGETS = (
    "tests/test_package_filesystem.py",
    "tests/test_check_reference_readiness.py",
)
WHOLE_SUITE = TARGETS[0]

CAUGHT = "CAUGHT"
SURVIVED = "SURVIVED"
INVALID = "INVALID"

#: A wrapper that neutralizes exactly ONE arm of the verifier and recomputes the status honestly, so
#: the mutation removes a guard rather than breaking the module. Shared because two mutations differ
#: only in which code they drop.
_DROP_ARM = """
import package_filesystem as pfs
_orig = pfs.verify_package
def verify_package(root, classification):
    result = _orig(root, classification)
    findings = tuple(row for row in result.findings if row.code != {code})
    if result.unassessable:
        status = pfs.STATUS_UNASSESSABLE
    elif findings:
        status = pfs.STATUS_FINDINGS
    else:
        status = pfs.STATUS_CLEAN
    return pfs.PackageFilesystemResult(
        status=status,
        findings=findings,
        unassessable=result.unassessable,
        files_declared=result.files_declared,
        files_verified=result.files_verified,
    )
pfs.verify_package = verify_package
"""


@dataclass(frozen=True)
class Mutation:
    """One patch, the test that must catch it, and the tests that must not."""

    code: str
    anchor: str | None = None
    controls: tuple[str, ...] = ()
    whole_suite: str | None = None


MUTATIONS: dict[str, Mutation] = {
    # --- the integration itself ----------------------------------------------------------------
    "remove-the-package-invocation": Mutation(
        code="""
import check_reference_readiness as crr
import package_filesystem as pfs
# The pre-#562-S1 gate: a safe package went straight into evidence collection and source resolution,
# so a package that had gained, lost or changed a file was READY on evidence nobody could attribute.
crr.verify_package = lambda root, classification: pfs.PackageFilesystemResult(status=pfs.STATUS_CLEAN)
""",
        anchor="test_an_extra_file_in_a_package_blocks_before_resolve_and_discovery",
        controls=(
            "test_a_SAFE_package_continues_into_the_current_behaviour_unchanged",
            "test_a_package_whose_manifest_describes_its_bytes_is_clean",
        ),
    ),
    "verify-after-evidence-instead-of-before": Mutation(
        code="""
import check_reference_readiness as crr
import package_filesystem as pfs
# Same check, wrong ORDER: discovery has already run by the time integrity is judged, which is the
# whole defect - a damaged package's evidence has been read and its source resolved before anyone
# asked whether the package is what it says it is.
_orig = crr.verify_package
def verify_package(root, classification):
    crr._collect_evidence(root, None, None)
    return _orig(root, classification)
crr.verify_package = verify_package
""",
        anchor="test_an_extra_file_in_a_package_blocks_before_resolve_and_discovery",
        controls=("test_a_SAFE_package_continues_into_the_current_behaviour_unchanged",),
    ),
    # --- strict JSON ----------------------------------------------------------------------------
    "remove-the-duplicate-key-hook": Mutation(
        code="""
import package_filesystem as pfs
# `json.loads` keeps the LAST value for a repeated key, silently. The package is still non-clean
# afterwards - every real file reads as undeclared - so only an anchor that names the DUPLICATE-KEY
# guard can tell this apart from the guard still working.
pfs._no_duplicate_keys = lambda pairs: dict(pairs)
""",
        anchor="test_a_duplicate_key_at_the_top_level_is_refused",
        controls=("test_a_package_whose_manifest_describes_its_bytes_is_clean",),
    ),
    "duplicate-hook-only-at-the-top-level": Mutation(
        code="""
import package_filesystem as pfs
import json
# The half-fix: refuse a repeated key in the OUTERMOST object only. A nested duplicate - inside
# `oracle.objects[]`, four levels down - passes, which is precisely where nobody was looking.
_depth = {"n": 0}
def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) and any(key in ("contents", "unit", "kind") for key in keys):
        raise pfs._ManifestError(pfs.CODE_MANIFEST_DUPLICATE_KEY)
    return dict(pairs)
pfs._no_duplicate_keys = _no_duplicate_keys
""",
        anchor="test_a_duplicate_key_NESTED_deep_in_the_manifest_is_refused_too",
        controls=("test_a_duplicate_key_at_the_top_level_is_refused",),
    ),
    "remove-parse-constant": Mutation(
        code="""
import package_filesystem as pfs
# Python's default: NaN/Infinity become floats and the document parses. The manifest then means
# something no other JSON reader agrees with, and the package verifies CLEAN.
pfs._no_constants = float
""",
        anchor="test_a_non_finite_json_constant_is_refused",
        controls=("test_a_package_whose_manifest_describes_its_bytes_is_clean",),
    ),
    # --- keys -----------------------------------------------------------------------------------
    "remove-the-alias-normalization": Mutation(
        code="""
import package_filesystem as pfs
# Compare keys literally. Two spellings that are ONE file on a case-insensitive host now look like
# two declarations - and the package stays non-clean for the WRONG reason (a missing file), which is
# what makes a "not clean" assertion here vacuous.
pfs.alias_key = lambda key: key
""",
        anchor="test_two_keys_that_differ_only_by_case_cannot_describe_distinct_bytes",
        controls=("test_a_package_whose_manifest_describes_its_bytes_is_clean",),
    ),
    "accept-any-key-that-is-a-string": Mutation(
        code="""
import package_filesystem as pfs
# The permissive spelling: any string is a path. Absolute, traversing, device-reserved and
# backslash-separated keys all become declarations again.
pfs.is_canonical_key = lambda key: bool(key)
""",
        anchor="test_an_unsafe_declared_key_is_refused",
        controls=("test_a_package_whose_manifest_describes_its_bytes_is_clean",),
    ),
    # --- the walk ---------------------------------------------------------------------------------
    "remove-the-reparse-dead-end": Mutation(
        code="""
import os
import stat
from pathlib import Path
import package_filesystem as pfs
# Descend through a reparse point and say nothing about it: the package's contents are then decided
# by bytes on the far side of a junction, which is the boundary failure the walk exists to prevent.
def walk_package(root):
    files, rows, empty = {}, [], []
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            listed = sorted(entries, key=lambda entry: entry.name)
        for entry in listed:
            relative = f"{prefix}{entry.name}"
            info = entry.stat(follow_symlinks=True)
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), f"{relative}/"))
            elif stat.S_ISREG(info.st_mode):
                files[relative] = Path(entry.path)
    return files, rows, empty
pfs.walk_package = walk_package
""",
        anchor="test_a_directory_junction_is_a_finding_and_a_DEAD_END",
        controls=("test_a_package_whose_manifest_describes_its_bytes_is_clean",),
    ),
    # --- set equality and digests -----------------------------------------------------------------
    "remove-the-extra-file-arm": Mutation(
        code=_DROP_ARM.format(code="pfs.CODE_FILE_UNDECLARED"),
        anchor="test_an_extra_file_is_refused",
        controls=(
            "test_a_deleted_file_is_refused",
            "test_a_package_whose_manifest_describes_its_bytes_is_clean",
        ),
    ),
    "remove-the-digest-comparison": Mutation(
        code=_DROP_ARM.format(code="pfs.CODE_DIGEST_MISMATCH"),
        anchor="test_a_changed_byte_is_caught_because_every_declared_file_is_REHASHED",
        controls=(
            "test_an_extra_file_is_refused",
            "test_a_package_whose_manifest_describes_its_bytes_is_clean",
        ),
    ),
    "unassessable-counts-as-clean": Mutation(
        code="""
import package_filesystem as pfs
# The fail-open that "I could not tell" always tempts: fold it into clean. Every refusal above still
# works, so only a control that separates the two states notices.
pfs.PackageFilesystemResult.is_clean = property(lambda self: not self.findings)
""",
        anchor="test_an_unassessable_result_is_never_clean_even_when_NOTHING_else_is_wrong",
        controls=("test_a_package_whose_manifest_describes_its_bytes_is_clean",),
    ),
    # --- discriminating whole-suite controls -------------------------------------------------------
    "control-cosmetic-reword-of-every-diagnostic": Mutation(
        code="""
import package_filesystem as pfs
# Pure presentation, ASCII preserved. If this is CAUGHT, the suite asserts on incidental wording.
pfs._DETAILS = {code: detail.replace(" - ", "; ") for code, detail in pfs._DETAILS.items()}
""",
        whole_suite=SURVIVED,
    ),
    "control-absent-anchor": Mutation(
        code="""
import package_filesystem as pfs
# Names something that does not exist. Must be reported invalid, never credited as a detection.
pfs.no_such_function_exists.disabled = True
""",
        whole_suite=INVALID,
    ),
}


@dataclass
class Failure:
    """One expectation that did not hold."""

    mutation: str
    node: str
    want: str
    got: str
    detail: str = field(default="")

    def __str__(self) -> str:
        return f"{self.mutation} / {self.node}: want {self.want}, got {self.got} ({self.detail})"


def resolve_node(name: str) -> str:
    """The full pytest node id for an anchor, or a hard error if it is not uniquely findable."""
    hits = [target for target in TARGETS if f"def {name}(" in (ROOT / target).read_text(encoding="utf-8")]
    if len(hits) != 1:
        raise SystemExit(f"anchor {name!r} found in {len(hits)} suite(s), expected exactly 1: {hits}")
    return f"{hits[0]}::{name}"


def baseline_is_clean() -> bool:
    """A mutation is only evidence against a clean baseline."""
    proc = subprocess.run(
        [PY, "-m", "pytest", *TARGETS, "-q", "--no-header", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=sanitized_env(),
    )
    print(f"BASELINE {len(TARGETS)} suite(s) exit={proc.returncode}")
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
    return proc.returncode == 0


def verdict_for(name: str, code: str, target: str) -> tuple[str, str]:
    """Score one mutation against one pytest target, using the shared harness's lifecycle record."""
    try:
        _, exit_code, detail, outcomes = run(name, code, target)
    except SystemExit as exc:
        # `run()` raises when the injected plugin never imported - the absent-anchor case.
        return INVALID, str(exc)
    if observed_mutation(outcomes):
        note = detail if not session_ended_abnormally(outcomes) else f"{detail} [abnormal exit {exit_code}]"
        return CAUGHT, note
    if session_is_trustworthy(outcomes):
        return SURVIVED, detail
    return INVALID, f"no verdict (exit {exit_code}, {detail})"


def check(name: str, mutation: Mutation) -> list[Failure]:
    """Run one mutation against its anchor and controls, or against the whole suite."""
    failures: list[Failure] = []
    if mutation.whole_suite is not None:
        got, detail = verdict_for(name, mutation.code, WHOLE_SUITE)
        flag = "ok " if got == mutation.whole_suite else "BAD"
        print(f"{flag} {got:8s} (want {mutation.whole_suite:8s})  {name:56s} <whole suite>")
        if got != mutation.whole_suite:
            failures.append(Failure(name, "<whole suite>", mutation.whole_suite, got, detail))
        return failures
    expectations = [(mutation.anchor or "", CAUGHT), *((node, SURVIVED) for node in mutation.controls)]
    for node, want in expectations:
        got, detail = verdict_for(name, mutation.code, resolve_node(node))
        flag = "ok " if got == want else "BAD"
        print(f"{flag} {got:8s} (want {want:8s})  {name:56s} {node}")
        if got != want:
            failures.append(Failure(name, node, want, got, detail))
    return failures


def main() -> int:
    """Run every mutation against its committed anchor, and fail on any mismatch."""
    if not baseline_is_clean():
        print("\nHARNESS ERROR: baseline is not clean, so no mutation verdict is trustworthy.")
        return 2
    print()
    failures = [failure for name, mutation in MUTATIONS.items() for failure in check(name, mutation)]
    checks = sum(1 if m.whole_suite else 1 + len(m.controls) for m in MUTATIONS.values())
    print()
    if failures:
        print("MUTATION EXPECTATIONS NOT MET:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"All {len(MUTATIONS)} mutations matched their expectations ({checks} anchor/control checks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
