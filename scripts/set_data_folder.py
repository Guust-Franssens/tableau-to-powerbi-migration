"""
purpose: Manage the per-model `DataFolder` M-parameter that each generated Fabric semantic model
         uses to locate its imported CSV data. The value committed to git is a portable placeholder
         (`<REPO_ROOT>\\migrations\\<slug>\\data\\`) so no contributor's absolute machine path (and
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

REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "<REPO_ROOT>"
# Matches:  expression DataFolder = "....."   (captures the quoted value)
DATAFOLDER_RE = re.compile(r'(expression\s+DataFolder\s*=\s*")([^"]*)(")')
# A Windows absolute path under a user profile, i.e. a leaked machine path.
ABSOLUTE_USER_PATH_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\"']+", re.IGNORECASE)


def _model_expression_files() -> list[Path]:
    return sorted(REPO_ROOT.glob("migrations/*/fabric/*.SemanticModel/definition/expressions.tmdl"))


def _slug_for(expr_file: Path) -> str:
    """migrations/<slug>/fabric/<Model>.SemanticModel/definition/expressions.tmdl -> <slug>."""
    return expr_file.relative_to(REPO_ROOT).parts[1]


def _rewrite(expr_file: Path, sanitize: bool) -> bool:
    text = expr_file.read_text(encoding="utf-8")
    slug = _slug_for(expr_file)
    base = PLACEHOLDER if sanitize else str(REPO_ROOT)
    new_value = f"{base}\\migrations\\{slug}\\data\\"

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
        # Ignore this script itself (it necessarily documents the pattern).
        if path.name == "set_data_folder.py":
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
        print("no semantic-model expressions.tmdl files found under migrations/*/fabric/")
        return
    mode = "sanitize (placeholder)" if args.sanitize else "localize (this checkout)"
    print(f"{mode}: {len(files)} model(s)")
    changed = sum(_rewrite(f, args.sanitize) for f in files)
    print(f"done - {changed} file(s) updated")


if __name__ == "__main__":
    main()
