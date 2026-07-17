"""
purpose: Capture reference screenshots of published Tableau Public dashboards via Playwright
         (live-render, then full-page screenshot). Used to give the pbi-report-builder and
         pbi-migration-validator a visual "front-end to work towards" and to diff against.
usage:   python scripts/capture_tableau_screenshots.py <batch.json>
         where batch.json is a list of {"url": "<tableau public viz url>", "out": "<png path>"}.
         The Tableau static-image endpoint returns an HTML bootstrap (not a PNG) without a live
         session, so we render the interactive viz and screenshot it instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("capture_tableau_screenshots")

COOKIE_SELECTORS = ["#onetrust-accept-btn-handler", "button:has-text('Accept')"]
VIZ_SELECTORS = "iframe, canvas, .tab-widget, tableau-viz"


def capture_one(page, url: str, out_path: Path, wait_ms: int) -> int:
    """Render a single Tableau Public viz URL and save a full-page screenshot. Returns file size."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    for sel in COOKIE_SELECTORS:
        try:
            page.click(sel, timeout=2500)
            break
        except PlaywrightError:
            continue
    try:
        page.wait_for_selector(VIZ_SELECTORS, timeout=30000)
    except PlaywrightError as e:
        logger.warning("viz selector not matched for %s (%s) - screenshotting anyway", url, str(e)[:60])
    page.wait_for_timeout(wait_ms)
    page.screenshot(path=str(out_path), full_page=True)
    size = out_path.stat().st_size
    logger.info("Saved %s (%d bytes)", out_path, size)
    return size


def main() -> None:
    """Parse args, then render + screenshot every {url, out} job in the batch JSON file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path, help="JSON list of {url, out} capture jobs")
    parser.add_argument("--wait-ms", type=int, default=12000, help="settle time after load before screenshot")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    args = parser.parse_args()

    jobs = json.loads(args.batch.read_text(encoding="utf-8"))
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        for job in jobs:
            try:
                size = capture_one(page, job["url"], Path(job["out"]), args.wait_ms)
                if size < 20000:  # a real rendered viz is >>20KB; smaller usually means a blank/bootstrap page
                    logger.warning("Screenshot for %s is suspiciously small (%d bytes)", job["out"], size)
            except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                # Batch resilience: one bad viz URL must not abort the whole capture run.
                failures += 1
                logger.error("Failed to capture %s: %s", job.get("url"), str(e)[:120])
        browser.close()
    if failures:
        logger.error("%d capture(s) failed", failures)
        sys.exit(1)


if __name__ == "__main__":
    main()
