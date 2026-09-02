"""
purpose: quarantine the ONE lossy string comparison in this toolkit - fail if anything outside
         `object_identity.py` calls `object_identity.normalize()`.
usage:   python scripts/check_identity_normalization.py [--json <file>] [--quiet]

Why a lint rule and not a convention
-------------------------------------
`object_identity.py` makes an ambiguous identity unrepresentable *inside* `IdentityIndex`:
`Resolution.value()` raises unless exactly one match exists, and an index built with
`normalized=False` has no lossy key table at all. That is a real guarantee - but only for joins that
go through it. A plain `dict` keyed on `normalize(name)` is still one line away, and this defect has
now defeated a convention at FIVE successive layers of PR #428 (routing, matching, normalization,
manual-kind, unit-join). A sixth author reaching for a raw dict is not hypothetical; it is the single
most likely way layer six arrives.

The bound the reviewer set was never "close the fifth layer". It was:

    a new join written by a future author cannot express the ambiguous case.

A type plus an available raw dict is a convention. A type plus a rule that fails the build is a
guarantee, and this is that rule.

Deliberately NARROW, and that is the point
-------------------------------------------
This bans exactly one thing: a call whose callee resolves to `object_identity.normalize`, from any
tracked `scripts/*.py` or `tests/*.py` other than `object_identity.py` itself. It does **not** try to
detect "any lossy join", which is undecidable and would produce false positives on unrelated code -
and a rule that cries wolf gets switched off, at which point it protects nothing. A narrow rule that
holds is worth far more than a broad one someone disables.

Two consequences of that narrowness, both deliberate:

* Other modules have their own `normalize()` (`group_oracle_by_workbook.py`) and stdlib
  `unicodedata.normalize` is used in `work_dirs.py`. Neither is this function, and neither is
  reported. Import aliases are resolved per file, so `oid.normalize(...)`,
  `object_identity.normalize(...)` and a bare `normalize(...)` after `from object_identity import
  normalize` are all caught, while `grp.normalize(...)` is not.
* Occurrences inside string literals are invisible to the AST and so are not reported. That is
  correct: `tests/mutation_reference_readiness.py` carries `oid.normalize` inside mutation source
  strings on purpose, to REINTRODUCE the defect and prove the suite catches it.

Enumeration, not reasoning
--------------------------
This rule exists because a claim of mine - "normalization is gone from every engine-to-engine join" -
was disproved by the reviewer enumerating the joins and finding four survivors. Writing the rule
found two more that neither of us had listed, including a lossy comparison on WORKBOOK identity in
`Evidence.is_for`. When you want to claim "X is gone from every Y", enumerate the Ys; better still,
make a machine do it every build.

Exit codes
----------
| 0 | no call to `object_identity.normalize` outside its own module. |
| 1 | at least one call found; each is printed with the file, line and the fix. |
| 2 | usage error (argparse). |
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The module that owns the lossy comparison, and the only file allowed to call it.
OWNER_MODULE = "object_identity"
OWNER_FILE = f"{OWNER_MODULE}.py"
GUARDED_FUNCTION = "normalize"

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_USAGE = 2

FIX = (
    "route the join through `object_identity.IdentityIndex` instead - it returns a `Resolution` "
    "whose reader raises unless exactly one match exists, so an ambiguous key cannot be silently "
    "resolved. If you only need to REPORT that two names look alike, use "
    "`object_identity.shares_name()`, which may never satisfy a page."
)


@dataclass(frozen=True)
class Violation:
    """One call to the guarded function from outside its module."""

    path: str
    line: int
    expression: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: calls {self.expression} - {FIX}"


def _guarded_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """`(module aliases, bare names)` this file could reach the guarded function through.

    Resolved per file rather than matched textually, so `grp.normalize(...)` from a DIFFERENT
    module's own `normalize` is not reported - a false positive is how a rule gets disabled.
    """
    modules: set[str] = set()
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == OWNER_MODULE)
        elif isinstance(node, ast.ImportFrom) and node.module == OWNER_MODULE:
            bare.update(alias.asname or alias.name for alias in node.names if alias.name == GUARDED_FUNCTION)
    return modules, bare


def _calls(tree: ast.Module, modules: set[str], bare: set[str]) -> list[ast.Call]:
    """Every call whose callee resolves to the guarded function."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in bare:
            found.append(node)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == GUARDED_FUNCTION
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            found.append(node)
    return found


def _expression(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}()"
    return f"{getattr(func, 'id', GUARDED_FUNCTION)}()"


def scan_source(path: Path, source: str) -> list[Violation]:
    """Violations in one file's source text. Unparseable files are reported, never skipped."""
    display = path.name if path.parent == REPO_ROOT else str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    if path.name == OWNER_FILE:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Violation(display, exc.lineno or 0, f"UNPARSEABLE ({exc.msg})")]
    modules, bare = _guarded_aliases(tree)
    if not modules and not bare:
        return []
    return [Violation(display, node.lineno, _expression(node)) for node in _calls(tree, modules, bare)]


def tracked_python_files(root: Path) -> list[Path]:
    """Tracked `scripts/` and `tests/` Python files. Git-tracked so scratch copies are not scanned."""
    listed = subprocess.run(
        ["git", "ls-files", "scripts/*.py", "tests/*.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(root / name for name in listed)


def scan(root: Path) -> list[Violation]:
    """Every violation across the tracked suite."""
    violations: list[Violation] = []
    for path in tracked_python_files(root):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        violations.extend(scan_source(path, source))
    return violations


def render(violations: list[Violation]) -> str:
    """Human-readable verdict, matching the sibling offline gates."""
    if not violations:
        return (
            f"IDENTITY NORMALIZATION: OK - no call to `{OWNER_MODULE}.{GUARDED_FUNCTION}()` outside "
            f"{OWNER_FILE}. The lossy comparison stays quarantined."
        )
    lines = [
        f"IDENTITY NORMALIZATION: {len(violations)} call(s) to "
        f"`{OWNER_MODULE}.{GUARDED_FUNCTION}()` outside {OWNER_FILE}:"
    ]
    lines.extend(f"  {violation}" for violation in violations)
    lines.append(
        "  This defect has defeated a convention at five successive layers, so the rule is a gate "
        "rather than a guideline. If you genuinely need a new lossy comparison, add it INSIDE "
        f"{OWNER_FILE} where its ambiguity handling is enforced by construction."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root to scan")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    args = parser.parse_args(argv)

    violations = scan(args.root)
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "id": "identity-normalization",
                    "status": "OK" if not violations else "VIOLATIONS",
                    "guarded": f"{OWNER_MODULE}.{GUARDED_FUNCTION}",
                    "violations": [
                        {"path": item.path, "line": item.line, "expression": item.expression} for item in violations
                    ],
                    "fix": FIX,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    if not args.quiet:
        print(render(violations))
    return EXIT_VIOLATIONS if violations else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
