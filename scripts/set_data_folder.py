"""
purpose: Manage the per-model `DataFolder` M-parameter that each generated Fabric semantic model
         uses to locate its imported CSV data. The value committed to git is a portable placeholder
         (`<REPO_ROOT>\\<tree>\\<slug>\\data\\`) so no contributor's absolute machine path (and
         username) ever ships in the repo. Run this once after cloning to point every model at your
         local checkout so Power BI Desktop can refresh with real data.
usage:   python scripts/set_data_folder.py            # localize: set every model to THIS checkout's absolute path
         python scripts/set_data_folder.py --sanitize # restore the <REPO_ROOT> placeholder (run before committing)
         python scripts/set_data_folder.py --check     # CI gate: fail if any tracked file leaks an absolute user path
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_paths import HOST_PROFILE_PATH_RE  # noqa: E402  # pylint: disable=wrong-import-position

REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "<REPO_ROOT>"
# Matches:  expression DataFolder = "....."   (captures the quoted value)
# Also accepts SourceFolder: most models use DataFolder, but at least one (shipping-kpis) was authored
# with SourceFolder. Matching both stops a model silently drifting out of localize/sanitize coverage.
DATAFOLDER_RE = re.compile(r'(expression\s+(?:DataFolder|SourceFolder)\s*=\s*")([^"]*)(")')
# A leaked absolute path under a user profile, in the forms this repo's artifacts actually produce.
#
# ⚠️ **The pattern itself now lives in `scripts/host_paths.py` and this is an ALIAS.** It was
# copied - not shared - into two other guards, and both copies drifted into asking a weaker question:
# one anchored it with `^`, and `package_unit._declares_unsafe_path` parsed the string as a path
# instead of searching it. A host path wrapped in prose (`HTTP 503: <it> could not be opened`) was
# therefore refused by this gate and shipped by both of the others (#480 B1). Three competing
# definitions of one question is how that leak class survived six review rounds, so there is now
# exactly one definition per question, and every consumer imports it.
#
# ⚠️ **This gate deliberately keeps the NARROW question, and round 9 is why that is a choice rather
# than an oversight.** What ships to a customer is now judged by `host_paths.discloses_host_location`
# - any absolute location on any host, in any spelling - because a build drive, a UNC share and a
# POSIX root all name the operator's machine. This gate cannot ask that: it is `search`ed over every
# git-tracked file, and this repo's own fixtures, runbooks and docstrings name those locations on
# purpose. The invariant still holds one-directionally and is asserted in `tests/test_package_unit.py`
# - the shipping predicate is a strict superset of this one, so a package can never ship what a
# commit could not.
ABSOLUTE_USER_PATH_RE = HOST_PROFILE_PATH_RE


def _model_expression_files() -> list[Path]:
    """expressions.tmdl across all three migration trees (examples/, migrations/workbooks/, migrations/datasources/)."""
    return sorted(
        p
        for tree in ("examples", "migrations/workbooks", "migrations/datasources")
        for p in REPO_ROOT.glob(f"{tree}/*/fabric/*.SemanticModel/definition/expressions.tmdl")
    )


def _tree_and_slug_for(expr_file: Path) -> tuple[str, str]:
    """<tree...>/<slug>/fabric/<Model>.SemanticModel/definition/expressions.tmdl -> (<tree...>, <slug>).

    Derived from the END, not the start: the trees have different depths (`examples/<slug>/` is one
    level, `migrations/workbooks/<slug>/` is two). Indexing `parts[0], parts[1]` silently returned
    ("migrations", "workbooks") for every user migration - dropping the slug and pointing every model
    at the same non-existent data folder.
    """
    parts = expr_file.relative_to(REPO_ROOT).parts
    slug_idx = len(parts) - 5  # fabric / <X>.SemanticModel / definition / expressions.tmdl
    return "\\".join(parts[:slug_idx]), parts[slug_idx]


def _rewrite(expr_file: Path, sanitize: bool) -> bool:
    text = expr_file.read_text(encoding="utf-8")
    tree, slug = _tree_and_slug_for(expr_file)
    base = PLACEHOLDER if sanitize else str(REPO_ROOT)
    new_value = f"{base}\\{tree}\\{slug}\\data\\"

    def _sub(match: re.Match[str]) -> str:
        return f"{match.group(1)}{new_value}{match.group(3)}"

    new_text, n = DATAFOLDER_RE.subn(_sub, text)
    if n == 0:
        print(f"  WARN no DataFolder expression in {expr_file.relative_to(REPO_ROOT)}")
        return False
    if new_text != text:
        expr_file.write_text(new_text, encoding="utf-8")
        print(f"  set {slug} -> {new_value}")
        return True
    return False


def _tracked_files() -> list[Path]:
    """Return git-tracked files (what actually ships), so the check ignores local/gitignored scratch."""
    git = shutil.which("git") or "git"
    out = subprocess.run(
        [git, "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line]


def _check() -> int:
    """Fail (exit 1) if any git-tracked file leaks an absolute user path. Used by CI."""
    offenders: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pbix", ".hyper", ".twbx", ".twb", ".abf"}:
            continue
        # Files that necessarily CONTAIN the pattern to define or test it. Kept to an explicit,
        # tiny allowlist so it can never be widened by accident into a real blind spot.
        if path.name in {"set_data_folder.py", "test_repo_layout.py"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if ABSOLUTE_USER_PATH_RE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    if offenders:
        print("ABSOLUTE USER PATH LEAK - these tracked files contain a 'X:\\Users\\<name>' path:")
        for o in offenders:
            print(f"  {o}")
        print("Run `python scripts/set_data_folder.py --sanitize` (models) and de-hardcode any scripts.")
        return 1
    print("OK - no absolute user paths found in tracked files.")
    return 0


def main() -> None:
    """Parse args and run the requested mode (localize / sanitize / check)."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sanitize", action="store_true", help="restore the <REPO_ROOT> placeholder before committing")
    group.add_argument("--check", action="store_true", help="CI gate: fail if any tracked file leaks an absolute path")
    args = parser.parse_args()

    if args.check:
        sys.exit(_check())

    files = _model_expression_files()
    if not files:
        print(
            "no semantic-model expressions.tmdl files found under "
            "examples|migrations/workbooks|migrations/datasources /*/fabric/"
        )
        return
    mode = "sanitize (placeholder)" if args.sanitize else "localize (this checkout)"
    print(f"{mode}: {len(files)} model(s)")
    changed = sum(_rewrite(f, args.sanitize) for f in files)
    print(f"done - {changed} file(s) updated")


if __name__ == "__main__":
    main()
