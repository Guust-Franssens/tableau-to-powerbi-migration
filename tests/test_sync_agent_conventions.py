"""Tests for `scripts/sync_agent_conventions.py`'s bundle-path gate.

Every test here pins a boundary that has already MOVED once, so each one names the failure it exists
to prevent rather than merely exercising a branch:

* Issue #123 - `<bundle>/out/pbip/` was documented in AGENTS.md and all four personas for weeks. Every
  copy agreed, and agreement was the only thing checked. That defect must keep failing, forever.
* The widened scan (persona bodies, not just the generated block) then rejected a CORRECT document:
  `<bundle>/summary.md` is a real bundle-root file, and the directory rule read it as a directory
  named `summary.md`, reporting "there is no `out/` level" about a file that has nothing to do with
  `out/`. `docs/operator-runbook.md` already cites 6 distinct files that way, so the cheapest fix
  available to an author was to DELETE the citation.
* The `--bundle` on-disk resolution likewise must not demand what a legitimate bundle omits:
  `data/` (no flat-file extracts) or a conditionally written root file (`empty-model-check.json`).

The through-line: a fix that stops rejecting files by also stopping rejecting `out/` is the worst
outcome, so the negative case is asserted beside every positive one.
"""

# Test names and explicit empty-list comparisons carry the boundary language this module pins.
# pylint: disable=invalid-name,missing-function-docstring,use-implicit-booleaness-not-comparison

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sync_agent_conventions as sac  # noqa: E402  # pylint: disable=wrong-import-position

BLOCK = """<!-- BEGIN:shared-conventions -->
- **Three locations.**
  | stage | location |
  |---|---|
  | engine truth | `<bundle>/reports/` |
  | working copy | `<bundle>/pbip/` |

  A bundle is `<bundle>/{pbip,reports,semantic_models,handover,data}`.
<!-- END:shared-conventions -->
"""

PERSONA = """---
name: probe
description: a probe persona
---

# Probe — Subagent

## Gotchas

{body}
"""


@pytest.fixture(name="repo")
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A miniature repo: one AGENTS.md carrying the block, one persona whose body we vary."""
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    source = tmp_path / "AGENTS.md"
    source.write_text(BLOCK, encoding="utf-8")
    monkeypatch.setattr(sac, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sac, "SOURCE", source)
    monkeypatch.setattr(sac, "AGENTS_DIR", agents_dir)

    def write_persona(body: str) -> Path:
        path = agents_dir / "probe.agent.md"
        path.write_text(PERSONA.format(body=body), encoding="utf-8")
        return path

    def write_block(text: str) -> None:
        source.write_text(text, encoding="utf-8")

    return write_persona, write_block


def _bundle(root: Path, dirs: tuple[str, ...], files: tuple[str, ...] = ()) -> Path:
    for name in dirs:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in files:
        (root / name).write_text("{}", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The correct document must pass (the regression this suite was added for).
# ---------------------------------------------------------------------------


def test_bundle_root_file_citation_is_not_a_layout_claim(repo) -> None:
    """`<bundle>/summary.md` is a citation, not a claim that a directory named `summary.md` exists."""
    write_persona, _ = repo
    write_persona("Read `<bundle>/summary.md` first, then `<bundle>/empty-model-check.json`.")
    assert sac.check_bundle_paths(None) == []


@pytest.mark.parametrize(
    "cited",
    ["summary.md", "report.json", "phase-timings.json", "deploy-journal.jsonl", "deploy-estate-id.txt"],
)
def test_every_root_file_style_the_runbook_uses_is_accepted(repo, cited: str) -> None:
    """docs/operator-runbook.md writes all of these; the gate must not make an author delete them."""
    write_persona, _ = repo
    write_persona(f"See `<bundle>/{cited}` for the verdict.")
    assert sac.check_bundle_paths(None) == []


# ---------------------------------------------------------------------------
# ...and the original defect must still fail. Both halves, always together.
# ---------------------------------------------------------------------------


def test_issue_123_out_level_still_fails_in_the_block(repo) -> None:
    """The exact #123 shape: an `out/` level in the generated conventions block."""
    write_persona, write_block = repo
    write_persona("Nothing to see here.")
    write_block(BLOCK.replace("`<bundle>/reports/`", "`<bundle>/out/reports/`"))
    problems = sac.check_bundle_paths(None)
    assert len(problems) == 1
    assert "`out` is not a bundle directory" in problems[0]
    assert "the shared-conventions block" in problems[0]


def test_issue_123_out_level_still_fails_in_a_persona_body(repo) -> None:
    """Same defect outside the fence - invisible before the scan was widened."""
    write_persona, _ = repo
    write_persona("The model lives at `<bundle>/out/pbip/<wb>/<Name>.SemanticModel`.")
    problems = sac.check_bundle_paths(None)
    assert len(problems) == 1
    assert "`out` is not a bundle directory" in problems[0]
    assert "probe.agent.md" in problems[0]


def test_a_file_citation_does_not_launder_an_out_level(repo) -> None:
    """The dotted-name exemption must not become a bypass: `out` is still `out`."""
    write_persona, _ = repo
    write_persona("Read `<bundle>/summary.md`, but the model is in `<bundle>/out/pbip/`.")
    problems = sac.check_bundle_paths(None)
    assert len(problems) == 1
    assert "`out` is not a bundle directory" in problems[0]


def test_an_unknown_directory_still_fails(repo) -> None:
    """A plausible-but-wrong directory (no dot) is exactly what BUNDLE_DIRS is for."""
    write_persona, _ = repo
    write_persona("Artifacts land in `<bundle>/output/`.")
    assert any("`output` is not a bundle directory" in p for p in sac.check_bundle_paths(None))


@pytest.mark.parametrize(
    ("cited", "offender"),
    [
        ("<bundle>/out/reports/", "out"),
        ("<bundle>/out.d/reports/", "out.d"),
        ("<bundle>/out.old/pbip/", "out.old"),
        ("<bundle>/v2.0/reports/", "v2.0"),
    ],
)
def test_a_dotted_non_final_segment_is_a_directory_claim(repo, cited: str, offender: str) -> None:
    """Reviewer's probes. The first three passed (exit 0) when a dot ANYWHERE exempted the path.

    The regex captures only the first segment, so `out.d` looked like a filename and the layout claim
    behind it - `/reports/` - went unchecked. A file citation is the LAST segment with no trailing
    separator; a dot on a non-final segment proves nothing.
    """
    write_persona, _ = repo
    write_persona(f"The engine writes `{cited}`.")
    problems = sac.check_bundle_paths(None)
    assert len(problems) == 1
    assert f"`{offender}` is not a bundle directory" in problems[0]


def test_an_extensionless_final_token_is_treated_as_a_directory(repo) -> None:
    """DECIDED, not accidental: `<bundle>/LICENSE` FAILS.

    A dot is required on top of last-segment, so a dot-free final token is checked. The conservative
    side: `<bundle>/out` (the #123 defect written without a trailing slash) reads identically, and no
    bundle-root artifact observed on a real 38-workbook bundle is extensionless. Cost of this choice is
    one loud, trivially fixable false positive; cost of the other is a silent hole where the original
    defect lives.
    """
    write_persona, _ = repo
    write_persona("See `<bundle>/LICENSE` for terms.")
    problems = sac.check_bundle_paths(None)
    assert len(problems) == 1
    assert "`LICENSE` is not a bundle directory" in problems[0]


def test_a_trailing_slash_alone_is_still_a_directory_claim(repo) -> None:
    """`<bundle>/reports/` has no further segment but is not a file - the trailing `/` decides."""
    write_persona, _ = repo
    write_persona("Engine truth is `<bundle>/reports/`, never edited.")
    assert sac.check_bundle_paths(None) == []
    write_persona("Engine truth is `<bundle>/rreports/`, never edited.")
    assert any("`rreports` is not a bundle directory" in p for p in sac.check_bundle_paths(None))


# ---------------------------------------------------------------------------
# --bundle: resolve real locations, tolerate what a legitimate bundle omits.
# ---------------------------------------------------------------------------


def test_bundle_resolution_passes_a_complete_bundle(repo, tmp_path: Path) -> None:
    write_persona, _ = repo
    write_persona("Read `<bundle>/summary.md`.")
    bundle = _bundle(tmp_path / "full", ("pbip", "reports", "semantic_models", "handover", "data"), ("summary.md",))
    assert sac.check_bundle_paths(bundle) == []


def test_bundle_missing_a_documented_location_fails(repo, tmp_path: Path) -> None:
    """`fakeB`: no `reports/`, which the block documents as a location."""
    write_persona, _ = repo
    write_persona("Nothing to see here.")
    bundle = _bundle(tmp_path / "fakeB", ("pbip", "semantic_models", "handover", "data"))
    problems = sac.check_bundle_paths(bundle)
    assert any("<bundle>/reports/" in p and "does not exist" in p for p in problems)


def test_bundle_with_only_pbip_fails(repo, tmp_path: Path) -> None:
    """`fakeC`: `pbip/` alone - the shape a half-written bundle has."""
    write_persona, _ = repo
    write_persona("Nothing to see here.")
    bundle = _bundle(tmp_path / "fakeC", ("pbip",))
    assert any("<bundle>/reports/" in p and "does not exist" in p for p in sac.check_bundle_paths(bundle))


def test_data_absent_is_tolerated_because_the_brace_form_is_vocabulary(repo, tmp_path: Path) -> None:
    """An estate with no flat-file extracts has no `data/`; the enumeration describes the layout."""
    write_persona, _ = repo
    write_persona("Nothing to see here.")
    bundle = _bundle(tmp_path / "nodata", ("pbip", "reports", "semantic_models", "handover"))
    assert sac.check_bundle_paths(bundle) == []


def test_a_conditionally_written_root_file_warns_but_does_not_fail(repo, tmp_path: Path) -> None:
    """`empty-model-check.json` is absent from a real completed bundle - a warning, not a verdict."""
    write_persona, _ = repo
    write_persona("Read `<bundle>/empty-model-check.json` before sign-off.")
    bundle = _bundle(tmp_path / "cond", ("pbip", "reports", "semantic_models", "handover", "data"))
    assert sac.check_bundle_paths(bundle) == []


# ---------------------------------------------------------------------------
# The repo's own documents, checked as documents rather than as fixtures.
# ---------------------------------------------------------------------------


def test_this_repo_passes_its_own_gate() -> None:
    """No monkeypatching: the shipped AGENTS.md and personas must satisfy the rule they publish."""
    assert sac.check_bundle_paths(None) == []


# ---------------------------------------------------------------------------
# The near-cap warning band (issue #181).
#
# `pbi-report-builder` reached 29,941/30,000 - 59 characters - and nothing said so until an edit
# would have turned CI red. Worse, the shared block is GENERATED, so an edit aimed at AGENTS.md
# fails the build via a persona the author never opened. These tests pin the two properties that
# make the band useful rather than merely present: it must exclude the over-cap files that already
# fail loudly, and it must NEVER reach the exit code.
# ---------------------------------------------------------------------------


def _persona_of_size(path: Path, size: int) -> Path:
    """Write a file whose `prompt_size` is exactly `size`, so boundaries can be asserted."""
    path.write_text("x" * size, encoding="utf-8")
    assert sac.prompt_size(path) == size, "fixture must hit the size exactly or the boundary tests lie"
    return path


def test_near_cap_band_catches_a_file_just_inside_it(tmp_path: Path) -> None:
    """One character past the line is the whole point: the warning must fire BEFORE the cap."""
    agent = _persona_of_size(tmp_path / "tight.agent.md", sac.PROMPT_NEAR_CAP_LIMIT + 1)
    assert sac.near_cap([agent]) == [agent]


def test_the_near_cap_line_itself_is_not_in_the_band(tmp_path: Path) -> None:
    """Exclusive boundary. Pins `>` against a `>=` mutation that would warn a character early."""
    agent = _persona_of_size(tmp_path / "exact.agent.md", sac.PROMPT_NEAR_CAP_LIMIT)
    assert sac.near_cap([agent]) == []


def test_a_comfortable_persona_is_not_in_the_band(tmp_path: Path) -> None:
    """The negative case beside the positive one: a normal persona must stay silent."""
    agent = _persona_of_size(tmp_path / "roomy.agent.md", sac.PROMPT_NEAR_CAP_LIMIT - 1)
    assert sac.near_cap([agent]) == []


def test_a_file_exactly_at_the_cap_is_near_cap_and_not_over(tmp_path: Path) -> None:
    """`> PROMPT_CHAR_LIMIT` is what fails, so the cap itself is the last warnable size."""
    agent = _persona_of_size(tmp_path / "brink.agent.md", sac.PROMPT_CHAR_LIMIT)
    assert sac.near_cap([agent]) == [agent]
    assert sac.report_sizes([agent]) == []


def test_an_over_cap_persona_is_excluded_from_the_band(tmp_path: Path) -> None:
    """It already fails loudly. Listing it twice puts an advisory in competition with a verdict.

    This also pins the upper bound of the band: drop `<= PROMPT_CHAR_LIMIT` and this test fails.
    """
    agent = _persona_of_size(tmp_path / "over.agent.md", sac.PROMPT_CHAR_LIMIT + 1)
    assert sac.near_cap([agent]) == []
    assert sac.report_sizes([agent]) == [agent]


def test_near_cap_never_reaches_the_exit_code(tmp_path: Path) -> None:
    """The load-bearing property. `report_sizes`'s return drives `main`'s exit code, so a near-cap
    persona must come back as zero over-cap files - a warning that fails the build is not a warning.
    """
    agent = _persona_of_size(tmp_path / "warnonly.agent.md", sac.PROMPT_NEAR_CAP_LIMIT + 500)
    assert sac.near_cap([agent]) == [agent]
    assert sac.report_sizes([agent]) == []


def test_the_band_sits_below_the_cap() -> None:
    """A near-cap limit at or above the cap would make the warning unreachable."""
    assert 0 < sac.PROMPT_NEAR_CAP_LIMIT < sac.PROMPT_CHAR_LIMIT


def test_the_repo_personas_stay_under_the_HARD_cap() -> None:
    """No monkeypatching, and deliberately the hard cap rather than the band.

    An earlier version of this test asserted the shipped personas were not in the 97% BAND. Blind
    review on PR #316 showed that defeats the whole design: `pytest -q` is a blocking step in
    `.github/workflows/checks.yml`, so a persona at 98% - still inside its documented limit - would
    turn CI red through pytest, exactly the build-break `report_sizes` is written to avoid. The band
    is advisory; only the hard cap may fail a build, and it already does via `report_sizes`.
    """
    assert sac.report_sizes(sorted((REPO_ROOT / ".github" / "agents").glob("*.agent.md"))) == []


def test_the_near_cap_warning_is_actually_EMITTED(tmp_path: Path, caplog) -> None:
    """The band's whole value is the message a human reads, and list membership does not prove it.

    Blind review deleted the `log.warning` call outright and all 30 tests still passed: every other
    test here asserts a return value, and the warning is a side effect none of them observe. This
    pins the visible behaviour - the file name and its exact remaining headroom.
    """
    agent = _persona_of_size(tmp_path / "loud.agent.md", sac.PROMPT_NEAR_CAP_LIMIT + 250)
    with caplog.at_level(logging.WARNING, logger=sac.log.name):
        sac.report_sizes([agent])
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    # Assert the INVARIANT (which file, how much headroom), never the phrasing around it. Blind
    # review demonstrated that pinning "650 left" fails on a behaviour-preserving reword to
    # "650 chars remaining" - a false alarm on a harmless edit. The looser form still catches both
    # realistic regressions: a deleted warning and one downgraded to log.info both yield "".
    assert "loud.agent.md" in warnings, "the warning must name the file, not just count personas"
    assert "650" in warnings, "the warning must state the exact remaining headroom"


def test_no_near_cap_warning_when_nothing_is_in_the_band(tmp_path: Path, caplog) -> None:
    """The negative case beside the positive one: a comfortable persona must produce silence."""
    agent = _persona_of_size(tmp_path / "quiet.agent.md", 20_000)
    with caplog.at_level(logging.WARNING, logger=sac.log.name):
        sac.report_sizes([agent])
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# Root-document contracts (AGENTS.md).
#
# The size gate above is the only executable thing that has ever touched AGENTS.md, and it measures
# LENGTH - so every routing contract the root document carries could be deleted to make room and no
# test would notice. That is the fail-open direction: a shorter AGENTS.md passes the gate harder.
#
# These are deliberately NOT a generalized documentation framework. They are a fixed anchor set of
# the contracts a dispatcher executes: the session-start/migration-start timing split, the four input
# routes, the five intake decisions, the brief path, Gate B, the post-round-2 scope freeze, the moved
# incident evidence, and the size targets themselves. Anchors are semantic (a command, a decision
# name, a link target), never a line number, and whitespace is normalized first so re-wrapping a
# paragraph is not a failure. Prose around an anchor may be rewritten freely.
# ---------------------------------------------------------------------------

AGENTS_MD = REPO_ROOT / "AGENTS.md"
AGENT_OPS_MD = REPO_ROOT / "docs" / "agent-operations.md"
OPERATOR_RUNBOOK_MD = REPO_ROOT / "docs" / "operator-runbook.md"
ORACLE_SCRIPT = REPO_ROOT / "scripts" / "capture_tableau_oracle.py"

# Project TARGETS, tighter than `sac.PROMPT_CHAR_LIMIT`, measured with the repository's own gate
# (`sac.prompt_size` - the whole file, CRLF included) so this cannot disagree with the tool that
# fails the build. There is deliberately no lower bound: `dry-run-operator` is far below the persona
# target and that is fine.
AGENTS_SIZE_TARGET = 45_000
PERSONA_SIZE_TARGET = 22_000

# contract id -> (document, required anchors). Table-driven so the mutation proof below can delete
# each anchor individually and name the contract that noticed.
ROOT_CONTRACTS: dict[str, tuple[Path, tuple[str, ...]]] = {
    "session-start-timing": (
        AGENTS_MD,
        (
            "powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1 -Update -CheckUpstream",
            "| Session start (nothing in flight) | `preflight.ps1 -Update -CheckUpstream` |",
            "| Migration start (orchestrator step 0) | `preflight.ps1` (plain) |",
            "| Mid-migration | **don't upgrade the installed tooling** |",
        ),
    ),
    "dispatcher-input-routes": (
        AGENTS_MD,
        (
            "**A Tableau Server/Cloud site** (URL + PAT)",
            "python scripts/run_engine_survey.py --server <host>",
            "python scripts/assess_estate.py --out _assessment --survey _assessment/estate_survey.json",
            "python scripts/tableau_lineage.py --plan",
            "python scripts/harvest_estate_assets.py --out <dir>",
            "python scripts/run_estate.py --input <dir>/assets --output <bundle>",
            "**A folder of `.twb`/`.twbx`**",
            "python scripts/run_estate.py --input <folder> --output <bundle>",
            "**One `.twb`/`.twbx`**",
            "python scripts/parse_tableau.py <file> -o <spec>",
            "dispatch `@tableau-migrator`",
            "**A `.tds`/`.tdsx`** (data source, no workbook)",
            "`parse_tableau.py` accepts it directly",
        ),
    ),
    "migration-brief-path": (
        AGENTS_MD,
        ("`migrations/workbooks/<name>/migration-brief.md`",),
    ),
    "five-intake-decisions": (
        AGENTS_MD,
        (
            "**Confirm the plan from step 1**",
            "**Autonomy** — see below. Default `standard`.",
            "**Fidelity bar**",
            "**If we hit a wall — stop, or degrade?**",
            "**Who drives the data refreshes?** — see below. Default `scripted`.",
        ),
    ),
    "credential-stop-outranks-autonomy": (
        AGENTS_MD,
        (
            "| `autopilot` | decide, log it | decide, flag in the summary | **ask — always** |",
            "No level clears the credential stop",
        ),
    ),
    "gate-b-one-block": (
        AGENTS_MD,
        (
            "### Gate B — after parse + probe, before building",
            "**ONE block, not four serial stops**",
            "1. Published datasources",
            "2. Live sources that failed the probe",
            "3. Extract-only sources",
            "4. The high-severity `limitations_encountered` digest",
        ),
    ),
    "post-round-2-scope-freeze": (
        AGENTS_MD,
        (
            "**After R2 freeze scope**",
            "a **new class or new proof mechanism** forces simplify/delete/split/descope",
            "**Proof escalation.** Direct tests are the default.",
        ),
    ),
    "agent-operations-evidence-link": (
        AGENTS_MD,
        (
            "Incident evidence behind every rule below: [`docs/agent-operations.md`](docs/agent-operations.md).",
            "Dumps, numbers and what remains unexplained: [`docs/agent-operations.md`](docs/agent-operations.md).",
        ),
    ),
    "agent-operations-backlink": (
        AGENT_OPS_MD,
        (
            "[`AGENTS.md`](../AGENTS.md)",
            "`AGENTS.md` is the contract and this file is the reason",
        ),
    ),
    "oracle-capture-exit-contract": (
        AGENTS_MD,
        (
            'Exit 4 is "no views selected"',
            "**exit 3** is a total non-credential failure",
            "`3` total non-credential failure",
            "`4` no views selected",
        ),
    ),
    "flat-package-layout-root": (
        AGENTS_MD,
        (
            "`--out` names this directory itself",
            "`packages/<Unit>/`",
            "nested `packages/<batch>/<Unit>/` remains readable for compatibility",
        ),
    ),
    "flat-package-layout-runbook": (
        OPERATOR_RUNBOOK_MD,
        (
            "**`--out` now names the canonical `packages/` directory itself.**",
            "Each unit lands directly at `packages/<Unit>/`.",
            "Nested `packages/<batch>/<Unit>/` layouts remain readable for compatibility",
        ),
    ),
}

ANCHOR_CASES = [
    pytest.param(contract, anchor, id=f"{contract}-{index}")
    for contract, (_, anchors) in ROOT_CONTRACTS.items()
    for index, anchor in enumerate(anchors)
]


def _normalized(path: Path) -> str:
    """Collapse every run of whitespace, so an anchor survives a re-wrapped paragraph."""
    return " ".join(path.read_text(encoding="utf-8").split())


def _missing_contracts(texts: dict[Path, str]) -> list[str]:
    """Contract ids whose anchors are not all present, each naming the anchor that went missing."""
    return [
        f"{contract}: {anchor!r} missing from {path.name}"
        for contract, (path, anchors) in ROOT_CONTRACTS.items()
        for anchor in anchors
        if anchor not in texts[path]
    ]


def _repo_texts() -> dict[Path, str]:
    return {path: _normalized(path) for path in {doc for doc, _ in ROOT_CONTRACTS.values()}}


def test_the_shipped_root_documents_carry_every_contract() -> None:
    """The positive case: AGENTS.md and its evidence doc satisfy the anchor set as committed."""
    assert _missing_contracts(_repo_texts()) == []


@pytest.mark.parametrize(("contract", "anchor"), ANCHOR_CASES)
def test_deleting_any_required_anchor_fails_its_named_contract(contract: str, anchor: str) -> None:
    """Mutation proof: each anchor is individually load-bearing, and names which contract it serves.

    Without this, an anchor set is only as strong as its weakest member - a duplicated or already
    unreachable anchor would sit in the table forever, credited as coverage. Removing exactly one
    occurrence must produce a failure that names THIS contract, and every other contract must stay
    green so the report points at the deletion rather than at the document generally.
    """
    doc, _ = ROOT_CONTRACTS[contract]
    texts = _repo_texts()
    assert anchor in texts[doc], "fixture invariant: the anchor must be present before it is deleted"
    baseline = _missing_contracts(texts)
    texts[doc] = texts[doc].replace(anchor, "", 1)

    added = [failure for failure in _missing_contracts(texts) if failure not in baseline]

    assert added == [f"{contract}: {anchor!r} missing from {doc.name}"]


def test_the_retired_wrong_tool_exit_claim_is_gone_from_the_capture_row() -> None:
    """The oracle row once carried `capture_tableau_reference.py`'s exit semantics after the command
    in it had already become the oracle. Exit 3 there means a total non-credential FAILURE, not a
    "wrong tool for this source" signal, so the old sentence told a reader to keep going after a
    capture that produced nothing.
    """
    text = _normalized(AGENTS_MD)
    assert "exits 3** on an empty target" not in text
    assert '"wrong tool for this source"' not in text


def test_agents_md_exit_semantics_match_the_oracle_script() -> None:
    """Independent oracle: the meanings come from the script's own documented contract, not from us.

    Both codes are asserted in the direction the finding corrected - 4 is a selection miss, 3 is a
    total failure - so a future edit that swaps them fails here as well as in the anchor set.
    """
    script = " ".join(ORACLE_SCRIPT.read_text(encoding="utf-8").split())
    assert "``3`` total non-credential failure" in script
    assert "``4`` no views selected" in script

    text = _normalized(AGENTS_MD)
    assert "`3` total non-credential failure" in text
    assert "`4` no views selected" in text


def test_agents_md_stays_within_the_project_size_target() -> None:
    """Measured with `sac.prompt_size`, the same whole-file count the persona cap uses."""
    size = sac.prompt_size(AGENTS_MD)
    assert size <= AGENTS_SIZE_TARGET, f"AGENTS.md is {size} chars, over the {AGENTS_SIZE_TARGET} target"


def test_every_persona_stays_within_the_project_size_target() -> None:
    """A target below `PROMPT_CHAR_LIMIT`, so the hard cap is never the first thing to notice.

    No floor is asserted: `dry-run-operator` is naturally far below this, and demanding a minimum
    would reward padding.
    """
    oversize = {
        path.name: sac.prompt_size(path)
        for path in sorted((REPO_ROOT / ".github" / "agents").glob("*.agent.md"))
        if sac.prompt_size(path) > PERSONA_SIZE_TARGET
    }
    assert oversize == {}, f"personas over the {PERSONA_SIZE_TARGET} target: {oversize}"


def test_the_project_targets_bind_before_the_hard_cap() -> None:
    """A target above the enforced cap would be decorative - the cap would fail first, every time."""
    assert PERSONA_SIZE_TARGET < sac.PROMPT_CHAR_LIMIT
