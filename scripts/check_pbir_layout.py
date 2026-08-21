"""
purpose: gate PBIR pages whose main content column is uniformly displaced below a full-height sidebar.
usage:   python scripts/check_pbir_layout.py <bundle-or-report-dir> [...] [--json <file>] [--quiet] [--warn-only]

Why this exists
---------------
Issue #278 is not a generic whitespace detector. The reported broken page still has content spanning
the overall page height because a correctly translated sidebar fills the vacated range. The signal is
column-specific: many main-content visuals share a large leading Y offset, while a separate sidebar
column starts near the top and spans through that missing range.

This gate therefore does NOT fail a single intentionally spaced visual, nor a page that simply has a
large bottom gap. It detects the confirmed downward-displacement shape only. The customer's earlier
bottom-dead-zone measurement remains unconfirmed and is deliberately out of scope.

Exit codes
----------
| 0 | scan ran and no displaced main-content column was found. |
| 1 | at least one page has a displaced dense main-content column masked by a full-height sidebar. |
| 2 | usage error (argparse) - a missing path never produces a verdict. |
| 3 | SKIPPED: no report/page/visual layout was found, so nothing was measured. |
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPORT_NAME = "pbir-layout-check.json"

STATUS_OK = "OK"
STATUS_DISPLACED = "DISPLACED_MAIN_COLUMN"
STATUS_SKIPPED = "SKIPPED"

EXIT_OK = 0
EXIT_DISPLACED = 1
EXIT_USAGE = 2
EXIT_SKIPPED = 3

MIN_MAIN_VISUALS = 6
MIN_LEADING_GAP_PX = 300.0
MIN_LEADING_GAP_RATIO = 0.12
SIDEBAR_TOP_MAX_PX = 120.0
SIDEBAR_SPAN_RATIO = 0.55
MIN_HORIZONTAL_GAP_PX = 20.0
MAX_SIDEBAR_WIDTH_RATIO = 0.35


@dataclass(frozen=True)
class Box:
    """One visual's position on a page."""

    name: str
    visual_type: str
    file: Path
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        """Right edge."""
        return self.x + self.width

    @property
    def bottom(self) -> float:
        """Bottom edge."""
        return self.y + self.height


@dataclass(frozen=True)
class Page:
    """One PBIR page and the boxes it contains."""

    report: Path
    page_dir: Path
    name: str
    display_name: str
    width: float
    height: float
    boxes: list[Box]


def find_reports(root: Path) -> list[Path]:
    """The `.Report` folders that ship under `root`, using `pbip/` for engine bundles."""
    root = root.resolve()
    if root.name.endswith(".Report"):
        return [root]
    pbip = root / "pbip"
    base = pbip if pbip.is_dir() else root
    return sorted({path.resolve() for path in base.rglob("*.Report") if path.is_dir()}, key=str)


def _number(value: Any) -> float | None:
    """Return a finite numeric value from a PBIR position property."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _valid_box(x: float | None, y: float | None, width: float | None, height: float | None) -> bool:
    """Whether all coordinates exist and the visual occupies positive area."""
    return x is not None and y is not None and width is not None and height is not None and width > 0 and height > 0


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read one JSON object, swallowing unreadable or malformed files as unmeasured."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _visual_box(path: Path) -> Box | None:
    """Parse the PBIR `position` box from one `visual.json`."""
    payload = _read_json(path)
    if payload is None:
        return None
    position = payload.get("position")
    if not isinstance(position, dict):
        return None
    x = _number(position.get("x"))
    y = _number(position.get("y"))
    width = _number(position.get("width"))
    height = _number(position.get("height"))
    if not _valid_box(x, y, width, height):
        return None
    visual = payload.get("visual") if isinstance(payload.get("visual"), dict) else {}
    visual_type = visual.get("visualType") if isinstance(visual.get("visualType"), str) else "unknown"
    name = payload.get("name") if isinstance(payload.get("name"), str) else path.parent.name
    return Box(name=name, visual_type=visual_type, file=path, x=x, y=y, width=width, height=height)


def iter_pages(report_dir: Path) -> list[Page]:
    """Read every PBIR page with numeric dimensions and positioned visuals."""
    pages_root = report_dir / "definition" / "pages"
    if not pages_root.is_dir():
        return []
    pages: list[Page] = []
    for page_json in sorted(pages_root.rglob("page.json")):
        payload = _read_json(page_json)
        if payload is None:
            continue
        width = _number(payload.get("width"))
        height = _number(payload.get("height"))
        if width is None or height is None or width <= 0 or height <= 0:
            continue
        boxes = [box for box in (_visual_box(path) for path in sorted(page_json.parent.rglob("visual.json"))) if box]
        name = payload.get("name") if isinstance(payload.get("name"), str) else page_json.parent.name
        display = payload.get("displayName") if isinstance(payload.get("displayName"), str) else name
        pages.append(
            Page(
                report=report_dir,
                page_dir=page_json.parent,
                name=name,
                display_name=display,
                width=width,
                height=height,
                boxes=boxes,
            )
        )
    return pages


def _overlap_width(left: Box, right: Box) -> float:
    """Horizontal overlap width for two visual boxes."""
    return max(0.0, min(left.right, right.right) - max(left.x, right.x))


def _same_column(left: Box, right: Box) -> bool:
    """Whether two visuals belong to the same x-column."""
    overlap = _overlap_width(left, right)
    return overlap >= min(left.width, right.width) * 0.45


def _column_groups(boxes: list[Box]) -> list[list[Box]]:
    """Cluster visuals by horizontal overlap, enough for offline layout triage."""
    groups: list[list[Box]] = []
    for box in sorted(boxes, key=lambda item: (item.x, item.y, item.width)):
        for group in groups:
            if any(_same_column(box, existing) for existing in group):
                group.append(box)
                break
        else:
            groups.append([box])
    return groups


def _has_sidebar(page: Page, main_boxes: list[Box], leading_y: float) -> Box | None:
    """Find a separate full-height sidebar masking the main column's leading gap."""
    main_left = min(box.x for box in main_boxes)
    main_right = max(box.right for box in main_boxes)
    for box in page.boxes:
        separated = box.right <= main_left - MIN_HORIZONTAL_GAP_PX or box.x >= main_right + MIN_HORIZONTAL_GAP_PX
        if not separated:
            continue
        if box.y > SIDEBAR_TOP_MAX_PX:
            continue
        if box.bottom < max(leading_y, page.height * SIDEBAR_SPAN_RATIO):
            continue
        if box.width > page.width * MAX_SIDEBAR_WIDTH_RATIO:
            continue
        return box
    return None


def _page_findings(page: Page) -> list[dict[str, Any]]:
    """Detect displaced main-content columns on one page."""
    findings: list[dict[str, Any]] = []
    if len(page.boxes) < MIN_MAIN_VISUALS + 1:
        return findings
    min_gap = max(MIN_LEADING_GAP_PX, page.height * MIN_LEADING_GAP_RATIO)
    for group in _column_groups(page.boxes):
        if len(group) < MIN_MAIN_VISUALS:
            continue
        leading_y = min(box.y for box in group)
        if leading_y < min_gap:
            continue
        sidebar = _has_sidebar(page, group, leading_y)
        if sidebar is None:
            continue
        findings.append(
            {
                "page": page.name,
                "display_name": page.display_name,
                "report": str(page.report),
                "page_dir": str(page.page_dir),
                "main_visuals": len(group),
                "leading_y": leading_y,
                "main_column": {
                    "left": min(box.x for box in group),
                    "right": max(box.right for box in group),
                    "top": leading_y,
                    "bottom": max(box.bottom for box in group),
                },
                "sidebar": {
                    "name": sidebar.name,
                    "visual_type": sidebar.visual_type,
                    "x": sidebar.x,
                    "y": sidebar.y,
                    "width": sidebar.width,
                    "height": sidebar.height,
                    "bottom": sidebar.bottom,
                    "file": str(sidebar.file),
                },
                "files": [str(box.file) for box in sorted(group, key=lambda item: (item.y, item.x))],
                "detail": (
                    "dense main-content column starts far below page top while a separate sidebar spans that range"
                ),
            }
        )
    return findings


def scan_report(report_dir: Path) -> dict[str, Any]:
    """Scan one `.Report` for displaced main-content columns."""
    pages = iter_pages(report_dir)
    findings = [finding for page in pages for finding in _page_findings(page)]
    return {
        "report": str(report_dir),
        "status": STATUS_DISPLACED if findings else STATUS_OK,
        "pages_scanned": len(pages),
        "visuals_scanned": sum(len(page.boxes) for page in pages),
        "findings": findings,
    }


def scan(root: Path) -> dict[str, Any]:
    """Scan every shipping report under one path."""
    return merge([scan_report(report_dir) for report_dir in find_reports(root)])


def merge(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-report reports into one verdict."""
    measured = [report for report in reports if report["pages_scanned"] and report["visuals_scanned"]]
    failing = [report for report in measured if report["status"] == STATUS_DISPLACED]
    if not measured:
        status = STATUS_SKIPPED
    else:
        status = STATUS_DISPLACED if failing else STATUS_OK
    return {
        "status": status,
        "reports_scanned": len(measured),
        "reports_with_displaced_columns": len(failing),
        "pages_scanned": sum(report["pages_scanned"] for report in measured),
        "visuals_scanned": sum(report["visuals_scanned"] for report in measured),
        "findings": sum(len(report["findings"]) for report in measured),
        "reports": measured,
        "skipped": [report for report in reports if not (report["pages_scanned"] and report["visuals_scanned"])],
    }


def render(report: dict[str, Any]) -> str:
    """Human-readable verdict, matching sibling offline gates."""
    if report["status"] == STATUS_SKIPPED:
        return "PBIR LAYOUT CHECK: SKIPPED - nothing measured (no report/page/visual layout found)"
    if report["status"] == STATUS_OK:
        return (
            f"PBIR LAYOUT CHECK: OK - no displaced main-content column in {report['pages_scanned']} page(s), "
            f"{report['visuals_scanned']} visual(s)."
        )
    lines = [
        f"PBIR LAYOUT CHECK: DISPLACED_MAIN_COLUMN - {report['findings']} page column(s) in "
        f"{report['reports_with_displaced_columns']} of {report['reports_scanned']} report(s)."
    ]
    for one in report["reports"]:
        if one["status"] != STATUS_DISPLACED:
            continue
        lines.append(f"  {Path(one['report']).name}")
        for finding in one["findings"]:
            sidebar = finding["sidebar"]
            lines.append(
                f"    - {finding['display_name']}: {finding['main_visuals']} main visual(s) start at "
                f"y={finding['leading_y']:.2f}; sidebar {sidebar['name']} spans "
                f"y={sidebar['y']:.2f}..{sidebar['bottom']:.2f}"
            )
    lines.append(
        "  Detects the confirmed downward-displacement shape only; the earlier bottom-dead-zone "
        "measurement remains unconfirmed."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle folder(s) or .Report folder(s)")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0 after a successful scan")
    args = parser.parse_args(argv)

    if not args.paths:
        parser.error("give a bundle/report path")
    for path in args.paths:
        if not path.is_dir():
            parser.error(f"{path} is not a directory")

    merged = merge([report for path in args.paths for report in scan(path)["reports"]])
    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged))
    if args.warn_only:
        return EXIT_OK
    if merged["status"] == STATUS_SKIPPED:
        return EXIT_SKIPPED
    if merged["status"] == STATUS_DISPLACED:
        return EXIT_DISPLACED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
