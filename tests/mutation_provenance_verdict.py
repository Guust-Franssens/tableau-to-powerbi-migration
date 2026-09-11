"""Mutation harness for the provenance phase's VERDICT and its console evidence (PR #594, Refs #576).

    python tests/mutation_provenance_verdict.py

Not named ``test_*``, so pytest does not collect it - it *drives* pytest, exactly as
``tests/mutation_reference_readiness.py`` does, and it **imports** that file's scoring machinery from
``mutation_harness`` rather than forking it. The scoring is the load-bearing part: a non-zero exit
alone is not a detection (``pytest tests/does_not_exist.py`` exits 4 having run nothing), so the
verdict comes from pytest's own lifecycle record.

What is being proved
--------------------
Two blind-review findings on PR #594, restored one at a time as mutations:

* **the absolute artifact path in the detail** - the success line and the refusal line named
  ``<out>/source-provenance.json`` by its location on disk, so the run root (drive, account name,
  customer folder) left the machine on a line meant to be pasted into an issue;
* **the status-only verdict** - ``phase.status in SUCCESS_STATUSES`` mapped a result carrying
  ``input_count: 0``, ``inputs: []`` and a ``success`` status to ok=True, and the run continued to
  adjudication and handover having stamped nothing.

Each mutation redefines ``run_estate.stamp_inputs`` with the pre-correction behaviour rather than
poking a constant, because that is the code the review was about. Every mutation declares:

* ``anchor``   - the pytest node that must CATCH it, run ALONE. The committed claim.
* ``controls`` - nodes that must SURVIVE it, run alone. Without them, "caught" cannot be told apart
  from "the mutation broke everything", which makes a mutation score meaningless.

Exit 0 only when every anchor caught its mutation and every control survived it.
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

#: The suites an anchor may live in - the coordinator's provenance phase, and the module that
#: decides what a result MEANS. An anchor's file is resolved rather than hard-coded, and a name
#: found in zero or in both suites is a hard error: an anchor that quietly stopped existing would
#: make this harness green while proving nothing.
TARGETS = (
    "tests/test_run_estate.py",
    "tests/test_stamp_tableau_provenance.py",
)

CAUGHT = "CAUGHT"
SURVIVED = "SURVIVED"
INVALID = "INVALID"

#: The pre-correction `stamp_inputs`, parameterised by the four flags each mutation sets. The body
#: is otherwise the shipped one, so a mutation changes exactly the behaviour it names.
_STAMP_BODY = """
from pathlib import Path

import run_estate
import stamp_tableau_provenance as prov


def stamp_inputs(input_dir, out_dir):
    try:
        result = prov.build(input_dir, prov.resolve_env(Path(".env")))
    except Exception as exc:  # noqa: BLE001
        result = prov.failure_result("build-failed", "build", exc)

    if PUBLISH_FIRST:
        published = run_estate.write_source_provenance(out_dir, result)
        result = prov.normalize_result(result)
    else:
        if NORMALIZE:
            result = prov.normalize_result(result)
        published = run_estate.write_source_provenance(out_dir, result)
    if published is None:
        return run_estate.ProvenanceStampResult(False, "publication_failed", "could not be published")

    phase = result.get("phase") if isinstance(result, dict) else None
    status = phase.get("status", "unknown") if isinstance(phase, dict) else "unknown"
    records = result.get("inputs") if isinstance(result, dict) else []
    records = records if isinstance(records, list) else []
    matched = sum(
        1
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("origin"), dict)
        and record["origin"].get("match") == "sha256"
    )
    count = result.get("input_count", 0) if isinstance(result, dict) else 0
    where = out_dir / run_estate.SOURCE_PROVENANCE_REPORT if ABSOLUTE else run_estate.SAFE_SOURCE_PROVENANCE_REPORT
    detail = f"{count} input(s) stamped, {matched} confirmed against the site ({status}) -> {where}"
    ok = status in prov.SUCCESS_STATUSES if STATUS_ONLY else prov.is_success(result)
    return run_estate.ProvenanceStampResult(ok, status, detail)


run_estate.stamp_inputs = stamp_inputs
"""


def _stamp(*, absolute=False, normalize=True, status_only=False, publish_first=False) -> str:
    """One mutation of `stamp_inputs`, expressed as the flags that restore a pre-correction shape."""
    flags = {
        "ABSOLUTE": absolute,
        "NORMALIZE": normalize,
        "STATUS_ONLY": status_only,
        "PUBLISH_FIRST": publish_first,
    }
    return "\n".join(f"{name} = {value!r}" for name, value in flags.items()) + _STAMP_BODY


@dataclass(frozen=True)
class Mutation:
    """One patch, the test that must catch it, and the tests that must not."""

    code: str
    anchor: str
    controls: tuple[str, ...] = ()


MUTATIONS: dict[str, Mutation] = {
    # --- finding 1: the detail named the artifact by its location on disk ---------------------
    "detail-names-the-absolute-artifact-path": Mutation(
        code=_stamp(absolute=True),
        anchor="test_an_honest_local_only_result_is_published_unchanged_and_passes",
        controls=(
            "test_a_publication_failure_is_a_non_success_stamp",
            "test_a_self_contradictory_result_publishes_a_failure_and_refuses",
        ),
    ),
    "the_shareable_detail_on_every_status": Mutation(
        code=_stamp(absolute=True),
        anchor="test_the_provenance_line_names_the_artifact_bundle_relatively",
        controls=(
            "test_a_publication_failure_is_a_non_success_stamp",
            "test_a_self_contradictory_result_publishes_a_failure_and_refuses",
        ),
    ),
    "the_refusal_console_line_names_the_absolute_path": Mutation(
        code=_stamp(absolute=True),
        anchor="test_the_refusal_console_line_carries_no_host_path",
        controls=("test_a_provenance_failure_stops_before_adjudication_and_handover",),
    ),
    "the_success_log_line_names_the_absolute_path": Mutation(
        code=_stamp(absolute=True),
        anchor="test_the_success_log_line_carries_no_host_path",
        # NOT the honest-local-only control: it pins the whole detail string, so this mutation is
        # caught there too - a second anchor, not a survivor.
        controls=("test_a_self_contradictory_result_publishes_a_failure_and_refuses",),
    ),
    # --- finding 2: the verdict was read off phase.status --------------------------------------
    "verdict-from-phase-status-alone": Mutation(
        code=_stamp(status_only=True),
        anchor="test_the_verdict_does_not_rest_on_normalization_having_rewritten_the_status",
        controls=(
            "test_a_self_contradictory_result_publishes_a_failure_and_refuses",
            "test_an_honest_local_only_result_is_published_unchanged_and_passes",
        ),
    ),
    "no-consistency-check-at-all": Mutation(
        code=_stamp(normalize=False, status_only=True),
        anchor="test_a_self_contradictory_result_publishes_a_failure_and_refuses",
        controls=(
            "test_an_empty_structured_provenance_result_is_published_and_refuses",
            "test_an_honest_local_only_result_is_published_unchanged_and_passes",
        ),
    ),
    "an-unassessable-result-still-reaches-adjudication": Mutation(
        code=_stamp(normalize=False, status_only=True),
        anchor="test_a_contradictory_result_never_reaches_adjudication_or_handover",
        controls=("test_a_provenance_failure_stops_before_adjudication_and_handover",),
    ),
    "published-before-it-was-normalised": Mutation(
        code=_stamp(publish_first=True),
        anchor="test_a_self_contradictory_result_publishes_a_failure_and_refuses",
        controls=("test_an_honest_local_only_result_is_published_unchanged_and_passes",),
    ),
    # --- the rule the other two answers are derived from ---------------------------------------
    "a-success-status-with-no-inputs-is-not-a-fault": Mutation(
        code="""
import stamp_tableau_provenance as prov

_faults = prov.consistency_faults
prov.consistency_faults = lambda result: [f for f in _faults(result) if f != "success-without-inputs"]
""",
        anchor="test_a_self_contradictory_result_is_faulted_and_never_passes",
        controls=("test_a_self_consistent_result_is_never_faulted_or_rewritten",),
    ),
}


@dataclass(frozen=True)
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


def baseline_is_clean(nodes: list[str]) -> bool:
    """A mutation is only evidence against a clean baseline - measured on the nodes it will score."""
    proc = subprocess.run(
        [PY, "-m", "pytest", *nodes, "-q", "--no-header", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=sanitized_env(),
    )
    print(f"BASELINE {len(nodes)} node(s) exit={proc.returncode}")
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
    """Run one mutation against its anchor and each of its controls, alone."""
    failures: list[Failure] = []
    expectations = [(mutation.anchor, CAUGHT), *((node, SURVIVED) for node in mutation.controls)]
    for node, want in expectations:
        got, detail = verdict_for(name, mutation.code, resolve_node(node))
        flag = "ok " if got == want else "BAD"
        print(f"{flag} {got:8s} (want {want:8s})  {name:52s} {node}")
        if got != want:
            failures.append(Failure(name, node, want, got, detail))
    return failures


def main() -> int:
    """Run every mutation against its committed anchor, and fail on any mismatch."""
    nodes = sorted(
        {resolve_node(node) for m in MUTATIONS.values() for node in (m.anchor, *m.controls)},
    )
    if not baseline_is_clean(nodes):
        print("\nHARNESS ERROR: baseline is not clean, so no mutation verdict is trustworthy.")
        return 2
    print()
    failures = [failure for name, mutation in MUTATIONS.items() for failure in check(name, mutation)]
    checks = sum(1 + len(mutation.controls) for mutation in MUTATIONS.values())
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
