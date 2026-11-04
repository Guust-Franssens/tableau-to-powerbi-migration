"""
purpose: Remove the retired credential gate's write-deny ACEs from trees that were armed before it was deleted.
usage:   python scripts/disarm_credential_gate.py <root> [--apply]

The retired gate denied the current user `(OI)(CI)(WD,AD,WA)` on every `fabric`, `pbip`,
`*.SemanticModel` and `*.Report` anchor of a gated migration, and its children inherited the deny.
Once the gate is gone nothing removes those ACEs, so an old tree stays read-only for editing and
refresh. This tool walks `<root>` without following reparse points, finds anchor-named folders that
carry an explicit (non-inherited) deny of exactly that shape for the current user, and lists them.
`--apply` runs `icacls <dir> /remove:d <user>` on each one and re-reads the ACL to prove it is gone.

Other principals' ACEs and explicit allows are left alone. A folder where the current user ALSO has
some other explicit deny is reported and skipped, because `/remove:d` would strip that deny as well.
Marker and ledger files are ignored on purpose: after the gate is deleted they are inert evidence.

Exit codes: 0 nothing left to remove (or not Windows); 1 a listing or removal failed, or a folder was
skipped; 3 dry run found armed folders (re-run with --apply).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

from bundle_corpus import is_reparse_entry

GATE_FLAGS = frozenset({"OI", "CI", "DENY"})
GATE_RIGHTS = frozenset({"WD", "AD", "WA"})
ANCHOR_NAMES = frozenset({"fabric", "pbip"})
ANCHOR_SUFFIXES = (".semanticmodel", ".report")
EXIT_CLEAN = 0
EXIT_FAILED = 1
EXIT_ARMED = 3

Runner = Callable[[list[str]], tuple[int, str]]


class Ace(NamedTuple):
    """One parsed `icacls` ACE line."""

    principal: str
    flags: frozenset[str]
    rights: frozenset[str]


def parse_acl_listing(listing: str, target: str) -> list[Ace]:
    """Parse `icacls <target>` output. icacls echoes the target before the first ACE only."""
    aces = []
    for raw in listing.splitlines():
        line = raw.removeprefix(target).strip()
        principal, separator, permissions = line.partition(":(")
        if not separator:
            continue
        groups = re.findall(r"\(([^)]*)\)", "(" + permissions)
        if not groups:
            continue
        aces.append(
            Ace(
                principal=principal.strip(),
                flags=frozenset(group.upper() for group in groups[:-1]),
                rights=frozenset(right.strip().upper() for right in groups[-1].split(",")),
            )
        )
    return aces


def is_current_user(principal: str, user: str) -> bool:
    """icacls prints `DOMAIN\\user` for an ACE granted to a bare `%USERNAME%`; accept both forms."""
    who, me = principal.casefold(), user.casefold()
    if not me:
        return False
    return who == me or ("\\" not in me and who.rsplit("\\", 1)[-1] == me)


def is_gate_ace(ace: Ace) -> bool:
    """Exactly the gate's explicit deny: no inherited (I), no other flag, no other right."""
    return ace.flags == GATE_FLAGS and ace.rights == GATE_RIGHTS


def classify(listing: str, target: str, user: str) -> str:
    """Return `armed`, `conflict` (gate deny plus another explicit deny of ours) or `clean`."""
    ours = [
        ace
        for ace in parse_acl_listing(listing, target)
        if is_current_user(ace.principal, user) and "DENY" in ace.flags and "I" not in ace.flags
    ]
    if not any(is_gate_ace(ace) for ace in ours):
        return "clean"
    return "armed" if all(is_gate_ace(ace) for ace in ours) else "conflict"


def is_anchor_name(name: str) -> bool:
    """The gate's anchor folder names, compared case-insensitively like the file system."""
    folded = name.casefold()
    return folded in ANCHOR_NAMES or folded.endswith(ANCHOR_SUFFIXES)


def iter_anchor_dirs(root: Path, errors: list[str]) -> Iterator[Path]:
    """Yield anchor-named directories under `root` (inclusive); never enter a reparse point."""
    pending = [root]
    while pending:
        current = pending.pop()
        if is_anchor_name(current.name):
            yield current
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            errors.append(f"cannot list {current}: {exc.strerror or exc}")
            continue
        for entry in reversed(children):
            try:
                info = os.lstat(entry.path)
            except OSError as exc:
                errors.append(f"cannot stat {entry.path}: {exc.strerror or exc}")
                continue
            if is_reparse_entry(info) or not entry.is_dir(follow_symlinks=False):
                continue
            pending.append(Path(entry.path))


def run_icacls(args: list[str]) -> tuple[int, str]:
    """The only process boundary: `icacls <args>`, never raising."""
    try:
        proc = subprocess.run(["icacls", *args], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def is_windows() -> bool:
    """Seam for tests; the gate only ever set ACLs on Windows."""
    return os.name == "nt"


def _state(directory: Path, user: str, runner: Runner) -> str | None:
    code, listing = runner([str(directory)])
    return classify(listing, str(directory), user) if code == 0 else None


def _root_refusal(root: Path, user: str) -> str | None:
    if not user:
        return "%USERNAME% is not set, so the current user cannot be identified."
    try:
        info = os.lstat(root)
    except OSError as exc:
        return f"cannot read root {root}: {exc.strerror or exc}"
    if is_reparse_entry(info):
        return f"root {root} is a reparse point; pass the real folder, links are never followed."
    return None


def _scan(root: Path, user: str, runner: Runner, errors: list[str]) -> list[Path]:
    armed = []
    for directory in iter_anchor_dirs(root, errors):
        state = _state(directory, user, runner)
        if state is None:
            errors.append(f"cannot read the ACL of {directory}")
        elif state == "conflict":
            errors.append(
                f"skipped {directory}: you also hold another explicit deny there, which /remove:d would "
                "strip too; inspect it with icacls and remove the gate deny by hand"
            )
        elif state == "armed":
            armed.append(directory)
    return armed


def disarm(
    root: Path,
    *,
    apply: bool,
    runner: Runner = run_icacls,
    user: str | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Testable core: list (dry run) or remove the current user's gate denies under `root`."""
    if not is_windows():
        out("Not Windows: the gate never set ACLs here, nothing to do.")
        return EXIT_CLEAN
    user = os.environ.get("USERNAME", "") if user is None else user
    refusal = _root_refusal(root, user)
    if refusal:
        out(f"ERROR: {refusal}")
        return EXIT_FAILED

    errors: list[str] = []
    armed = _scan(root, user, runner, errors)
    for directory in armed:
        if not apply:
            out(f"ARMED   {directory}")
            continue
        code, output = runner([str(directory), "/remove:d", user])
        if code != 0 or _state(directory, user, runner) != "clean":
            errors.append(f"removal failed for {directory}: {output or f'exit {code}'}")
        else:
            out(f"CLEARED {directory}")

    for error in errors:
        out(f"ERROR: {error}")
    if errors:
        return EXIT_FAILED
    if armed and not apply:
        out(f"{len(armed)} armed folder(s) found. Re-run with --apply to remove the deny.")
        return EXIT_ARMED
    out(f"Removed the deny from {len(armed)} folder(s)." if armed else "Nothing left to remove.")
    return EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Remove the retired credential gate's (OI)(CI)(DENY)(WD,AD,WA) ACEs for the current user from "
            "fabric, pbip, *.SemanticModel and *.Report folders under ROOT. Dry run by default."
        ),
        epilog="Exit codes: 0 nothing left to remove, 1 a failure or skipped folder, 3 dry run found armed folders.",
    )
    parser.add_argument("root", type=Path, help="folder to scan, for example a run folder or _runs")
    parser.add_argument("--apply", action="store_true", help="run icacls /remove:d on each armed folder")
    args = parser.parse_args(argv)
    return disarm(args.root.absolute(), apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
