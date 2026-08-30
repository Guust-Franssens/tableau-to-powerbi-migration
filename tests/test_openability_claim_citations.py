"""Pin the honest framing of the engine's ``openability_selfcheck`` claim in the instruction surface.

Issue #312. ``openability_selfcheck`` is the **engine's** claim, shipped in its ``report.json``; it is
not a check this repo runs. Our failure mode is citing it as if it were verification. Measured on
``_runs/estate-2.339.0-20260829/report.json``: ``ok: true`` on **30 of 44** workbooks that the same
file records defects for, ``issues[]`` empty on 43 of 44. An agent reading ``ok`` as "this model is
fine" is reading a false green, and a false green in a persona converts a defect into a sign-off.

So this is a **pinning** test, deliberately narrow:

* Personas are scanned generally -- **every** block that cites ``openability_selfcheck`` must carry a
  qualifier. Personas are the instruction surface where the risk lives, and today there is no site
  there that legitimately cites the field bare, so the scan needs no exemption list.
* The skill bundle is pinned by heading only. It cites the field twice as an *indictment* (a model
  whose every decimal was inflated 493x while the selfcheck reported ``ok: true``), and a
  natural-language "is this citation critical enough" matcher would either false-flag those or be
  loosened until it matched anything. A heading pin cannot rot into a no-op.

Anti-vacuity is the point of ``test_the_scan_finds_the_known_citations``: a prose scanner whose regex
stops matching passes silently, which is the same defect class the issue reports. If the wording
moves, this test fails and the pin is updated **deliberately**.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

FIELD = "openability_selfcheck"

# Personas that instruct an agent about the engine's handover payload. Both cite the field today;
# a persona that stops citing it fails ``test_the_scan_finds_the_known_citations`` rather than
# quietly dropping out of coverage.
POLICED_PERSONAS = (
    ".github/agents/pbi-migration-validator.agent.md",
    ".github/agents/pbi-semantic-builder.agent.md",
)

# The skill bundle that owns the full breakdown (scope, measured false-green rate, which ``checks``
# omissions are vacuous and which are genuine, and the only offline proof a model opens).
GOTCHAS_SKILL = ".github/skills/powerbi-semantic-model-gotchas/SKILL.md"
GOTCHAS_HEADING = "### `openability_selfcheck.ok` is the engine's CLAIM, not a verified open"

# Any ONE of these in the citing block is enough. They are the ways this repo says "this is a claim,
# not a verification" -- not a spelling list for one sentence, so a rewrite that keeps the meaning
# keeps passing. ``n(ot|on)-exhaustive`` covers both forms already in use.
QUALIFIERS = (
    re.compile(r"n(?:ot|on)[-\s]exhaustive", re.IGNORECASE),
    re.compile(r"\bclaim", re.IGNORECASE),
    re.compile(r"\bnarrow", re.IGNORECASE),
    re.compile(r"not a verified open", re.IGNORECASE),
    re.compile(r"says nothing", re.IGNORECASE),
    re.compile(r"not evaluated", re.IGNORECASE),
    re.compile(r"blind to", re.IGNORECASE),
)

# The minimum number of citing blocks each policed persona must still contain. Deliberately a floor,
# not an equality: adding a qualified citation is fine, losing one is a pin change.
MINIMUM_CITATIONS = {
    ".github/agents/pbi-migration-validator.agent.md": 2,
    ".github/agents/pbi-semantic-builder.agent.md": 2,
}


def citing_blocks(text: str) -> list[tuple[int, str]]:
    """Return ``(1-based line number, block text)`` for every block that names the field.

    A markdown table row is its own block -- a persona's field glossary is a table, and swallowing
    neighbouring rows would let an adjacent row's qualifier vouch for an unqualified one. Otherwise
    the block is the enclosing paragraph (the run of contiguous non-blank lines), which is what
    survives reflowing prose to a different line width.
    """
    lines = text.splitlines()
    blocks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if FIELD not in line:
            continue
        if line.lstrip().startswith("|"):
            blocks.append((index + 1, line))
            continue
        start = index
        while start > 0 and lines[start - 1].strip() and not lines[start - 1].lstrip().startswith("|"):
            start -= 1
        end = index
        while end + 1 < len(lines) and lines[end + 1].strip() and not lines[end + 1].lstrip().startswith("|"):
            end += 1
        blocks.append((index + 1, "\n".join(lines[start : end + 1])))
    return blocks


def unqualified(text: str) -> list[tuple[int, str]]:
    """Return the citing blocks carrying no qualifier at all."""
    return [
        (line_number, block)
        for line_number, block in citing_blocks(text)
        if not any(pattern.search(block) for pattern in QUALIFIERS)
    ]


@pytest.mark.parametrize("relative_path", POLICED_PERSONAS)
def test_every_persona_citation_is_qualified(relative_path: str) -> None:
    """A persona may cite the field, but never bare -- bare is how a claim becomes evidence."""
    text = (REPO / relative_path).read_text(encoding="utf-8")
    findings = unqualified(text)
    assert not findings, (
        f"{relative_path} cites `{FIELD}` with no qualifier at line(s) "
        f"{[line for line, _ in findings]}. It is the ENGINE's claim about its own build -- a static "
        "scan of model TMDL text, blind to the report and to data, which shipped `ok: true` on 30 of "
        "44 workbooks the same report.json recorded defects for (2.339.0). Say what it is, or the "
        f"next reader signs off on it. See {GOTCHAS_SKILL} section 8."
    )


@pytest.mark.parametrize("relative_path", POLICED_PERSONAS)
def test_the_scan_finds_the_known_citations(relative_path: str) -> None:
    """Fail loudly if the citations vanish, so the scan can never pass by matching nothing.

    Without this, deleting or rewording the citation makes ``unqualified()`` return an empty list and
    the qualifier test green -- coverage lost silently, which is the exact shape of the defect being
    pinned here.
    """
    text = (REPO / relative_path).read_text(encoding="utf-8")
    found = len(citing_blocks(text))
    expected = MINIMUM_CITATIONS[relative_path]
    assert found >= expected, (
        f"{relative_path} now has {found} `{FIELD}` citation(s), below the pinned floor of "
        f"{expected}. If the citation was removed on purpose, lower the floor in this test in the "
        "same commit; do not let the scan go quiet."
    )


def test_the_gotchas_skill_owns_the_full_breakdown() -> None:
    """The uncapped bundle, not a near-cap persona, is where the measured detail lives."""
    text = (REPO / GOTCHAS_SKILL).read_text(encoding="utf-8")
    assert GOTCHAS_HEADING in text, (
        f"{GOTCHAS_SKILL} no longer carries the section heading pinned by issue #312:\n"
        f"  {GOTCHAS_HEADING}\n"
        "That section is what the personas point at instead of restating the detail inside a "
        "30,000-char cap. Restore it, or repoint the personas in the same commit."
    )


def test_an_unqualified_citation_is_detected() -> None:
    """The scanner must fire on a bare citation -- otherwise the gate above is decorative."""
    bare = "Read `openability_selfcheck` from the handover slice and confirm the model is fine.\n"
    assert [line for line, _ in unqualified(bare)] == [1]


def test_a_qualified_citation_is_accepted() -> None:
    """...and must not fire on the honest framing, or it would be routed around rather than obeyed."""
    honest = (
        "`openability_selfcheck` is the engine's claim about its own build, not a verified open;\n"
        "its `checks` map is not exhaustive.\n"
    )
    assert unqualified(honest) == []


def test_a_table_row_cannot_borrow_a_neighbours_qualifier() -> None:
    """Block extraction stops at a table row; a glossary table is the likeliest citation site."""
    table = (
        "| field | meaning |\n"
        "|---|---|\n"
        "| `viz_fidelity` | one row per worksheet -- the engine's claim, not exhaustive |\n"
        "| `openability_selfcheck` | read it and move on |\n"
    )
    assert [line for line, _ in unqualified(table)] == [4]


def test_a_reflowed_paragraph_is_read_whole() -> None:
    """The qualifier may sit on a different line of the same paragraph than the field name."""
    reflowed = (
        "Treat every field in the handover slice as a claim to adjudicate,\n"
        "`openability_selfcheck` very much included.\n"
    )
    assert unqualified(reflowed) == []
