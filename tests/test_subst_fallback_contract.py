r"""Contract tests for the `subst` Desktop-open fallback documented for issue #566.

Why this file exists
--------------------
The shared conventions carried an UNCONDITIONAL prohibition - *"never substitute a junction,
`subst` or symlink"* - that was never measured. Measured 2026-09-08 on the committed issue-194
repro, one third of it is wrong: a physically over-ceiling tree (required path **273**) that Power
BI Desktop refuses opens through `subst R: <same physical root>` (deepest path **253**), refreshes
four real rows and captures its page.

Correcting a prohibition is the dangerous direction of edit, because the permission is memorable and
the boundary is not. So this file pins BOTH halves in the same run:

* the permission exists and is discoverable (AGENTS.md, the dry-run persona, the runbook branch, the
  navigation row, and the canonical recipe in `docs/windows-path-limits.md`);
* the boundary survives beside it - the physical path stays canonical, `check_path_ceiling.py` still
  judges the physical tree, an alias is never a waiver, and the retired absolute prohibition is not
  left on the page next to its own correction.

The `docs/windows-path-limits.md` half is deliberately checked HERE rather than in
`tests/test_sync_agent_conventions.py`: that file's anchor set is the dispatcher contract carried by
the root documents, while this is the evidence page behind one measurement.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENTS_MD = REPO_ROOT / "AGENTS.md"
DRY_RUN_OPERATOR_MD = REPO_ROOT / ".github" / "agents" / "dry-run-operator.agent.md"
OPERATOR_RUNBOOK_MD = REPO_ROOT / "docs" / "operator-runbook.md"
WINDOWS_PATH_LIMITS_MD = REPO_ROOT / "docs" / "windows-path-limits.md"
INDEX_MD = REPO_ROOT / "docs" / "INDEX.md"

#: The exact wording that was retired. It must not survive in the two contract documents that used
#: to carry it - an additive edit that leaves the absolute prohibition beside its own exception is
#: how an agent picks the shorter, more familiar rule and the correction never lands.
RETIRED_PROHIBITION = "never substitute a junction, `subst` or symlink"

#: document -> phrases the #566 contract requires. Each is semantic (a measurement, a command, a
#: boundary), never a line number, and whitespace is normalized first so re-wrapping is not a
#: failure. Every phrase must occur EXACTLY once in its document, which the mutation proof below
#: relies on and `test_every_required_phrase_occurs_exactly_once` states outright.
REQUIRED: dict[str, tuple[Path, tuple[str, ...]]] = {
    # The canonical detailed explanation, recipe and evidence.
    "canonical-measurement": (
        WINDOWS_PATH_LIMITS_MD,
        (
            # Both halves of the control. The physical refusal proves the tree is genuinely over the
            # ceiling; the alias open proves the MAPPING is what Desktop responded to. Either alone
            # is an anecdote.
            "| **physical root** | **273** — one `…SemanticModel\\definition\\tables\\*.tmdl` | ❌ refused, "
            "naming that file |",
            "| **same root through `subst R:`** | **253** |",
            "full refresh returned **four real rows** (`DATA_OK`, `--no-save`), stable page capture 1/1",
            "Measured 2026-09-08 (#566)",
            "This is measured **for that fixture on this host**",
        ),
    ),
    "canonical-boundary": (
        WINDOWS_PATH_LIMITS_MD,
        (
            "Machine-local and not portable.",
            "and this machine after sign-out or reboot need not have it",
            "`check_path_ceiling.py` measures the **physical** tree",
            "claim `portable` / path-safe because an alias opened it",
            "That records how\n  the file was *opened*; it is not the run's identity",
            "Never use the alias to waive a failed `check_path_ceiling.py`, a `check_unit.py` path gate "
            "or a\n  promotion destination check",
            "Never record the alias as the run's canonical path",
            "Never point `python scripts/work_dirs.py --verify` at the alias spelling",
            "Pointing a gate at the alias does not shorten what it measures.",
            '`Path("Q:/wt566docs").resolve()` → `C:\\tfmig\\wt566docs`',
        ),
    ),
    "canonical-recipe": (
        WINDOWS_PATH_LIMITS_MD,
        (
            "subst | Select-String '^R:\\\\'",
            "subst R: C:\\tfmig\\i194\\0001\\out",
            "Stop-Process -Id <literal pid> -Force\n# 5. ALWAYS remove the mapping",
            "subst R: /d",
        ),
    ),
    # One short branch plus a link - the runbook is not a second copy of the rationale.
    "runbook-branch": (
        OPERATOR_RUNBOOK_MD,
        (
            "### 4.13 Desktop refuses to open a project and names a file that is too long",
            "a required path of **273** was refused; the same output root mapped to `R:` measured **253**",
            "`check_path_ceiling.py` still measures the physical tree",
            "[`docs/windows-path-limits.md`](windows-path-limits.md) (§6",
        ),
    ),
    # One navigation row, so the recipe is reachable without knowing it exists.
    "navigation-row": (
        INDEX_MD,
        ("§6 the **measured `subst` fallback** for opening an over-ceiling PBIP in Desktop",),
    ),
}

PHRASE_CASES = [
    pytest.param(contract, phrase, id=f"{contract}-{index}")
    for contract, (_, phrases) in REQUIRED.items()
    for index, phrase in enumerate(phrases)
]


def _normalized(path: Path) -> str:
    """Collapse every run of whitespace, so a phrase survives a re-wrapped paragraph."""
    return " ".join(path.read_text(encoding="utf-8").split())


def _missing(texts: dict[Path, str]) -> list[str]:
    return [
        f"{contract}: {phrase!r} missing from {path.name}"
        for contract, (path, phrases) in REQUIRED.items()
        for phrase in phrases
        if " ".join(phrase.split()) not in texts[path]
    ]


def _texts() -> dict[Path, str]:
    return {path: _normalized(path) for path in {doc for doc, _ in REQUIRED.values()}}


def test_the_documents_carry_the_whole_contract_as_committed() -> None:
    """Positive case: permission, boundary, recipe, branch and navigation row all present."""
    assert _missing(_texts()) == []


@pytest.mark.parametrize(("contract", "phrase"), PHRASE_CASES)
def test_deleting_any_required_phrase_fails_its_named_contract(contract: str, phrase: str) -> None:
    """Mutation proof: every phrase is individually load-bearing and names what it protects.

    Without this a phrase set is only as strong as its weakest member - a duplicated or already
    unreachable phrase would sit in the table forever, credited as coverage.
    """
    doc, _ = REQUIRED[contract]
    needle = " ".join(phrase.split())
    texts = _texts()
    assert needle in texts[doc], "fixture invariant: the phrase must be present before it is deleted"
    baseline = _missing(texts)
    texts[doc] = texts[doc].replace(needle, "", 1)

    added = [failure for failure in _missing(texts) if failure not in baseline]

    assert added == [f"{contract}: {phrase!r} missing from {doc.name}"]


@pytest.mark.parametrize(("contract", "phrase"), PHRASE_CASES)
def test_every_required_phrase_occurs_exactly_once(contract: str, phrase: str) -> None:
    """A phrase present twice would make the mutation proof above vacuous for that phrase."""
    doc, _ = REQUIRED[contract]
    assert _normalized(doc).count(" ".join(phrase.split())) == 1


@pytest.mark.parametrize("doc", [AGENTS_MD, DRY_RUN_OPERATOR_MD])
def test_the_retired_absolute_prohibition_is_gone_from_the_contract_documents(doc: Path) -> None:
    """The correction replaces the old rule; it does not sit beside it.

    Measured shape of the failure this prevents: two rules on one page, one absolute and one
    conditional, and the absolute one is shorter and older.
    """
    assert RETIRED_PROHIBITION not in _normalized(doc)


def test_the_retired_prohibition_survives_only_as_a_quoted_correction() -> None:
    """The evidence page keeps the old wording ON PURPOSE - labelled as corrected, with its scope.

    An unexplained disappearance would let the same unmeasured rule be re-derived later, which is
    exactly how #566 came to exist.
    """
    text = _normalized(WINDOWS_PATH_LIMITS_MD)
    assert RETIRED_PROHIBITION in text
    assert "This section corrects an earlier unconditional prohibition." in text
    assert "The junction and symlink halves stand." in text


@pytest.mark.parametrize("doc", [AGENTS_MD, DRY_RUN_OPERATOR_MD, OPERATOR_RUNBOOK_MD])
def test_every_document_that_permits_the_alias_also_states_its_boundary(doc: Path) -> None:
    """Fail-open control: no document may grant the permission without the limit beside it.

    Judged on the same page rather than across the set, because a subagent receives ONE file.
    """
    text = _normalized(doc)
    if "`subst`" not in text:
        pytest.fail(f"{doc.name} no longer mentions the fallback at all")
    assert "waives no gate" in text or "may claim path-safety because an alias opened it" in text
    assert (
        "never the recorded path" in text
        or "never the run's recorded path" in text
        or "never the canonical path" in text
    )


def test_the_alias_is_never_offered_as_a_replacement_for_the_short_physical_root() -> None:
    """The default must stay the default: a short PHYSICAL root, allocated before the engine runs."""
    limits = _normalized(WINDOWS_PATH_LIMITS_MD)
    assert "The default has not changed" in limits
    assert "python scripts/work_dirs.py <slug> --runs-parent <short-parent> --json" in limits

    runbook = _normalized(OPERATOR_RUNBOOK_MD)
    assert "The fix is a short physical root" in runbook


def test_the_measured_fixture_named_by_the_evidence_page_exists() -> None:
    """Independent oracle: the repro the measurement cites is committed, not a remembered path."""
    fixture = REPO_ROOT / "fixtures" / "upstream-repros" / "issue-194-long-pbir-path"
    assert (fixture / "README.md").is_file()
    assert "fixtures/upstream-repros/issue-194-long-pbir-path" in _normalized(WINDOWS_PATH_LIMITS_MD)


def test_the_guard_test_named_by_the_evidence_page_exists() -> None:
    """The claim that our own gates resolve a substituted drive is cited to a real test.

    A citation to a test that does not exist reads as evidence and is not; this is the cheapest
    check that the alias-resolution claim is anchored in executable code rather than in prose.
    """
    guard = (REPO_ROOT / "tests" / "test_harvest_output_guard.py").read_text(encoding="utf-8")
    assert "def test_the_canonical_probe_is_what_lets_a_substituted_drive_through" in guard
    assert "test_the_canonical_probe_is_what_lets_a_substituted_drive_through" in _normalized(WINDOWS_PATH_LIMITS_MD)


def test_the_ceiling_gate_still_scans_the_resolved_target() -> None:
    """Independent oracle for the load-bearing sentence: the gate's own source, not the prose.

    `docs/windows-path-limits.md` tells an operator that handing `check_path_ceiling.py` an alias
    spelling does not shorten what it measures. That is only true while the CLI scans
    `target.resolve()`; if it ever scanned the spelling it was given, the documented boundary would
    silently become false and the alias WOULD look like a way to pass the gate.
    """
    source = " ".join((REPO_ROOT / "scripts" / "check_path_ceiling.py").read_text(encoding="utf-8").split())
    assert "scan( target.resolve()," in source or "scan(target.resolve()," in source


@pytest.mark.skipif(os.name != "nt", reason="`subst` is Windows-only")
def test_resolving_a_substituted_drive_returns_the_physical_path() -> None:
    """The measurement the page cites, re-run: `resolve()` sees through `subst`.

    This is the difference between "the gate resolves its argument" (true, above) and "resolving is
    enough" (the claim that matters). Both halves are needed: a resolver that returned the alias
    spelling would satisfy the source check and still leave the artifact judged as if it were short.
    """
    letter = next((candidate for candidate in "ZYXWVUT" if not Path(f"{candidate}:\\").exists()), None)
    if letter is None:
        pytest.skip("no free drive letter available for subst")
    target = REPO_ROOT
    made = subprocess.run(["subst", f"{letter}:", str(target)], capture_output=True, text=True, check=False, shell=True)
    if made.returncode != 0:
        pytest.skip(f"subst failed: {made.stdout.strip()} {made.stderr.strip()}")
    try:
        aliased = Path(f"{letter}:\\docs\\windows-path-limits.md")
        assert aliased.exists(), "fixture invariant: the alias must expose the tree"
        assert aliased.resolve() == (target / "docs" / "windows-path-limits.md").resolve()
    finally:
        subprocess.run(["subst", f"{letter}:", "/d"], capture_output=True, text=True, check=False, shell=True)
