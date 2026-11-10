"""
purpose: Invalidate stale issue routing and reject changed targets before gh-aw publishes.
usage: python scripts/issue_intake_guard.py prepare|check
"""

import argparse
from collections.abc import Callable
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

READY = "status:ready-for-implementation"
ROUTING_LABELS = {
    "env:cloud-ok",
    "env:local-only",
    "needs:desktop",
    "needs:tableau-server",
    "needs:engine-plugin",
    "needs:credentials",
    "needs:protected-files",
    "needs:details",
}


def guard(event: dict[str, Any], phase: str, request: Callable[[str, str], Any]) -> None:
    """Only mutate the triggering, unchanged open issue and our fixed label set."""
    action = event["action"]
    expected = event["issue"]
    number = expected["number"]
    if phase not in {"prepare", "check"} or action not in {"opened", "edited", "reopened", "labeled"}:
        raise ValueError("Unsupported intake phase or event")
    if action == "labeled" and event["label"]["name"] != READY:
        raise ValueError("Only the readiness label can trigger reclassification")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError("Invalid issue number")
    current = request("GET", f"issues/{number}")
    for snapshot in (expected, current):
        if snapshot.get("state") != "open" or "pull_request" in snapshot or snapshot.get("number") != number:
            raise ValueError("Target is not the triggering open issue")
    if any(current.get(field) != expected.get(field) for field in ("title", "body")):
        raise ValueError("Issue changed since this event; a newer intake must handle it")
    if phase == "prepare":
        stale = set(ROUTING_LABELS)
        if action != "labeled":
            stale.add(READY)
        labels = {label["name"] for label in current["labels"]}
        for label in sorted(stale & labels):
            request("DELETE", f"issues/{number}/labels/{quote(label, safe='')}")
    logging.info("Issue intake %s passed for #%d", phase, number)


def github_request(method: str, path: str) -> Any:
    """Use only the Actions token and the current repository's GitHub API."""
    repository = os.environ["GITHUB_REPOSITORY"]
    request = Request(
        f"https://api.github.com/repos/{repository}/{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response) if method == "GET" else None


def main() -> int:
    """Read trusted Actions event context and fail visibly without publishing issue text."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "check"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if event["repository"]["full_name"].casefold() != os.environ["GITHUB_REPOSITORY"].casefold():
            raise ValueError("Event repository does not match the workflow repository")
        guard(event, args.phase, github_request)
    except HTTPError as exc:
        logging.error("Issue intake refused: GitHub API HTTP %d; check Actions permissions", exc.code)
        return 1
    except (OSError, URLError) as exc:
        logging.error("Issue intake refused: %s while reading event or contacting GitHub", type(exc).__name__)
        return 1
    except (KeyError, ValueError) as exc:
        logging.error("Issue intake refused: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
