"""
purpose: prove the SHARED identity files on this branch are byte-identical to the sibling PR's, and
         fail when that cannot be established. The half of the byte-identity gate that CI's shallow
         checkout cannot run.
usage:   python tests/verify_shared_identity_pin.py [--ref <git-ref>]
         exit 0 identical / 1 drifted / 2 cannot establish (ref missing, git unavailable)

Why "cannot establish" is a FAILURE, not a skip: round 3 measured a claim of byte-identity that was
false by two commits and an incompatible API, and nothing in the repo would have caught it. A check
that quietly passes when it could not look is worse than no check, because it is credited as one.

Ref resolution prefers ``origin/master`` - once the sibling PR merges, that IS the shared truth - and
falls back to the sibling branch while it is still open.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED = ("scripts/object_identity.py", "tests/test_object_identity.py")
PREFERRED_REFS = ("origin/master", "origin/feat/reference-readiness-gate")

EXIT_IDENTICAL = 0
EXIT_DRIFTED = 1
EXIT_CANNOT_ESTABLISH = 2


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, check=False)


def _has_file(ref: str, relative: str) -> bool:
    return _git("cat-file", "-e", f"{ref}:{relative}").returncode == 0


def resolve_ref(explicit: str | None) -> str | None:
    """The first ref that actually carries every shared file, or None when none does."""
    for ref in [explicit] if explicit else PREFERRED_REFS:
        if ref and all(_has_file(ref, relative) for relative in SHARED):
            return ref
    return None


def main(argv: list[str] | None = None) -> int:
    """Compare the shared files against the sibling ref; exit 2 when that cannot be established."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", help="compare against this git ref instead of the preferred list")
    args = parser.parse_args(argv)

    if _git("rev-parse", "--git-dir").returncode != 0:
        print("CANNOT ESTABLISH: not a git working tree, so byte-identity cannot be verified", file=sys.stderr)
        return EXIT_CANNOT_ESTABLISH

    ref = resolve_ref(args.ref)
    if ref is None:
        tried = args.ref or ", ".join(PREFERRED_REFS)
        print(
            f"CANNOT ESTABLISH: none of [{tried}] carries {', '.join(SHARED)}. "
            "Fetch the sibling branch (git fetch origin feat/reference-readiness-gate) and re-run; "
            "a shallow clone cannot answer this question.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_ESTABLISH

    drifted = [relative for relative in SHARED if _git("diff", "--quiet", ref, "--", relative).returncode != 0]
    commit = _git("rev-parse", ref).stdout.decode().strip()
    if drifted:
        print(f"DRIFTED from {ref}@{commit[:8]}: {', '.join(drifted)}", file=sys.stderr)
        print("Re-take them verbatim, then update PINNED in tests/test_shared_identity_pin.py.", file=sys.stderr)
        return EXIT_DRIFTED
    print(f"IDENTICAL to {ref}@{commit[:8]}: {', '.join(SHARED)}")
    return EXIT_IDENTICAL


if __name__ == "__main__":
    raise SystemExit(main())
