"""
purpose: Harvest real Tableau Public workbooks from the Discover feed and stress-test the parser
         against them, to surface real-world edge cases (new data_type values, unseen zone/shelf
         idioms, schema gaps) the way the airline 'spatial'/'table' and metadata-record gaps were found.
usage:   # 1) collect candidate workbook ids from the Discover API (needs playwright):
         python scripts/harvest_tableau_public.py discover --out candidates.json
         # 2) download a diverse subset and run the parser over each, writing a triage report:
         python scripts/harvest_tableau_public.py triage candidates.json --out-dir _harvest --per-channel 2

A Tableau Public workbook downloads (as a full .twbx, data included) from
https://public.tableau.com/workbooks/<workbookRepoUrl>.twb regardless of the .twb suffix.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_tableau import (  # noqa: E402  # pylint: disable=wrong-import-position
    parse_workbook,
    validate_spec,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("harvest_tableau_public")

DEFAULT_SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "migration-spec.schema.json"
WORKBOOK_URL = "https://public.tableau.com/workbooks/{repo}.twb"
DISCOVER_URL = "https://public.tableau.com/app/discover"


def discover(args: argparse.Namespace) -> None:
    """Intercept the Discover feed's BFF API responses and save unique workbook candidates."""
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel  # lazy

    bodies: list[tuple[str, Any]] = []

    def on_response(resp):
        url = resp.url
        if "public.tableau.com" not in url or "application/json" not in resp.headers.get("content-type", ""):
            return
        if not any(seg in url for seg in ("/bff/v1/trending", "/discover/v2/vizzes/", "/discover/v3/authors/")):
            return
        try:
            bodies.append((url, resp.json()))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1400})
        page.on("response", on_response)
        page.goto(DISCOVER_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(args.scrolls):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(1100)
        page.wait_for_timeout(3000)
        browser.close()

    candidates: dict[str, dict[str, Any]] = {}
    for url, body in bodies:
        channel = url.split("/")[-1].split("?")[0]
        items = body if isinstance(body, list) else (body.get("contents", []) if isinstance(body, dict) else [])
        for item in items:
            if not isinstance(item, dict):
                continue
            repo = item.get("workbookRepoUrl") or item.get("vizRepoUrl")
            if repo and repo not in candidates:
                candidates[repo] = {
                    "workbookRepoUrl": repo,
                    "defaultViewRepoUrl": item.get("defaultViewRepoUrl"),
                    "title": item.get("title"),
                    "author": item.get("authorProfileName") or item.get("authorDisplayName"),
                    "viewCount": item.get("viewCount"),
                    "channel": channel,
                }
    args.out.write_text(json.dumps(list(candidates.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Harvested %d unique workbooks -> %s", len(candidates), args.out)


def _select(candidates: list[dict[str, Any]], per_channel: int, limit: int | None) -> list[dict[str, Any]]:
    """Pick a diverse subset: the top-viewed `per_channel` workbooks from each channel."""
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        by_channel.setdefault(c.get("channel") or "?", []).append(c)
    selected: list[dict[str, Any]] = []
    for items in by_channel.values():
        items.sort(key=lambda x: -(x.get("viewCount") or 0))
        selected.extend(items[:per_channel])
    selected.sort(key=lambda x: -(x.get("viewCount") or 0))
    return selected[:limit] if limit else selected


def _download(repo: str, dest: Path, max_mb: int) -> Path | None:
    """Download a workbook's .twbx; skip if it exceeds max_mb. Returns the path or None."""
    url = WORKBOOK_URL.format(repo=repo)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310  (trusted host)
            length = int(resp.headers.get("Content-Length") or 0)
            if length and length > max_mb * 1_000_000:
                logger.warning("skip %s (%.1f MB > %d MB cap)", repo, length / 1e6, max_mb)
                return None
            data = resp.read()
    except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        logger.error("download failed %s: %s", repo, str(e)[:100])
        return None
    if not data[:2] == b"PK":
        logger.warning("skip %s (not a zip/.twbx, likely not downloadable)", repo)
        return None
    dest.write_bytes(data)
    return dest


def _triage_one(c: dict[str, Any], wb_dir: Path, spec_dir: Path, max_mb: int, schema: Path) -> dict[str, Any]:
    """Download one workbook and run the parser + schema validation over it, returning a result row."""
    repo = c["workbookRepoUrl"]
    row: dict[str, Any] = {**c, "downloaded": False, "parse_ok": False, "schema_ok": False, "error": None}
    path = _download(repo, wb_dir / f"{repo}.twbx", max_mb)
    if path is None:
        row["error"] = "download-skipped-or-failed"
        return row
    row["downloaded"] = True
    row["size_mb"] = round(path.stat().st_size / 1e6, 1)
    try:
        spec = parse_workbook(path)
        row["parse_ok"] = True
        row["stats"] = {
            "data_sources": len(spec["data_sources"]),
            "worksheets": len(spec["worksheets"]),
            "dashboards": len(spec["dashboards"]),
            "parameters": len(spec["parameters"]),
            "limitations": len(spec["limitations_encountered"]),
        }
        (spec_dir / f"{repo}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            validate_spec(spec, schema)
            row["schema_ok"] = True
        except Exception as ve:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            row["error"] = f"schema: {str(ve).splitlines()[0][:200]}"
    except Exception as pe:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        row["error"] = f"parse: {type(pe).__name__}: {str(pe)[:200]}"
    return row


def triage(args: argparse.Namespace) -> None:
    """Download a diverse subset and run the parser over each, recording pass/fail + any schema gaps."""
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    selection = _select(candidates, args.per_channel, args.limit)
    wb_dir, spec_dir = args.out_dir / "twbx", args.out_dir / "specs"
    wb_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)
    results = []
    logger.info("Triaging %d of %d harvested workbooks", len(selection), len(candidates))
    for i, c in enumerate(selection, 1):
        logger.info("[%d/%d] %s (%s)", i, len(selection), c["workbookRepoUrl"], c.get("channel"))
        results.append(_triage_one(c, wb_dir, spec_dir, args.max_mb, args.schema))

    (args.out_dir / "triage.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    parse_fail = [r for r in results if r["downloaded"] and not r["parse_ok"]]
    schema_fail = [r for r in results if r["parse_ok"] and not r["schema_ok"]]
    logger.info(
        "Triage done: %d clean / %d parse-fail / %d schema-fail / %d total downloaded",
        sum(1 for r in results if r["schema_ok"]),
        len(parse_fail),
        len(schema_fail),
        sum(1 for r in results if r["downloaded"]),
    )
    for r in parse_fail + schema_fail:
        logger.info("  ISSUE %s -> %s", r["workbookRepoUrl"], r["error"])


def main() -> None:
    """CLI entry point: 'discover' harvests candidates, 'triage' downloads + parses a subset."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="harvest candidate workbook ids from the Discover feed")
    d.add_argument("--out", type=Path, default=Path("candidates.json"))
    d.add_argument("--scrolls", type=int, default=12)
    t = sub.add_parser("triage", help="download a diverse subset and run the parser over each")
    t.add_argument("candidates", type=Path)
    t.add_argument("--out-dir", type=Path, default=Path("_harvest"))
    t.add_argument("--per-channel", type=int, default=2)
    t.add_argument("--max-mb", type=int, default=80)
    t.add_argument("--limit", type=int, default=None)
    t.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args()

    (discover if args.cmd == "discover" else triage)(args)


if __name__ == "__main__":
    main()
