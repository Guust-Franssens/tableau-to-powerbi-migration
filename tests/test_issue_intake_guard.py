"""Behavioral controls for issue-intake invalidation and stale-target refusal."""

from copy import deepcopy
import json
from pathlib import Path
import sys
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import issue_intake_guard as intake  # noqa: E402  # pylint: disable=wrong-import-position


def issue_event(action: str = "edited") -> dict:
    """A synthetic event with independent routing, approval and priority labels."""
    return {
        "action": action,
        "repository": {"full_name": "example/toolkit"},
        "label": {"name": intake.READY},
        "issue": {
            "number": 17,
            "state": "open",
            "title": "Offline parser fix",
            "body": "Synthetic issue body",
            "labels": [
                {"name": name}
                for name in ("env:cloud-ok", "needs:desktop", intake.READY, "status:plan-approved", "priority:next")
            ],
        },
    }


@pytest.mark.parametrize(
    ("action", "expected_removed"),
    [
        ("edited", ["env%3Acloud-ok", "needs%3Adesktop", "status%3Aready-for-implementation"]),
        ("labeled", ["env%3Acloud-ok", "needs%3Adesktop"]),
    ],
)
def test_prepare_removes_only_stale_labels(action: str, expected_removed: list[str]) -> None:
    """Scope edits revoke readiness; explicit ready events preserve human approval."""
    event = issue_event(action)
    calls = []

    def request(method: str, path: str) -> dict:
        calls.append((method, path))
        return deepcopy(event["issue"])

    intake.guard(event, "prepare", request)
    assert calls == [("GET", "issues/17")] + [("DELETE", f"issues/17/labels/{label}") for label in expected_removed]


@pytest.mark.parametrize(
    "change",
    [{"title": "Changed"}, {"body": "Changed"}, {"state": "closed"}, {"pull_request": {}}, {"number": 18}],
)
@pytest.mark.parametrize("phase", ["prepare", "check"])
def test_changed_or_wrong_target_refuses_without_writes(change: dict, phase: str) -> None:
    """Neither phase can operate on a different revision, PR, or closed issue."""
    event = issue_event()
    calls = []

    def request(method: str, path: str) -> dict:
        calls.append((method, path))
        return {**event["issue"], **change}

    with pytest.raises(ValueError):
        intake.guard(event, phase, request)
    assert calls == [("GET", "issues/17")]


def test_check_reads_without_clearing_successful_classification() -> None:
    """The publication check must not remove the routing established by preparation."""
    event = issue_event()
    calls = []

    def request(method: str, path: str) -> dict:
        calls.append((method, path))
        return {**event["issue"], "labels": []}

    intake.guard(event, "check", request)
    assert calls == [("GET", "issues/17")]


def test_unrelated_label_event_is_not_an_intake_trigger() -> None:
    """Our own label writes cannot start a classification loop."""
    event = issue_event("labeled")
    event["label"]["name"] = "env:cloud-ok"
    with pytest.raises(ValueError, match="readiness label"):
        intake.guard(event, "prepare", lambda *_: pytest.fail("No API call expected"))


def test_cli_refuses_api_failure_and_wrong_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Exercise CLI wiring and error exit without making any network request."""
    event = issue_event()
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/other")
    monkeypatch.setattr("sys.argv", ["issue_intake_guard.py", "prepare"])

    def refused(method: str, path: str) -> None:
        assert (method, path) == ("GET", "issues/17")
        raise HTTPError("https://api.github.com", 403, "Forbidden", {}, None)

    monkeypatch.setattr(intake, "github_request", refused)
    assert intake.main() == 1
    assert "repository does not match" in caplog.text
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/toolkit")
    assert intake.main() == 1
    assert "HTTP 403" in caplog.text
    assert event["issue"]["body"] not in caplog.text
