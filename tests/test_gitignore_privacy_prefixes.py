"""The `workshop-`/`engagement-`/`customer-` slug prefixes keep a customer unit untracked (#378)."""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ("workshop-", "engagement-", "customer-")
TREES = ("migrations/workbooks", "migrations/datasources")


def _ignored(paths: list[str]) -> set[str]:
    result = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin", "--no-index"],
        cwd=REPO_ROOT,
        input="\0".join(paths).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr.decode("utf-8", "replace")
    return {path for path in result.stdout.decode("utf-8").split("\0") if path}


def test_prefixes_ignore_the_slug_position_only() -> None:
    private = [f"migrations/{prefix}acme/fabric/x.tmdl" for prefix in PREFIXES]
    private += [
        f"{tree}/{prefix}acme/{payload}"
        for tree in TREES
        for prefix in PREFIXES
        for payload in ("fabric/Acme.SemanticModel/definition/model.tmdl", "migration-spec.json")
    ]
    tracked = [
        f"{tree}/shipping-kpis/{payload}"
        for tree in (*TREES, "examples")
        for payload in ("fabric/Acme.Report/definition/report.json", "migration-spec.json")
    ]
    tracked += [f"{TREES[0]}/shipping-kpis/fabric/{prefix}insights.SemanticModel/model.tmdl" for prefix in PREFIXES]
    tracked += ["migrations/workbooks/customer-notes.md", "docs/customer-text-exposure.md"]
    assert _ignored(private + tracked) == set(private)


def test_no_tracked_file_is_matched_by_an_exclude_rule() -> None:
    """Covers committed content; the constructed paths above cover what could never be tracked."""
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True).stdout
    assert tracked
    swept = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin", "--no-index"],
        cwd=REPO_ROOT,
        input=tracked,
        capture_output=True,
        check=False,
    )
    assert swept.returncode == 1 and not swept.stdout, swept.stdout.decode("utf-8", "replace")
