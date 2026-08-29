"""Behavioural regression tests for the `workshop-`/`engagement-`/`customer-` privacy prefixes.

THIS REPOSITORY IS PUBLIC. The three prefixes are the documented opt-out an operator uses to keep a
real customer migration on their own disk: prefix the slug, and the whole unit stays untracked. That
opt-out was broken. The rules were root-anchored at DEPTH 1 (`/migrations/customer-*/`) while the
trees `AGENTS.md` instructs are DEPTH 2 (`migrations/workbooks/<slug>/`,
`migrations/datasources/<slug>/`), so a prefixed slug matched nothing and the entire unit was
tracked - `fabric/**` (TMDL partitions carrying server and database names, M queries, PBIR) and
`migration-spec.json` included (issue #378).

The same class of bug had already been found, written up and fixed for `migration-brief.md` four
lines below in the same file, and was not generalised to its own neighbours. That is why this suite
exists: it pins the behaviour rather than the prose.

Everything here is judged by `git check-ignore`'s EXIT CODE (0 = ignored, 1 = not ignored). That
command prints nothing under `-q`, so a truthiness test on its stdout silently reads every path as
"not ignored" - a mistake already made once against this very file.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The two user trees `AGENTS.md` instructs operators to create. Both are one level deeper than the
#: original depth-1 rules assumed, which is the whole defect.
USER_TREES = ("migrations/workbooks", "migrations/datasources")

PRIVACY_PREFIXES = ("workshop-", "engagement-", "customer-")

#: Deliberately TRACKED payloads for an unprefixed slug - the interaction that makes this fix hard.
#: Widening the prefix rules must not swallow these, or the worked examples disappear.
DELIVERABLES = (
    "fabric/Acme.SemanticModel/definition/tables/Sales.tmdl",
    "fabric/Acme.Report/definition/report.json",
    "migration-spec.json",
)


def _is_ignored(path: str) -> bool:
    """True when git's exclude rules match `path`. Judged by exit code, never by stdout.

    `--no-index` is deliberate. `git check-ignore` consults the index by default and reports an
    already-TRACKED path as not-ignored whatever the patterns say. Measured on a throwaway repo
    whose `.gitignore` is just `customer-*/`, with `customer-acme/x.tmdl` committed: default
    `check-ignore -q` exits **1** (not ignored), `--no-index` exits **0** (ignored). Without the
    flag, `test_no_tracked_file_is_matched_by_an_exclude_rule` below could not fail even if a rule
    started matching a committed deliverable - it would be masked by the very tracking it exists to
    protect.
    """
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), f"git check-ignore failed on {path!r}: {result.stderr.strip()}"
    return result.returncode == 0


def _matching_rule(path: str) -> str:
    """The `.gitignore:<line>:<pattern>` that decided `path`, for failure messages."""
    result = subprocess.run(
        ["git", "check-ignore", "-v", "--no-index", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "<no rule matched>"


@pytest.mark.parametrize("tree", USER_TREES)
@pytest.mark.parametrize("prefix", PRIVACY_PREFIXES)
@pytest.mark.parametrize("payload", DELIVERABLES)
def test_prefixed_slug_is_ignored_in_the_depth_2_user_trees(tree: str, prefix: str, payload: str) -> None:
    """The defect itself: the documented opt-out must fire at the depth the docs instruct.

    `fabric/**` and `migration-spec.json` are the tracked-on-purpose deliverables, so they are the
    exact payloads a prefixed unit must take with it when it opts out.
    """
    path = f"{tree}/{prefix}acme/{payload}"
    assert _is_ignored(path), f"PRIVACY LEAK: {path} is NOT ignored on a public repo"


@pytest.mark.parametrize("prefix", PRIVACY_PREFIXES)
def test_prefixed_slug_is_still_ignored_at_depth_1(prefix: str) -> None:
    """Do not regress what already worked: the old rules matched `migrations/<prefix>*/`."""
    path = f"migrations/{prefix}acme/fabric/x.tmdl"
    assert _is_ignored(path), f"PRIVACY REGRESSION: {path} is NOT ignored"


@pytest.mark.parametrize("tree", USER_TREES + ("examples",))
@pytest.mark.parametrize("payload", DELIVERABLES)
def test_unprefixed_slug_stays_tracked(tree: str, payload: str) -> None:
    """The other half of the fix, and the half that is easy to break.

    Migration deliverables are tracked ON PURPOSE - that is how the worked examples in `examples/`
    exist. A prefix rule broad enough to fire at depth 2 must not also swallow an ordinary slug.
    """
    path = f"{tree}/shipping-kpis/{payload}"
    assert not _is_ignored(path), (
        f"{path} became ignored - migration deliverables are tracked on purpose. Matched: {_matching_rule(path)}"
    )


def test_prefix_rules_match_directories_only() -> None:
    """The trailing `/` is load-bearing: a FILE whose name starts with a prefix stays tracked.

    `docs/customer-text-exposure.md` is a real committed doc. Dropping the trailing slash from
    `**/customer-*/` would start matching it (and any future `customer-*.md`), which is how a
    privacy fix turns into a silent un-tracking of toolkit content.
    """
    tracked_doc = "docs/customer-text-exposure.md"
    assert (REPO_ROOT / tracked_doc).is_file(), f"{tracked_doc} moved - re-pick a tracked customer-* FILE"
    assert not _is_ignored(tracked_doc), f"{tracked_doc} is ignored. Matched: {_matching_rule(tracked_doc)}"
    assert not _is_ignored("migrations/workbooks/customer-notes.md")


def test_no_tracked_file_is_matched_by_an_exclude_rule() -> None:
    """Nothing already committed may become ignored - the regression this whole change risks.

    Sweeps every tracked path through `check-ignore --no-index` in one batch. NUL-separated so a
    path with a space or a newline cannot corrupt the comparison. Exit 1 means "no path matched",
    which is the passing state.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    assert tracked.stdout, "git ls-files returned nothing - wrong cwd?"

    swept = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin", "--no-index"],
        cwd=REPO_ROOT,
        input=tracked.stdout,
        capture_output=True,
        check=False,
    )
    assert swept.returncode in (0, 1), f"git check-ignore failed: {swept.stderr.decode('utf-8', 'replace')}"

    leaked = [p for p in swept.stdout.decode("utf-8", "replace").split("\0") if p]
    assert swept.returncode == 1 and not leaked, (
        "these TRACKED files are now matched by an exclude rule, which un-tracks committed "
        f"content on the next clone: {leaked}"
    )


def test_this_test_file_is_tracked() -> None:
    """A gate that reads git-tracked state is inert until the gate itself is committed.

    Doubly true here: the subject under test IS the ignore rules, so a self-ignoring test file
    would pass locally and never run in CI.
    """
    assert not _is_ignored("tests/test_gitignore_privacy_prefixes.py")
    listed = subprocess.run(
        ["git", "ls-files", "--", "tests/test_gitignore_privacy_prefixes.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert listed.stdout.strip(), "this test file is not tracked - `git add` it or CI will never run it"
