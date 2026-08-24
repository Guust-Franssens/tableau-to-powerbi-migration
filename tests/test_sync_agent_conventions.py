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

from __future__ import annotations

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


def test_this_repo_is_not_currently_in_the_band() -> None:
    """No monkeypatching. If this fails, the warning is doing its job - buy headroom, do not delete
    this test. Shipped sizes were 27,414-28,414 when the band was introduced.
    """
    assert sac.near_cap(sorted((REPO_ROOT / ".github" / "agents").glob("*.agent.md"))) == []
