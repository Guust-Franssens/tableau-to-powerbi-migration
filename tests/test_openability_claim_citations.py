"""Pin the reviewed wording of every ``openability_selfcheck`` citation in the instruction surface.

Issue #312. ``openability_selfcheck`` ships in the **engine's** ``report.json``; it is not a check
this repo runs. Our failure mode is citing it as if it were verification. Measured on
``_runs/estate-2.339.0-20260829/report.json``: ``ok: true`` on **30 of 44** workbooks that the same
file records defects for. An agent reading ``ok`` as "this model is fine" is reading a false green,
and a false green in a persona converts a defect into a sign-off.

WHY THIS IS A PIN AND NOT A CLASSIFIER
--------------------------------------
Three earlier versions of this file tried to *classify* prose as honest or not, and each was defeated
by a synonym the previous round had not listed:

===  ==========================================  ==============================================
 #   mechanism                                   sentence that defeated it
===  ==========================================  ==============================================
 1   positive word list (``claim``/``narrow``)   "the engine claim and authoritative verified
                                                 proof that the model opens"
 2   + a denylist of authority words             "is not exhaustive, but `ok` confirms the model
     (``authoritative``, ``verified proof``…)    will open" -- and equally "conclusively
                                                 establishes", "certifies", "sufficient for
                                                 openability sign-off"
===  ==========================================  ==============================================

Each round improved the classifier; each round it lost, because **the set of ways to assert something
in English is unbounded** and a denylist is a false-negative surface by construction. Measured: 4 of 5
reviewer sentences passed round 2 cleanly, and the 5th only failed incidentally.

So the mechanism changed instead of growing. **Every citing block is pinned by the SHA-256 of its
whitespace-normalized text.** That converts an undecidable classification into a decidable comparison:
a block either is the reviewed one or it is not. Nothing here reads the prose's meaning, and there is
no list to extend.

**The cost is real and intended.** Rewording a citation -- even improving it -- turns this test red
until the pin is updated in the same commit. That is the "deliberately approved" step: the pin diff
says *someone looked*, and the persona diff beside it is what they looked at. The failure message
prints the exact replacement line, so the fix is a paste, not an investigation.

Normalization is **whitespace only**. Re-wrapping a paragraph to a different line width is free; every
word change is a pin change. That is the intended boundary between "reflowed" and "materially
rewritten".

The skill bundle is pinned by heading. It cites the field twice as an *indictment* (a model whose
every decimal was inflated 493x while the selfcheck reported ``ok: true``), and those citations are
prose that should stay free to evolve.
"""

from __future__ import annotations

import hashlib
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

# What starts a new block, inclusively -- the marker belongs to the block it opens. Footnote
# definitions are here because two adjacent ones merged into a single block, which let a bare citation
# sit inside a neighbour's reviewed text and inherit its pin.
BLOCK_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\[\^[^\]]*\]:)")

# A thematic break: a boundary, not the start of anything.
THEMATIC_BREAK = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")

# A heading is EXCLUSIVE: prose beneath it is its own block, or every retitle would demand a pin
# update on a citation nobody touched.
HEADING = re.compile(r"^\s*#{1,6}\s")

# ---------------------------------------------------------------------------------------------
# THE PIN. Each entry is (sha256 of the whitespace-normalized block, an ASCII excerpt for humans).
# The excerpt is a label only -- never compared. Regenerate an entry from this test's own failure
# message; review the accompanying persona diff, which is the actual artifact under review.
# ---------------------------------------------------------------------------------------------
PINNED: dict[str, tuple[tuple[str, str], ...]] = {
    "pbi-migration-validator.agent.md": (
        (
            "a6620ad81ffa13743dbdb9d3cd539d64eff098ef0cb882887928ed2342ffd1cc",
            "1. **`handover/<workbook>.json`** ~ the deterministic tier's own **claims** about what i",
        ),
        (
            "7ab23314f245229d3dfa506d3ce92cd91a912281b9bc4440cda5e85c5ad7e144",
            "- **`openability_selfcheck.ok` is a claim too ~ the narrowest in the file. Adjudicate it",
        ),
    ),
    "pbi-semantic-builder.agent.md": (
        (
            "31717b00b8d91479b4cc7f9ca14e35cbd6a979195a1d0632bace46dfb4b90db1",
            "| ~ `openability_selfcheck` | a narrow structural self-check against the engine's own pa",
        ),
        (
            "927601b08a9e290a1f093a4a03c171a2dabbf52aaf4b11a64f69b3ff554f9805",
            "1. **Read the queue with `python scripts/read_handover.py <bundle> --workbook <name>`**,",
        ),
    ),
}


def personas() -> list[Path]:
    """Every persona on disk, discovered at run time -- never a hard-coded subset."""
    return sorted(AGENTS_DIR.glob("*.agent.md"))


def _boundary(line: str) -> bool:
    """True where a block must not be extended across."""
    stripped = line.lstrip()
    return (
        not line.strip()
        or stripped.startswith("|")
        or stripped.startswith("<!--")
        or bool(THEMATIC_BREAK.match(line))
        or bool(HEADING.match(line))
    )


def normalize(block: str) -> str:
    """Collapse every whitespace run to one space. Reflowing is free; a word change is not."""
    return " ".join(block.split())


def digest(block: str) -> str:
    """The pinned identity of a citation block."""
    return hashlib.sha256(normalize(block).encode("utf-8")).hexdigest()


def excerpt(block: str) -> str:
    """A short ASCII label for a pin entry. Never compared -- only printed."""
    text = normalize(block)
    return "".join(char if 32 <= ord(char) < 127 else "~" for char in text)[:88]


def citing_blocks(text: str) -> list[tuple[int, str]]:
    """Return ``(1-based line number, block text)`` for every block that names the field.

    A block is one table row, one list item, one footnote definition, or one plain paragraph -- never
    more. Isolation matters for the pin because a wide block folds unrelated neighbouring prose into
    the hash, so an edit far from the citation would demand a pin update it has nothing to do with.
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
        while start > 0 and not BLOCK_START.match(lines[start]) and not _boundary(lines[start - 1]):
            start -= 1
        end = index
        while end + 1 < len(lines) and not _boundary(lines[end + 1]) and not BLOCK_START.match(lines[end + 1]):
            end += 1
        blocks.append((index + 1, "\n".join(lines[start : end + 1])))
    return blocks


def unpinned(name: str, text: str) -> list[tuple[int, str]]:
    """Citing blocks in ``text`` whose wording is not in ``name``'s reviewed pin."""
    approved = {sha for sha, _ in PINNED.get(name, ())}
    return [(line, block) for line, block in citing_blocks(text) if digest(block) not in approved]


def missing(name: str, text: str) -> list[tuple[str, str]]:
    """Pin entries no longer found in ``text`` -- a citation deleted, reworded or moved."""
    present = {digest(block) for _, block in citing_blocks(text)}
    return [entry for entry in PINNED.get(name, ()) if entry[0] not in present]


def _inside_generated_block(text: str, line_number: int) -> bool:
    before = "\n".join(text.splitlines()[: line_number - 1])
    return before.count(SYNC_BEGIN) > before.count(SYNC_END)


@pytest.mark.parametrize("persona", personas(), ids=lambda p: p.name)
def test_every_persona_citation_is_pinned(persona: Path) -> None:
    """A citation may say anything a reviewer approved, and nothing a reviewer has not seen."""
    text = persona.read_text(encoding="utf-8")
    findings = unpinned(persona.name, text)
    generated = [line for line, _ in findings if _inside_generated_block(text, line)]
    paste = "\n".join(f'        ("{digest(block)}",\n         "{excerpt(block)}"),' for _, block in findings)
    hint = (
        f"\n\nLine(s) {generated} are inside {SYNC_BEGIN}: fix AGENTS.md and re-run "
        "scripts/sync_agent_conventions.py, never the persona copy."
        if generated
        else ""
    )
    assert not findings, (
        f"{persona.name} line(s) {[line for line, _ in findings]}: this `{FIELD}` citation is not the "
        "reviewed wording.\n\nThat is the intended behaviour, not a bug in this test -- the field is "
        "the ENGINE's claim about its own build (a static scan of model TMDL text, blind to the report "
        "and to data, `ok: true` on 30 of 44 workbooks the same report.json recorded defects for), so "
        "every wording that instructs an agent about it is reviewed once and then pinned. Earlier "
        "versions tried to judge the prose instead and lost to a synonym three times running.\n\n"
        "TO FIX: if you rewrote this deliberately, replace the stale entry under "
        f'PINNED["{persona.name}"] in this file with:\n\n{paste}\n\n'
        "...in the SAME commit, so the persona diff beside it is what a reviewer reads. If you did "
        f"not mean to change it, revert the persona. See {GOTCHAS_SKILL} section 8.{hint}"
    )


@pytest.mark.parametrize("name", sorted(PINNED), ids=str)
def test_every_pin_is_still_present(name: str) -> None:
    """Fail loudly if a pinned citation vanishes, so coverage can never be lost quietly.

    Exact, not a count: rewording a citation trips ``test_every_persona_citation_is_pinned``, while
    deleting or moving one trips only this. A scanner that stops matching is the same defect class the
    issue reports, one level up.
    """
    text = (AGENTS_DIR / name).read_text(encoding="utf-8")
    gone = missing(name, text)
    assert not gone, (
        f"{name} no longer contains {len(gone)} pinned `{FIELD}` citation(s):\n"
        + "\n".join(f"  - {label}" for _, label in gone)
        + "\n\nIf the citation was removed on purpose, delete its entry from PINNED in the same commit; "
        "do not let the coverage go quiet."
    )


def test_the_persona_scan_covers_every_agent_file() -> None:
    """The scan is discovered from disk; a hard-coded subset is how two personas went unexamined."""
    found = {path.name for path in personas()}
    assert found >= set(PINNED), f"pinned personas missing from {AGENTS_DIR}: {sorted(set(PINNED) - found)}"
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


# ----------------------------------------------------------------------------------------------
# Adversarial fixtures. Every sentence here defeated a previous mechanism; each must now be rejected
# for a STRUCTURAL reason (nobody reviewed this wording), never a lexical one.
# ----------------------------------------------------------------------------------------------

DEFEATED_EARLIER_MECHANISMS = (
    # round 1: a positive word list accepted anything containing "claim"
    "`openability_selfcheck.ok` is the engine claim and authoritative verified proof that the model opens.",
    # round 2: a denylist of authority words, defeated by four synonyms it did not list
    "`openability_selfcheck` is not exhaustive, but `ok` confirms the model will open.",
    "`openability_selfcheck` is not exhaustive; `ok` conclusively establishes openability.",
    "`openability_selfcheck` is not exhaustive, and `ok` certifies the model.",
    "`openability_selfcheck` says nothing about data, but `ok` is sufficient for openability sign-off.",
    "`openability_selfcheck` is not exhaustive; rely on `ok` as the openability verdict.",
)


@pytest.mark.parametrize("sentence", DEFEATED_EARLIER_MECHANISMS, ids=lambda s: s[:44])
def test_a_sentence_that_defeated_an_earlier_mechanism_is_rejected(sentence: str) -> None:
    """None of these is pinned, so none passes -- regardless of which words it happens to contain."""
    assert unpinned("pbi-migration-validator.agent.md", sentence + "\n")


def test_the_real_reviewed_wording_is_accepted() -> None:
    """The pin must accept exactly what is on disk, or it is unusable rather than strict."""
    for name in PINNED:
        text = (AGENTS_DIR / name).read_text(encoding="utf-8")
        assert unpinned(name, text) == [], name


def _validator_rule_block() -> str:
    """The validator's pinned rule bullet, selected by content rather than by index."""
    text = (AGENTS_DIR / "pbi-migration-validator.agent.md").read_text(encoding="utf-8")
    return next(block for _, block in citing_blocks(text) if "is a claim too" in block)


def test_reflowing_a_pinned_block_does_not_break_the_pin() -> None:
    """Whitespace-only normalization: re-wrapping is free, so the pin does not punish formatting."""
    block = _validator_rule_block()
    assert digest(block) == digest(block.replace("\n", "\n   ").replace(" ", "  "))


def test_changing_one_word_of_a_pinned_block_breaks_the_pin() -> None:
    """...and the other half of that boundary: prose changes are exactly what must be re-reviewed."""
    block = _validator_rule_block()
    assert "narrowest" in block
    assert digest(block) != digest(block.replace("narrowest", "broadest"))


def test_a_deleted_citation_is_reported_by_the_presence_check() -> None:
    """``missing()`` is what notices a citation REMOVED rather than reworded.

    Separate from the pin comparison, and it needs its own fixture: with only the live assertion,
    stubbing ``missing()`` to return nothing leaves every test green (measured: it survived once).
    """
    name = "pbi-migration-validator.agent.md"
    assert len(missing(name, "no citations here at all\n")) == len(PINNED[name])
    assert missing(name, (AGENTS_DIR / name).read_text(encoding="utf-8")) == []


def test_a_footnote_cannot_borrow_a_neighbouring_footnotes_pin() -> None:
    """Two adjacent footnote definitions merged into one block, which inherited the neighbour's text."""
    footnotes = (
        "[^a]: `viz_fidelity` is the engine's account and says nothing about what rendered.\n"
        "[^b]: `openability_selfcheck` tells you the model is fine.\n"
    )
    assert [line for line, _ in citing_blocks(footnotes)] == [2]
    assert citing_blocks(footnotes)[0][1].startswith("[^b]:")


def test_a_table_row_is_its_own_block() -> None:
    """A glossary table is a likely citation site, and each row is reviewed on its own."""
    table = (
        "| field | meaning |\n"
        "|---|---|\n"
        "| `viz_fidelity` | one row per worksheet |\n"
        "| `openability_selfcheck` | read it and move on |\n"
    )
    assert [line for line, _ in citing_blocks(table)] == [4]
    assert citing_blocks(table)[0][1].startswith("| `openability_selfcheck`")


def test_a_bullet_is_its_own_block() -> None:
    """A bullet list is one block per item, so a pin covers one rule rather than a whole section."""
    bullets = (
        "- **`viz_fidelity`** is the engine's account of what it rendered.\n"
        "- **`openability_selfcheck`** tells you the model is fine.\n"
    )
    assert citing_blocks(bullets)[0][1].startswith("- **`openability_selfcheck`**")


def test_a_heading_ends_the_preceding_block() -> None:
    """Prose under a heading must not absorb the heading, or every retitle becomes a pin update."""
    text = "## Reading the handover\n`openability_selfcheck` is one field of it.\n"
    assert citing_blocks(text)[0][1] == "`openability_selfcheck` is one field of it."


def test_a_plain_paragraph_walks_back_to_its_first_line() -> None:
    """The citing line may be a continuation -- the validator's numbered input list is that shape."""
    paragraph = "Every field in the handover slice is a claim,\n`openability_selfcheck` included.\n"
    assert citing_blocks(paragraph)[0][1].startswith("Every field")
