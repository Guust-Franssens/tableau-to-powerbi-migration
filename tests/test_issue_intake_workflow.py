"""Controls for issue-intake workflow concurrency isolation."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".github" / "workflows" / "issue-intake.md"
LOCK = ROOT / ".github" / "workflows" / "issue-intake.lock.yml"
EXPECTED_GROUP = (
    "issue-intake-${{ github.event.issue.number }}-${{ "
    "(github.event.action != 'labeled' || "
    "github.event.label.name == 'status:ready-for-implementation') "
    "&& 'accepted' || github.run_id }}"
)


def _source_frontmatter(path: Path) -> dict:
    """Load a workflow's YAML frontmatter."""
    document = path.read_text(encoding="utf-8")
    return yaml.safe_load(document.split("---", 2)[1])


def _group(issue_number: int, action: str, label: str | None, run_id: int) -> str:
    """Model the source expression's accepted-event and excluded-event branches."""
    accepted = action != "labeled" or label == "status:ready-for-implementation"
    discriminator = "accepted" if accepted else str(run_id)
    return f"issue-intake-{issue_number}-{discriminator}"


def test_excluded_label_events_cannot_cancel_accepted_intake_runs() -> None:
    """Accepted events serialize together; ignored label events are outside that group."""
    source_group = _source_frontmatter(SOURCE)["concurrency"]["group"]
    compiled_group = yaml.safe_load(LOCK.read_text(encoding="utf-8"))["concurrency"]["group"]

    assert source_group == EXPECTED_GROUP
    assert compiled_group == source_group
    opened = _group(17, "opened", None, 100)
    ready_label = _group(17, "labeled", "status:ready-for-implementation", 101)
    ignored_label = _group(17, "labeled", "status:needs-decision", 102)
    later_ignored_label = _group(17, "labeled", "status:needs-decision", 103)

    assert opened == ready_label
    assert ignored_label != opened
    assert later_ignored_label != ignored_label
