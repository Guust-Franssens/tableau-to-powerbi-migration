"""
purpose: Copy the shared agent conventions from AGENTS.md into every .github/agents/*.agent.md,
         because a custom-agent subagent receives ONLY its own persona file.
usage:   python scripts/sync_agent_conventions.py          # write the block into every agent
         python scripts/sync_agent_conventions.py --check  # CI gate: exit 1 if any agent has drifted

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
# Hence a warning, not a failure: it keeps the number visible instead of letting it drift silently.
PROMPT_CHAR_LIMIT = 30_000

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
    """Characters in the agent's prompt - the markdown body, excluding YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    _, body = _split_frontmatter(text)
    return len(body)


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
    args = parser.parse_args(argv)

    agents = sorted(AGENTS_DIR.glob("*.agent.md"))
    if not agents:
        log.error("No agent files found under %s", AGENTS_DIR)
        return 1

    drifted = [p for p in agents if apply_to(p, write=not args.check)]

    if args.check:
        if drifted:
            log.error("SHARED CONVENTIONS OUT OF SYNC - these agents do not carry the current AGENTS.md block:")
            for path in drifted:
                log.error("  %s", path.relative_to(REPO_ROOT))
            log.error("\nRun `python scripts/sync_agent_conventions.py` and commit the result.")
            log.error(
                "This matters because a subagent receives ONLY its persona file - a convention that "
                "lives only in AGENTS.md silently does not apply to it."
            )
            return 1
        log.info("OK - all %d agent(s) carry the current shared conventions.", len(agents))
        report_sizes(agents)
        return 0

    for path in drifted:
        log.info("  updated %s", path.relative_to(REPO_ROOT))
    log.info("done - %d of %d agent file(s) updated.", len(drifted), len(agents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
