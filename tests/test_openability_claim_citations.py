"""Pin the honest framing of the engine's ``openability_selfcheck`` claim in the instruction surface.

Issue #312. ``openability_selfcheck`` ships in the **engine's** ``report.json``; it is not a check
this repo runs. Our failure mode is citing it as if it were verification. Measured on
``_runs/estate-2.339.0-20260829/report.json``: ``ok: true`` on **30 of 44** workbooks that the same
file records defects for, ``issues[]`` empty on 43 of 44. An agent reading ``ok`` as "this model is
fine" is reading a false green, and a false green in a persona converts a defect into a sign-off.

**This file's first version was itself a false green, and the shape is worth keeping.** It scanned two
hard-coded personas and accepted any block containing a word like ``claim`` or ``narrow``. A reviewer
replaced the validator's qualified paragraph with

    `openability_selfcheck.ok` is the engine claim and authoritative verified proof that the model
    opens.

and **both assertions still passed** -- "claim" was present, so the sentence qualified itself while
saying the opposite. A positive word list cannot express this invariant. Two rules now do:

1. every citing block must carry an explicit **negative** assertion (``DISCLAIMERS``), and
2. no citing block may carry authority wording (``OVERCLAIMS``) -- which fails a block *regardless* of
   how many disclaimers sit beside it, because that is exactly the sentence above.

Blocks are isolated to a single table row or list item, so a qualified neighbour cannot vouch for a
bare citation; and every ``.github/agents/*.agent.md`` is scanned dynamically, because hard-coding two
of four left a new bare citation in the other two invisible.

The skill bundle is pinned by heading only. It cites the field twice as an *indictment* (a model whose
every decimal was inflated 493x while the selfcheck reported ``ok: true``), and a matcher tuned to
accept those would be loose enough to accept anything.

Anti-vacuity is the job of ``test_the_scan_finds_the_known_citations``: a prose scanner whose regex
stops matching passes silently, which is the same defect class the issue reports.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

FIELD = "openability_selfcheck"

AGENTS_DIR = REPO / ".github" / "agents"

# The skill bundle that owns the full breakdown (scope, measured false-green rate, which ``checks``
# omissions are vacuous and which are genuine, and how far the TMDL oracle actually gets).
GOTCHAS_SKILL = ".github/skills/powerbi-semantic-model-gotchas/SKILL.md"
GOTCHAS_HEADING = "### `openability_selfcheck.ok` is the engine's CLAIM, not a verified open"

# The generated region; a citation inside it comes from AGENTS.md, and editing the persona is wrong.
SYNC_BEGIN = "<!-- BEGIN:shared-conventions -->"
SYNC_END = "<!-- END:shared-conventions -->"

# A block must contain at least one of these. They are NEGATIVE assertions -- statements of what the
# field does not establish -- not merely words that tend to appear near honest prose. That distinction
# is the whole fix: "claim" and "narrow" both appear in a sentence calling the field authoritative.
DISCLAIMERS = (
    re.compile(r"n(?:ot|on)[-\s]exhaustive", re.IGNORECASE),
    re.compile(r"not a verified open", re.IGNORECASE),
    re.compile(r"\b(?:not|never)\b[^.\n]{0,40}\bverif(?:ication|ied|y)", re.IGNORECASE),
    re.compile(r"does not prove|proves? nothing", re.IGNORECASE),
    re.compile(r"says nothing about", re.IGNORECASE),
    re.compile(r"never passed", re.IGNORECASE),
    re.compile(r"never cite it", re.IGNORECASE),
    re.compile(r"not an openability proof", re.IGNORECASE),
    re.compile(r"\bblind to\b", re.IGNORECASE),
    re.compile(r"necessary,? not sufficient", re.IGNORECASE),
)

# A block containing any of these FAILS, however many disclaimers accompany it. A hedge does not
# cancel an assertion of authority; the reviewer's bypass sentence carried both.
OVERCLAIMS = (
    re.compile(r"\bauthoritative\b", re.IGNORECASE),
    re.compile(r"verified proof", re.IGNORECASE),
    re.compile(r"proof that (?:the |this |a )?model opens", re.IGNORECASE),
    re.compile(r"proves?(?: that)? (?:the |this |a )?model opens", re.IGNORECASE),
    re.compile(r"\btrust it\b", re.IGNORECASE),
    re.compile(r"\bdefinitive\b", re.IGNORECASE),
    re.compile(r"\bguarantee", re.IGNORECASE),
)

# Personas that must still cite the field, and how many blocks each must keep. A floor, not an
# equality: adding a qualified citation is fine, losing one is a deliberate pin change.
MINIMUM_CITATIONS = {
    "pbi-migration-validator.agent.md": 2,
    "pbi-semantic-builder.agent.md": 2,
}

# A markdown list item: the unit a bullet's qualifier is allowed to cover.
LIST_MARKER = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")


def personas() -> list[Path]:
    """Every persona on disk, discovered at run time -- never a hard-coded subset."""
    return sorted(AGENTS_DIR.glob("*.agent.md"))


def _boundary(line: str) -> bool:
    """True where a block must not be extended across."""
    return not line.strip() or line.lstrip().startswith("|")


def citing_blocks(text: str) -> list[tuple[int, str]]:
    """Return ``(1-based line number, block text)`` for every block that names the field.

    A block is one table row, one list item, or one plain paragraph -- never more. Widening it to the
    surrounding prose is what lets a qualified neighbour vouch for a bare citation, and a persona's
    field glossary is a table while its rules are a bullet list, so both units matter.
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
        while start > 0 and not LIST_MARKER.match(lines[start]) and not _boundary(lines[start - 1]):
            start -= 1
        end = index
        while end + 1 < len(lines) and not _boundary(lines[end + 1]) and not LIST_MARKER.match(lines[end + 1]):
            end += 1
        blocks.append((index + 1, "\n".join(lines[start : end + 1])))
    return blocks


def findings(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, reason)`` for each citing block that fails either rule."""
    out: list[tuple[int, str]] = []
    for line_number, block in citing_blocks(text):
        hit = next((p.pattern for p in OVERCLAIMS if p.search(block)), None)
        if hit:
            out.append((line_number, f"claims authority for the field (matched /{hit}/)"))
        elif not any(pattern.search(block) for pattern in DISCLAIMERS):
            out.append((line_number, "no explicit statement of what the field does NOT establish"))
    return out


def _inside_generated_block(text: str, line_number: int) -> bool:
    before = "\n".join(text.splitlines()[: line_number - 1])
    return before.count(SYNC_BEGIN) > before.count(SYNC_END)


@pytest.mark.parametrize("persona", personas(), ids=lambda p: p.name)
def test_every_persona_citation_is_qualified(persona: Path) -> None:
    """A persona may cite the field, but never bare -- bare is how a claim becomes evidence."""
    text = persona.read_text(encoding="utf-8")
    problems = findings(text)
    generated = [line for line, _ in problems if _inside_generated_block(text, line)]
    detail = "; ".join(f"line {line}: {reason}" for line, reason in problems)
    hint = (
        f"Line(s) {generated} are inside {SYNC_BEGIN} -- fix AGENTS.md and re-run "
        "scripts/sync_agent_conventions.py, never the persona copy. "
        if generated
        else ""
    )
    assert not problems, (
        f"{persona.name} cites `{FIELD}` without saying what it is not -- {detail}. It is the "
        "ENGINE's claim about its own build: a static scan of model TMDL text, blind to the report "
        "and to data, which shipped `ok: true` on 30 of 44 workbooks the same report.json recorded "
        "defects for (2.339.0). And no static gate settles openability -- the TMDL oracle is the "
        "mandatory parser-level gate and is still necessary-not-sufficient (duplicate measure names "
        f"deserialize clean and Desktop refuses to open). {hint}See {GOTCHAS_SKILL} section 8."
    )


@pytest.mark.parametrize("name, expected", sorted(MINIMUM_CITATIONS.items()))
def test_the_scan_finds_the_known_citations(name: str, expected: int) -> None:
    """Fail loudly if the citations vanish, so the scan can never pass by matching nothing.

    Without this, deleting or rewording a citation makes ``findings()`` return an empty list and the
    qualifier test green -- coverage lost silently, which is the shape of the defect being pinned.
    """
    text = (AGENTS_DIR / name).read_text(encoding="utf-8")
    found = len(citing_blocks(text))
    assert found >= expected, (
        f"{name} now has {found} `{FIELD}` citation(s), below the pinned floor of {expected}. If the "
        "citation was removed on purpose, lower the floor in this test in the same commit; do not let "
        "the scan go quiet."
    )


def test_the_persona_scan_covers_every_agent_file() -> None:
    """The scan is discovered from disk; a hard-coded subset is how two personas went unexamined."""
    found = {path.name for path in personas()}
    assert found >= set(MINIMUM_CITATIONS), f"personas missing from {AGENTS_DIR}: {sorted(found)}"
    assert len(found) >= 4, f"expected every persona to be discovered, found {sorted(found)}"


def test_the_gotchas_skill_owns_the_full_breakdown() -> None:
    """The uncapped bundle, not a near-cap persona, is where the measured detail lives."""
    text = (REPO / GOTCHAS_SKILL).read_text(encoding="utf-8")
    assert GOTCHAS_HEADING in text, (
        f"{GOTCHAS_SKILL} no longer carries the section heading pinned by issue #312:\n"
        f"  {GOTCHAS_HEADING}\n"
        "That section is what the personas point at instead of restating the detail inside a "
        "30,000-char cap. Restore it, or repoint the personas in the same commit."
    )


# --------------------------------------------------------------------------------------------------
# Adversarial fixtures. Each is a sentence a positive-word-list scanner accepted, or would have.
# --------------------------------------------------------------------------------------------------


def test_the_reviewers_bypass_sentence_is_rejected() -> None:
    """The exact text that defeated this file's first version. If it passes, the test is vacuous."""
    bypass = "`openability_selfcheck.ok` is the engine claim and authoritative verified proof that the model opens.\n"
    assert [reason for _, reason in findings(bypass)] == [
        "claims authority for the field (matched /\\bauthoritative\\b/)"
    ]


def test_a_hedge_does_not_cancel_an_assertion_of_authority() -> None:
    """Disclaimer AND overclaim in one block still fails -- the overclaim is what an agent acts on."""
    hedged = (
        "`openability_selfcheck` is a narrow check whose `checks` map is not exhaustive, but `ok` is authoritative.\n"
    )
    assert len(findings(hedged)) == 1


def test_the_old_positive_word_list_is_not_enough_on_its_own() -> None:
    """`claim` and `narrow` are both present here, and the sentence is still wrong."""
    smuggled = "A narrow claim: `openability_selfcheck.ok` proves the model opens.\n"
    assert len(findings(smuggled)) == 1


def test_a_bare_citation_is_detected() -> None:
    """No overclaim, no disclaimer -- the plain omission case."""
    bare = "Read `openability_selfcheck` from the handover slice and act on it.\n"
    assert [line for line, _ in findings(bare)] == [1]


def test_the_honest_framing_is_accepted() -> None:
    """...and the corrected wording must pass, or it would be routed around rather than obeyed."""
    honest = (
        "`openability_selfcheck` is the engine's claim about its own build, not a verified open;\n"
        "its `checks` map is not exhaustive and `ok` says nothing about bindings or data.\n"
    )
    assert findings(honest) == []


def test_a_table_row_cannot_borrow_a_neighbours_qualifier() -> None:
    """Block extraction stops at a table row; a glossary table is a likely citation site."""
    table = (
        "| field | meaning |\n"
        "|---|---|\n"
        "| `viz_fidelity` | one row per worksheet -- says nothing about rendering |\n"
        "| `openability_selfcheck` | read it and move on |\n"
    )
    assert [line for line, _ in findings(table)] == [4]


def test_a_bullet_cannot_borrow_the_previous_bullets_qualifier() -> None:
    """The hole a paragraph-level block leaves: an adjacent qualified bullet vouching for a bare one."""
    bullets = (
        "- **`viz_fidelity`** is the engine's account and says nothing about what rendered.\n"
        "- **`openability_selfcheck`** tells you the model is fine.\n"
    )
    assert [line for line, _ in findings(bullets)] == [2]


def test_a_reflowed_bullet_is_read_whole() -> None:
    """A qualifier may sit on a continuation line of the same bullet."""
    reflowed = (
        "- Treat every field in the handover slice as a claim, `openability_selfcheck` included:\n"
        "  it says nothing about bindings, filters or data.\n"
    )
    assert findings(reflowed) == []


def test_a_plain_paragraph_walks_back_to_its_first_line() -> None:
    """The qualifier may PRECEDE the citing line — the validator's numbered input list is this shape.

    Distinct from the bullet case above, which starts at the marker and so never walks backwards.
    Without this, breaking the backward walk leaves every test green (measured: it survived once).
    """
    reflowed = (
        "Every field in the handover slice says nothing about what actually rendered,\n"
        "`openability_selfcheck` included.\n"
    )
    assert findings(reflowed) == []
