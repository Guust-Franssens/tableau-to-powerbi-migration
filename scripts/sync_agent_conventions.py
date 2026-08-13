"""
purpose: Copy the shared agent conventions from AGENTS.md into every .github/agents/*.agent.md,
         because a custom-agent subagent receives ONLY its own persona file.
usage:   python scripts/sync_agent_conventions.py          # write the block into every agent
         python scripts/sync_agent_conventions.py --check  # CI gate: exit 1 if any agent has drifted
         python scripts/sync_agent_conventions.py --check --bundle <engine-bundle>
                                                           # ...and resolve the documented paths on disk

Why this exists (measured, not assumed)
---------------------------------------
`AGENTS.md` calls itself "conventions every agent inherits, so the individual `.github/agents/*.agent.md`
files stay lean and don't restate them". That premise is false for subagents.

Verified 2026-07-30 with a sentinel experiment (a fixture AGENTS.md carrying unique tokens plus a
probe persona):

    invoked as a ROOT session   (`copilot --agent=probe`)  -> AGENTS.md sentinels PRESENT
    invoked as a SUBAGENT       (via the Task tool)        -> AGENTS.md sentinels ABSENT

All four real agents independently confirmed it; `pbi-semantic-builder` reported that "AGENTS.md"
appears in its context exactly once - as a filename in a directory listing. `.github/copilot-instructions.md`
and the user's global instructions are cut off the same way. There is no `include`/`extends`
frontmatter and no documented inheritance mechanism: **text that is not in a `.agent.md` file does
not reach that agent as a subagent.**

The consequence was not theoretical. Conventions living only in AGENTS.md were silently no-ops, and
the orchestrator had already written down the symptom without knowing the cause:
"The shared convention tells each subagent to close its own instance when done, but in practice some
don't" (tableau-migrator.agent.md).

So the block is DUPLICATED into each persona on purpose. Four copies is exactly the redundancy
AGENTS.md was written to avoid - but generation plus a CI gate makes drift impossible, whereas the
"single source of truth" it replaces was invisible to 4 of 4 subagents. Deterministic duplication
beats an elegant abstraction that does not execute.

Reading it at runtime was considered and rejected: it costs a tool call, competes with a 40 KB
persona, and is discretionary - and the agent that most needs the rule (one stuck in a retry loop)
is precisely the one that will not pause to read a file.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "AGENTS.md"
AGENTS_DIR = REPO_ROOT / ".github" / "agents"

BEGIN = "<!-- BEGIN:shared-conventions -->"
END = "<!-- END:shared-conventions -->"

# GitHub documents a 30,000-character maximum for a custom agent prompt
# (docs.github.com/en/copilot/reference/custom-agents-configuration).
# Measured 2026-07-30: Copilot **CLI** does NOT enforce it - a ~48,000-char persona was probed at its
# tail and every section came back verbatim. But the documented limit covers GitHub.com and the IDEs
# too, so an over-cap persona is a portability risk: the same agent run GitHub-side may lose its tail,
# and in these files the tail is the accumulated `## Gotchas` - precisely the hard-won learnings.
PROMPT_CHAR_LIMIT = 30_000

# The canonical bundle layout, so a documented path can be checked against reality rather than only
# against the other copies of itself. `<bundle>/out/pbip/` survived in AGENTS.md and all four personas
# for weeks (issue #123) precisely because every copy agreed: consistency was the ONLY thing checked,
# and a uniformly wrong path passes that. Verified 2026-08-13 on a real 38-workbook bundle, and
# against the producer side (`scripts/migration_bundle.py` ENGINE_OUTPUT_DIRS = pbip/semantic_models/
# data; `scripts/run_estate.py` writes `handover/`; the engine writes `reports/`).
BUNDLE_DIRS = frozenset({"pbip", "reports", "semantic_models", "handover", "data"})

# ``<bundle>/pbip/`` or the brace form ``<bundle>/{pbip,reports,...}`` as written in prose/tables.
_BUNDLE_PATH = re.compile(r"<bundle>/(\{[^}`]*\}|[A-Za-z0-9_.\-]+)")


def _is_file_citation(name: str) -> bool:
    """True for a bundle-ROOT FILE (`summary.md`, `report.json`), which is not a layout claim.

    The directory rule below must not fire on one. Measured: widening the scan to persona bodies made
    the perfectly correct sentence *"read `<bundle>/summary.md` first"* fail CI with "`summary.md` is
    not a bundle directory ... there is no `out/` level" - a message that invents a trailing slash on a
    filename and then blames an unrelated defect. `docs/operator-runbook.md` already writes 6 distinct
    files that way, so the cheapest way to appease the gate would have been to delete the citation: a
    gate that trains people to write worse docs is worse than no gate.

    A dot is the discriminator because no bundle directory has one (`BUNDLE_DIRS` is the whole set) and
    every bundle-root artifact does.
    """
    return "." in name


PREAMBLE = (
    "> **Inherited from [`AGENTS.md`](../../AGENTS.md) — do not edit here.**\n"
    "> A custom-agent subagent receives ONLY this persona file: repo-level instruction files do not\n"
    "> reach it (verified). So these conventions are generated into every agent by\n"
    "> `scripts/sync_agent_conventions.py`, and CI fails if a copy drifts. Edit `AGENTS.md`, then\n"
    "> re-run that script.\n"
)

log = logging.getLogger("sync_agent_conventions")


def canonical_block() -> str:
    """The fenced conventions block from AGENTS.md, without its fences."""
    text = SOURCE.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise SystemExit(f"{SOURCE.name} is missing the {BEGIN} / {END} fences")
    body = text.split(BEGIN, 1)[1].split(END, 1)[0]
    return body.strip("\n")


def _rendered() -> str:
    return f"{BEGIN}\n{PREAMBLE}\n{canonical_block()}\n{END}"


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_including_fences, rest)."""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    cut = text.find("\n", end + 1) + 1
    return text[:cut], text[cut:]


def _insertion_point(body: str) -> int:
    """Index in `body` where the shared block belongs: AFTER the agent's own role statement.

    GitHub's documented ordering for an agent profile is role/persona first, then responsibilities,
    then scope constraints (see the example agents in the custom-agents docs). The block was
    originally inserted straight after the frontmatter, which pushed the agent's own
    `# <Name> — Subagent` identity down by ~70 lines: it read 6 KB of generic cross-cutting rules
    before learning what it *is*. Identity should frame the rules, not the other way round.

    So: insert after the first H1 and its intro prose, immediately before the first `##` section.
    That keeps the conventions early enough to be well-attended while letting the role lead.
    """
    h1 = re.search(r"^#\s+.+$", body, re.MULTILINE)
    if not h1:
        return 0
    section = re.search(r"^##\s+", body[h1.end() :], re.MULTILINE)
    return h1.end() + section.start() if section else len(body)


def apply_to(path: Path, write: bool) -> bool:
    """Insert or refresh the block in one agent file. Returns True when the file is (or would be) changed."""
    text = path.read_text(encoding="utf-8")
    block = _rendered()

    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        text = f"{head.rstrip()}\n{tail.lstrip()}" if head.strip().endswith("---") is False else f"{head}{tail}"
        # Re-place from scratch so an existing block also migrates to the correct position.
        text = re.sub(r"\n{3,}", "\n\n", text)

    frontmatter, body = _split_frontmatter(text)
    at = _insertion_point(body)
    updated = f"{frontmatter}{body[:at].rstrip()}\n\n{block}\n\n{body[at:].lstrip()}"

    original = path.read_text(encoding="utf-8")
    if updated == original:
        return False
    if write:
        path.write_text(updated, encoding="utf-8")
    return True


def prompt_size(path: Path) -> int:
    """Characters GitHub counts against the cap: the WHOLE file, frontmatter and line endings included.

    This used to measure the markdown body only, and that is how issue #132 stayed invisible:
    `tableau-migrator.agent.md` was **30,132 chars on disk** - over the documented cap - while this
    function reported 29,466 and printed a comfortable `98% of cap`. The cap was enforced; it was
    just enforced against a number nobody else uses. A measure that disagrees with the tool a human
    reaches for is worse than no measure - it *reassures* while the file is over.

    So: read with newline translation OFF, which makes this `(Get-Content -Raw).Length` on a CRLF
    working copy and `wc -c` on an LF one (PowerShell counts UTF-16 units, so it reads a handful
    higher wherever a persona uses a non-BMP emoji). That is the conservative reading - a Windows
    checkout counts its `\\r`s - and conservative is the right side to err on for a cap whose failure
    mode is a silently truncated tail.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return len(handle.read())


def documented_bundle_paths() -> dict[str, list[tuple[str, str]]]:
    """Bundle sub-paths named in the personas, mapped to (source, raw text) pairs that named them.

    Scope is the canonical block **and every persona file in full**, not just the block: the block was
    the only thing scanned when this check was written, so the identical wrong path in a persona's own
    `## Gotchas` - the half of the file the block does not generate - stayed invisible.

    `AGENTS.md` *outside* the block is deliberately NOT scanned. Its preamble cites `<bundle>/out/pbip/`
    on purpose, as the worked example of the bug this check exists to catch; scanning it would flag the
    sentence that explains the rule. That is a real blind spot, not an oversight - a wrong path in
    AGENTS.md prose is caught only by the personas it is copied into.
    """
    found: dict[str, list[tuple[str, str]]] = {}
    sources: list[tuple[str, str]] = [("the shared-conventions block", canonical_block())]
    sources += [
        (str(p.relative_to(REPO_ROOT)), p.read_text(encoding="utf-8")) for p in sorted(AGENTS_DIR.glob("*.agent.md"))
    ]
    for origin, text in sources:
        for match in _BUNDLE_PATH.finditer(text):
            token = match.group(1)
            names = token.strip("{}").split(",") if token.startswith("{") else [token]
            for name in (n.strip() for n in names):
                if name:
                    found.setdefault(name, []).append((origin, match.group(0)))
    return found


def check_bundle_paths(bundle: Path | None) -> list[str]:
    """Verify every `<bundle>/x` the personas document is a real bundle directory.

    Returns a list of human-readable problems (empty = OK). With `bundle`, the check is grounded in a
    reference bundle on disk; without one it still catches the #123 shape, because `out` is not a
    bundle directory in `BUNDLE_DIRS` no matter how many files agree that it is.

    On-disk resolution is deliberately limited to paths written as a **location** (`<bundle>/reports/`).
    A name that appears only inside the vocabulary enumeration `<bundle>/{pbip,reports,...}` is checked
    against `BUNDLE_DIRS` but not against disk, because that list describes the layout, not this
    bundle: an estate with no flat-file extracts has no `data/`, and failing it for that would punish a
    correct document with a legitimate bundle.

    Bundle-root FILES (`<bundle>/summary.md`) are citations, not layout claims, so the directory rule
    skips them - see `_is_file_citation`. Their absence under `--bundle` is a WARNING, never a failure:
    several are written conditionally (`empty-model-check.json`, `deploy-journal.jsonl` and
    `deploy-estate-id.txt` are all absent from a real completed bundle), so demanding them would
    reproduce, one layer down, exactly the false rejection this exemption removes.
    """
    problems = []
    documented = documented_bundle_paths()
    for name, occurrences in sorted(documented.items()):
        if name not in BUNDLE_DIRS and not _is_file_citation(name):
            origin, raw = occurrences[0]
            problems.append(
                f"{origin} documents `{raw}/`, but `{name}` is not a bundle directory. "
                f"A bundle is <bundle>/{{{','.join(sorted(BUNDLE_DIRS))}}} - there is no `out/` level."
            )
    if bundle is None:
        return problems

    if not bundle.is_dir():
        return problems + [f"--bundle {bundle} is not a directory"]
    actual = {p.name for p in bundle.iterdir() if p.is_dir()}
    for name, occurrences in sorted(documented.items()):
        as_location = [origin for origin, raw in occurrences if not raw.endswith("}")]
        if not as_location:
            continue
        if _is_file_citation(name):
            if not (bundle / name).exists():
                log.warning(
                    "  %s cites `<bundle>/%s`, absent from %s (may be written conditionally)",
                    as_location[0],
                    name,
                    bundle,
                )
        elif name in BUNDLE_DIRS and name not in actual:
            problems.append(f"{as_location[0]} documents `<bundle>/{name}/`, which does not exist in {bundle}")
    # The constant is evidence too, so let a real bundle correct it rather than the other way round.
    for name in sorted(BUNDLE_DIRS - actual):
        log.warning("  BUNDLE_DIRS lists %r, absent from %s - re-verify the constant", name, bundle)
    return problems


def report_sizes(agents: list[Path]) -> list[Path]:
    """Warn about personas over the documented prompt cap. Returns the over-cap files."""
    over = [p for p in agents if prompt_size(p) > PROMPT_CHAR_LIMIT]
    for path in sorted(agents, key=prompt_size, reverse=True):
        size = prompt_size(path)
        marker = "  OVER CAP" if size > PROMPT_CHAR_LIMIT else ""
        log.info("  %6d chars (%3d%% of cap)  %s%s", size, 100 * size // PROMPT_CHAR_LIMIT, path.name, marker)
    if over:
        log.warning(
            "%d persona(s) exceed GitHub's documented %d-char prompt cap. Copilot CLI does not "
            "enforce it (measured), but GitHub-hosted runs may truncate - and the tail of these "
            "files is the accumulated Gotchas. Trim before relying on the hosted path.",
            len(over),
            PROMPT_CHAR_LIMIT,
        )
    return over


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="CI gate: report drift and exit 1, changing nothing")
    parser.add_argument(
        "--allow-over-cap",
        action="store_true",
        help="with --check: report the prompt-cap overage but do not fail on it",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        help="a reference engine bundle; the documented <bundle>/... paths are then checked on disk "
        "instead of only against BUNDLE_DIRS",
    )
    args = parser.parse_args(argv)

    agents = sorted(AGENTS_DIR.glob("*.agent.md"))
    if not agents:
        log.error("No agent files found under %s", AGENTS_DIR)
        return 1

    # Order matters: sync first, then check paths. In write mode the check must see the state this run
    # leaves on disk, not the one it replaced - otherwise a run that FIXES a bad path still reports it
    # (observed while testing this change), and the operator "fixes" an already-fixed file.
    drifted = [p for p in agents if apply_to(p, write=not args.check)]
    path_problems = check_bundle_paths(args.bundle)

    if args.check:
        # Report EVERY problem, and report the path first. This used to return on drift before the
        # paths were even mentioned, so reintroducing `<bundle>/out/pbip/` printed "SHARED CONVENTIONS
        # OUT OF SYNC" - true, but a symptom: the personas were stale *because* AGENTS.md had just
        # changed. The wrong path only named itself on a second run, after the first was "fixed" by
        # syncing it into all four files. A gate that points at the symptom first teaches the wrong fix.
        failures = 0
        if path_problems:
            failures += 1
            log.error("DOCUMENTED BUNDLE PATH DOES NOT EXIST:")
            for problem in path_problems:
                log.error("  %s", problem)
            log.error(
                "Consistency between the copies proves nothing here - they are generated from one "
                "source, so a wrong path is wrong in all of them (issue #123)."
            )
        if drifted:
            failures += 1
            log.error("SHARED CONVENTIONS OUT OF SYNC - these agents do not carry the current AGENTS.md block:")
            for path in drifted:
                log.error("  %s", path.relative_to(REPO_ROOT))
            log.error("\nRun `python scripts/sync_agent_conventions.py` and commit the result.")
            log.error(
                "This matters because a subagent receives ONLY its persona file - a convention that "
                "lives only in AGENTS.md silently does not apply to it."
            )
        else:
            log.info("OK - all %d agent(s) carry the current shared conventions.", len(agents))
        over = report_sizes(agents)
        # The cap is now ENFORCED, not advisory. It was advisory while personas sat at 108-160% and a
        # hard failure would have blocked every commit; as of 2026-08-01 all four fit (~99%), so the
        # only thing left to catch is a regression - and this repo's own rule is that a mandate
        # without an exit code behind it is an anti-pattern. Use --allow-over-cap for a deliberate,
        # temporary overage.
        if over and not args.allow_over_cap:
            failures += 1
            log.error(
                "\nFAIL: %d persona(s) over the %d-char cap. Move craft knowledge into a skill bundle "
                "(.github/skills/powerbi-report-gotchas or powerbi-semantic-model-gotchas) rather than "
                "growing a persona - see docs/agent-architecture.md section 5. Re-run with "
                "--allow-over-cap only for a deliberate, temporary overage.",
                len(over),
                PROMPT_CHAR_LIMIT,
            )
        return 1 if failures else 0

    for path in drifted:
        log.info("  updated %s", path.relative_to(REPO_ROOT))
    log.info("done - %d of %d agent file(s) updated.", len(drifted), len(agents))
    if path_problems:
        # Non-zero in write mode too. A warning here is worse than useless: the write has just
        # PROPAGATED the block into all four personas, so the run that spread the bad path is exactly
        # the one that must not look successful.
        log.error("A DOCUMENTED BUNDLE PATH IS NOT A REAL BUNDLE DIRECTORY - written, not just proposed:")
        for problem in path_problems:
            log.error("  %s", problem)
        log.error("Fix the path at the source shown above and re-run; do not commit this state.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
