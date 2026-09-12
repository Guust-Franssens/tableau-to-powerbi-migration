"""
purpose: Manage the per-model folder M-parameter that each generated Fabric semantic model uses to
         locate its imported data. The value committed to git is a portable placeholder
         (`<REPO_ROOT>\\<tree>\\<slug>\\data\\`) so no contributor's absolute machine path (and
         username) ever ships in the repo. Run this once after cloning to point every model at your
         local checkout so Power BI Desktop can refresh with real data. `--package` is the same idea
         for ONE handover package: `scripts/package_unit.py` writes `<PACKAGE_ROOT>` rather than the
         machine that built the package, so BINDING it to wherever it now lives is a step of using
         it. Package mode delegates a guarded bind/reseal to package_unit: bind before opening,
         sanitize before transfer, then bind at the recipient. Inspection proves the current
         location, not shareability or readiness. Existing S1-dirty packages are not rebaselined.
usage:   python scripts/set_data_folder.py            # localize: set every model to THIS checkout's absolute path
         python scripts/set_data_folder.py --sanitize # restore the <REPO_ROOT> placeholder (run before committing)
         python scripts/set_data_folder.py --check     # CI gate: fail if any tracked file leaks an absolute user path
         python scripts/set_data_folder.py --package <dir>  # bind ONE package to its own data/
         python scripts/set_data_folder.py --package <absolute-dir> --inspect
         python scripts/set_data_folder.py --package <absolute-dir> --sanitize
         python scripts/set_data_folder.py --package <absolute-dir> --provider-package <absolute-provider>
         python scripts/set_data_folder.py --package <consumer-ABS> --sanitize --provider-package <provider-ABS>

Package exits: 0 success/idempotent/not applicable; 1 refusal/mismatch/published-with-residue;
2 usage; 3 cannot-establish; 130 interrupt with the observed transaction outcome.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_paths import HOST_PROFILE_PATH_RE  # noqa: E402  # pylint: disable=wrong-import-position
from path_flavour import join as flavour_join  # noqa: E402  # pylint: disable=wrong-import-position

REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "<REPO_ROOT>"
#: The placeholder `package_unit.py` writes into a handover package instead of the machine that
#: built it. Rooting the value here is what makes a package movable at all: the builder cannot know
#: where the package will end up, and Power Query rejects a relative `File.Contents` argument, so
#: binding is a step of CONSUMING the package rather than something baked into it (issue #461).
PACKAGE_PLACEHOLDER = "<PACKAGE_ROOT>"
#: Matches ANY `expression <name> = "<value>"` declaration, capturing the quoted value.
#:
#: ⚠️ **Matching by NAME was a blind spot, not a shortcut.** The old pattern accepted
#: `DataFolder|SourceFolder` only, so `package_unit.py`'s fallback parameter (`PackageDataFolder`,
#: used whenever a model already declares `DataFolder`) was silently skipped: a moved package's model
#: kept pointing at the OLD package location and nothing said so. An engine model may name its folder
#: parameter anything at all, so a name list can only ever be the cases someone remembered. The
#: value's SHAPE identifies a data-folder parameter, so that is what is tested (:func:`_data_tail`).
EXPRESSION_RE = re.compile(r'(expression\s+(?:#"[^"]+"|[^\s=]+)\s*=\s*")([^"]*)(")')
# The repo commit gate intentionally asks the narrower profile-path question. Customer-shipped
# artifacts use host_paths.discloses_host_location, which is a strict superset.
ABSOLUTE_USER_PATH_RE = HOST_PROFILE_PATH_RE
#: The directory every generated model reads its rows from - the convention `package_unit.py` writes
#: and this script re-roots onto. It is the segment the rewrite pivots on, never a whole value.
DATA_SEGMENT = "data"
EXPRESSIONS_TMDL = "expressions.tmdl"
#: What proves a directory IS a handover package even when it has no model to bind. A report-only
#: unit ships no `expressions.tmdl` at all, and the README prints the binding command in every
#: package, so treating "no model" as "you pointed me at the wrong folder" would make the package's
#: own first documented command fail for a whole class of units. The manifest tells the two apart.
PACKAGE_MANIFEST = "package-manifest.json"


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


def _is_pathish(value: str) -> bool:
    """Whether a parameter value looks like a filesystem path at all (absolute, or a placeholder)."""
    return value.startswith((PLACEHOLDER, PACKAGE_PLACEHOLDER)) or bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/)", value))


def _data_tail(value: str) -> str | None:
    """What a data-folder value names BELOW its `data` segment, or None when it has no such segment.

    ⚠️ **Dropping this tail is what made the relocation guidance produce a broken path.** A package's
    folder parameter is `<PACKAGE_ROOT>\\data\\<Unit>.Data\\` - the rows live in a SUBDIRECTORY of
    `data/`, because `package_unit._packaged_data_target` keeps the source folder's own leaf name so
    two sources cannot collide on one destination. Rewriting the whole value to `...\\data\\`
    therefore pointed the partition at a directory that merely CONTAINS the one it needs, and the
    file it reads is not there. Nothing downstream can see that: the model stays structurally perfect
    and fails at refresh, on someone else's machine.

    A package value has an explicit `<PACKAGE_ROOT>/data` boundary. Everything after it belongs
    to the tail, even another `data`. Package planning first projects held bound values onto that
    portable boundary; the legacy checkout-only fallback still reads from the last `data`.

    ⚠️ Returned `/`-joined and SEPARATOR-FREE of intent: the caller composes it onto the destination
    with that destination's own separator (:func:`path_flavour.join`). Joining it here with a literal
    backslash is what wrote `/tmp/package\\data\\...` on POSIX - one path segment with backslashes
    inside it - and then reported the directory missing (round-2 finding 4).
    """
    if value.startswith(PACKAGE_PLACEHOLDER):
        portable = value.replace("\\", "/")
        boundary = f"{PACKAGE_PLACEHOLDER}/{DATA_SEGMENT}"
        if portable == boundary:
            return ""
        return portable[len(boundary) + 1 :].rstrip("/") if portable.startswith(f"{boundary}/") else None
    parts = [part for part in re.split(r"[\\/]", value) if part]
    lowered = [part.casefold() for part in parts]
    if DATA_SEGMENT not in lowered:
        return None
    return "/".join(parts[len(lowered) - lowered[::-1].index(DATA_SEGMENT) :])


def _rewritten(text: str, base: str) -> tuple[str, int, list[str]]:
    """`(new text, rewrites, path values left alone)` - re-root every data-folder parameter onto ``base``.

    ``base`` replaces everything up to and including the `data` segment. The value's own tail below
    `data` and its trailing-separator convention are BOTH preserved: partitions concatenate a file
    name onto this value (`#"SourceFolder" & "\\Sample.xlsx"`), so adding or dropping a separator
    silently breaks every path built from it.

    ⚠️ **The separator comes from ``base``'s FLAVOUR, through the one composer both scripts share**
    (:func:`path_flavour.join`), never from a literal backslash and never from "does the string
    contain one". A POSIX directory whose name legitimately contains a backslash -
    `/var/tmp/customer\\name/Book` - reads as Windows to that heuristic and gets a mixed value back
    (round-2 finding 4); flavour is decided by the ROOT of the path, which is the only part of it
    that carries the answer.

    A path-shaped value with no `data` segment is returned as a finding rather than skipped - this
    script cannot tell where such a parameter should point, and saying nothing is how a model drifts
    out of coverage unnoticed.
    """
    rewrites = 0
    untouched: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        nonlocal rewrites
        value = match.group(2)
        if not _is_pathish(value):
            return match.group(0)
        tail = _data_tail(value)
        if tail is None:
            untouched.append(value)
            return match.group(0)
        rewrites += 1
        segments = [DATA_SEGMENT, tail] if tail else [DATA_SEGMENT]
        rerooted = flavour_join(base, *segments, trailing=value.endswith(("\\", "/")))
        return f"{match.group(1)}{rerooted}{match.group(3)}"

    return EXPRESSION_RE.sub(_sub, text), rewrites, untouched


def _rewrite(expr_file: Path, sanitize: bool) -> bool:
    text = expr_file.read_text(encoding="utf-8")
    tree, slug = _tree_and_slug_for(expr_file)
    segments = [*tree.split("\\"), slug]
    # The SANITIZED value is pinned to backslashes deliberately: it is the byte-identical placeholder
    # this repo commits, and the deliverable it lives in is opened in Power BI Desktop, which is
    # Windows-only. Composing it with the host separator would make `--sanitize` rewrite every
    # committed model differently depending on who ran it. The LOCALIZED value is the opposite case -
    # it names this machine, so it takes this machine's flavour, through the shared composer.
    base = "\\".join([PLACEHOLDER, *segments]) if sanitize else flavour_join(str(REPO_ROOT), *segments)

    new_text, rewrites, untouched = _rewritten(text, base)
    for value in untouched:
        print(
            f"  WARN {expr_file.relative_to(REPO_ROOT)} has a path parameter with no `{DATA_SEGMENT}` segment: {value}"
        )
    if rewrites == 0:
        print(f"  WARN no data-folder expression in {expr_file.relative_to(REPO_ROOT)}")
        return False
    if new_text != text:
        expr_file.write_text(new_text, encoding="utf-8")
        print(f"  set {slug} -> {flavour_join(base, DATA_SEGMENT, trailing=True)}")
        return True
    return False


def _package(
    root: str | Path, *, inspect: bool = False, sanitize: bool = False, providers: tuple[str, ...] = ()
) -> int:
    """Delegate package ownership locally; checkout rewriting remains independent."""
    from package_unit import bind_package  # pylint: disable=import-outside-toplevel

    result = bind_package(
        str(root), rewrite=_rewritten, inspect=inspect, sanitize=sanitize, provider_packages=providers
    )
    print(json.dumps(result.as_dict(), ensure_ascii=True))
    return result.exit_code


def _unresolved(text: str) -> list[str]:
    """Every data-folder value in ``text`` that does not name an existing directory."""
    return [
        f"the folder parameter names a directory that does not exist: {match.group(2)}"
        for match in EXPRESSION_RE.finditer(text)
        if _is_pathish(match.group(2))
        and _data_tail(match.group(2)) is not None
        and not Path(match.group(2).rstrip("\\/")).is_dir()
    ]


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


def main(argv: list[str] | None = None) -> int:
    """Parse checkout modes or locally delegate package bind / inspect / transactional sanitize."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sanitize", action="store_true", help="restore the <REPO_ROOT> placeholder before committing")
    group.add_argument("--check", action="store_true", help="CI gate: fail if any tracked file leaks an absolute path")
    group.add_argument("--inspect", action="store_true", help="read-only current package binding inspection")
    parser.add_argument(
        "--package",
        metavar="ABS",
        help="bind ONE handover package's model to its own data/, wherever the package now lives",
    )
    parser.add_argument("--provider-package", action="append", default=[], metavar="ABS")
    args = parser.parse_args(argv)
    if args.package is None and (args.inspect or args.provider_package):
        parser.error("package_options_require_package")
    if args.package is not None and args.check:
        parser.error("incompatible_package_options")

    if args.package is not None:
        return _package(
            args.package, inspect=args.inspect, sanitize=args.sanitize, providers=tuple(args.provider_package)
        )

    if args.check:
        return _check()

    files = _model_expression_files()
    if not files:
        print(
            "no semantic-model expressions.tmdl files found under "
            "examples|migrations/workbooks|migrations/datasources /*/fabric/"
        )
        return 0
    mode = "sanitize (placeholder)" if args.sanitize else "localize (this checkout)"
    print(f"{mode}: {len(files)} model(s)")
    changed = sum(_rewrite(f, args.sanitize) for f in files)
    print(f"done - {changed} file(s) updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
