"""
purpose: verify every replay script under a bundle's `_build/` is registered in the replay manifest,
         every registration still points at a real script, and the ordering it declares is
         satisfiable - so a re-run knows what to replay, and in what order (issue #259).
usage:   python scripts/check_replay_manifest.py --bundle <dir> [--json]

Why this exists
----------------
`declare_generated_edit.py` writes a drift declaration for a `_build/fix_*.py` script ONLY when the
target it wraps actually changed hash - an idempotent re-run (`DECLARE: NO_CHANGE`) records nothing
there. It registers the script separately, unconditionally, in `_build/replay-manifest/*.json`
(``generated_edit_declarations.append_replay_registration``), so the script stays discoverable even
on a run that changed nothing. This gate is the other half of that convention: it fails a bundle
with a `_build/*.py` script that has no matching registration, or a registration naming a script
that is no longer on disk - so a bundle with 161 undiscoverable fix scripts (the field report behind
issue #259) is a build-time failure instead of an archaeology exercise at replay time.

A registration's ordering claim is checked here too, not merely recorded: every
`--depends-on SCRIPT_IDENTITY` a script declared must name another REGISTERED script, and the whole
dependency graph must be acyclic - two scripts that depend on each other could never be replayed in
an order that satisfies both. Order is not tracked as one implicit global sequence (see
`declare_generated_edit.py`'s docstring for why a shared "next position" counter is the wrong
primitive for concurrent bundle-scratch writers); this gate only verifies each script's own claim is
internally consistent with its siblings', it does not compute or execute a replay order itself.

The trap this exists to avoid
------------------------------
An ABSENT `_build/replay-manifest/` directory is not "there are no replay scripts to check" - a
bundle can have real `_build/fix_*.py` scripts and simply never have registered any of them. Reading
that silently as "nothing to check" would make the exact failure this gate exists for invisible
again. So a bundle with scripts on disk and no manifest directory at all reports MANIFEST_ABSENT
(blocking), a state distinct from CLEAN (no scripts on disk, so no manifest is needed) and from
VIOLATIONS (a manifest exists but disagrees with what is on disk).

What it will NOT tell you
--------------------------
That a script is SAFE to replay, or that its `--order-independent` claim is actually true - both are
a human judgement this gate does not have the information to make. It only verifies the claim is
internally consistent (every dependency exists, no cycle), and that nothing on disk has gone
undocumented.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from generated_edit_declarations import REPLAY_MANIFEST_DIR, load_replay_registrations
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from generated_edit_declarations import REPLAY_MANIFEST_DIR, load_replay_registrations

BUILD_DIR = "_build"


def discover_replay_scripts(bundle: Path) -> set[str]:
    """Every tracked-looking `.py` file under `<bundle>/_build/`, as a bundle-relative POSIX path."""
    build_dir = bundle / BUILD_DIR
    if not build_dir.is_dir():
        return set()
    return {path.relative_to(bundle).as_posix() for path in build_dir.rglob("*.py")}


def latest_registrations(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The most-recently-recorded registration per script, when a script was registered more than
    once (e.g. re-run after the engine ran again). Earlier registrations still count as evidence
    the script EXISTS; only the ordering claim needs one authoritative version per script."""
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        identity = record.get("script_identity")
        if not isinstance(identity, str) or not identity:
            continue
        current = latest.get(identity)
        if current is None or str(record.get("recorded_at", "")) >= str(current.get("recorded_at", "")):
            latest[identity] = record
    return latest


def find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """One cycle in the dependency graph, as the sequence of script identities that form it."""
    WHITE, GRAY, BLACK = 0, 1, 2  # pylint: disable=invalid-name
    color = {node: WHITE for node in graph}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        path.append(node)
        for neighbor in graph.get(node, []):
            if neighbor not in color:
                continue  # unknown dependency is reported separately, not as a cycle
            if color[neighbor] == GRAY:
                return [*path[path.index(neighbor):], neighbor]
            if color[neighbor] == WHITE:
                found = visit(neighbor)
                if found:
                    return found
        path.pop()
        color[node] = BLACK
        return None

    for start in graph:
        if color[start] == WHITE:
            found = visit(start)
            if found:
                return found
    return None


def check(bundle: Path) -> tuple[str, list[str]]:
    """Adjudicate one bundle's replay manifest. Returns (state, notes)."""
    scripts_on_disk = discover_replay_scripts(bundle)
    registrations = load_replay_registrations(bundle)
    registered = latest_registrations(registrations)
    registered_identities = set(registered)

    manifest_dir = bundle / REPLAY_MANIFEST_DIR
    if scripts_on_disk and not manifest_dir.is_dir():
        return "MANIFEST_ABSENT", [
            f"{len(scripts_on_disk)} replay script(s) found under {BUILD_DIR}/, but "
            f"{REPLAY_MANIFEST_DIR.as_posix()}/ does not exist - none of them are registered.",
            *(f"UNREGISTERED: {script}" for script in sorted(scripts_on_disk)),
        ]

    unregistered = sorted(scripts_on_disk - registered_identities)
    dangling = sorted(registered_identities - scripts_on_disk)

    notes: list[str] = []
    for script in unregistered:
        notes.append(f"UNREGISTERED: {script} exists on disk with no replay-manifest registration")
    for script in dangling:
        notes.append(f"DANGLING: {script} is registered but no longer exists on disk")

    graph = {
        identity: [] if record.get("order_independent") else list(record.get("depends_on") or [])
        for identity, record in registered.items()
    }
    for identity, deps in graph.items():
        for dep in deps:
            if dep not in registered_identities:
                notes.append(f"UNKNOWN DEPENDENCY: {identity} depends_on {dep}, which is not registered")
    cycle = find_cycle(graph)
    if cycle:
        notes.append(f"DEPENDENCY CYCLE: {' -> '.join(cycle)}")

    if not notes:
        return "CLEAN", [f"{len(scripts_on_disk)} replay script(s) registered and consistent"]
    return "VIOLATIONS", notes


def main(argv: list[str] | None = None) -> int:
    """Exit 0 CLEAN, 1 VIOLATIONS, 2 no such bundle, 3 MANIFEST_ABSENT."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path, help="migration bundle to check")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.bundle.is_dir():
        sys.stderr.write(f"REPLAY_MANIFEST: ERROR no such bundle: {args.bundle}\n")
        return 2

    state, notes = check(args.bundle)
    if args.json:
        sys.stdout.write(json.dumps({"bundle": str(args.bundle), "state": state, "notes": notes}, indent=2) + "\n")
    else:
        print(f"REPLAY_MANIFEST: {state} {args.bundle}")
        for note in notes:
            print(f"  {note}")
    return {"CLEAN": 0, "VIOLATIONS": 1, "MANIFEST_ABSENT": 3}[state]


if __name__ == "__main__":
    raise SystemExit(main())
