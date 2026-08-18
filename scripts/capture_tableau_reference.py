"""
purpose: Acquire a provenance-stamped reference image of the SOURCE Tableau dashboard(s) for a
         migration, so pbi-report-builder can mimic the original and pbi-migration-validator can grade
         fidelity against immutable ground truth. See docs/reference-capture.md for the full design.
usage:   python scripts/capture_tableau_reference.py <tree>/<slug> [--public-url URL --view NAME]
                                                       [--server-rest] [--structural-only] [--force]

Providers, resolved by FITNESS (not availability):
  - public_playwright   : Tableau Public only (implemented; needs --public-url + --view)
  - embedded_thumbnail  : extract thumbnails baked into the .twb (implemented; layout-hint only)
  - manual              : user-dropped screenshots already in reference/ (implemented; validate + hash)
  - server_rest         : Tableau Server/Cloud REST image export (provider NOT wired; the transport is
                          implemented and live-tested in capture_tableau_oracle.py --images, which
                          calls the same /views/{id}/image?resolution=high endpoint. What is missing
                          here is the provider CONTRACT: the provenance manifest and state-pinning.)

Default is FAIL CLOSED: if nothing can produce a reference the script exits non-zero and asks for a
source, unless --structural-only is passed (which records a blocked manifest and cannot claim
visual fidelity). Secrets are read only from the environment, never from args/spec/logs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("capture-reference")

# Capabilities a provider's output is fit FOR (see docs/reference-capture.md). Only outputs carrying
# "validation_grade" may be used by the validator to sign off visual fidelity.
CAP_LAYOUT = "layout_grade"
CAP_TEXT = "text_readable"
CAP_STATE = "state_reproducible"
CAP_REVISION = "revision_bound"
CAP_VALIDATION = "validation_grade"

# Node/Playwright capture script. Kept inline so the tool is a single file; Chromium is already
# installed for this repo. Uses the documented Tableau-Public technique (domcontentloaded + explicit
# timeouts + dismiss OneTrust; Tableau Public never reaches networkidle).
_CAPTURE_JS = r"""
const { chromium } = require("playwright");
(async () => {
  const [url, out] = [process.argv[2], process.argv[3]];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 2 });
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3000);
    for (const sel of ["#onetrust-reject-all-handler", "#onetrust-accept-btn-handler"]) {
      const b = await page.$(sel);
      if (b) { await b.click().catch(() => {}); break; }
    }
    await page.waitForTimeout(14000);
    await page.screenshot({ path: out, fullPage: true });
    console.log("OK");
  } catch (e) {
    console.log("ERR " + e.message);
    process.exitCode = 2;
  } finally {
    await browser.close();
  }
})();
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _png_dimensions(path: Path) -> dict[str, int] | None:
    """Read pixel width/height from a PNG's IHDR chunk without extra dependencies."""
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", header[16:24])
        return {"w": width, "h": height, "dpr": 2}
    except OSError:
        return None


def _is_valid_png(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 20000 and _png_dimensions(path) is not None


def _slug_for(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "dashboard"


def _source_workbook(slug_dir: Path) -> Path | None:
    # 'source/' is this repo's per-workbook convention; 'in/' is the estate-bundle layout the
    # dispatcher lays down (in/ + out/), which otherwise silently yielded "no workbook found" and
    # skipped the embedded-thumbnail provider even though the .twbx was right there.
    for sub in ("source", "in"):
        src = slug_dir / sub
        if not src.is_dir():
            continue
        for pattern in ("*.twbx", "*.twb"):
            found = sorted(src.glob(pattern))
            if found:
                return found[0]
    return None


def _dashboard_names(slug_dir: Path) -> list[str]:
    spec = slug_dir / "migration-spec.json"
    if not spec.is_file():
        return []
    try:
        data = json.loads(spec.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [d.get("name", "dashboard") for d in data.get("dashboards", [])]


def capture_public_playwright(public_url: str, view: str, out_path: Path) -> dict | None:
    """Capture a Tableau PUBLIC view via headless Chromium. Returns a state-record dict or None."""
    node = shutil.which("node")
    if not node:
        log.error("node not found on PATH - cannot run the Playwright provider")
        return None
    url = f"https://public.tableau.com/views/{public_url}/{view}?:showVizHome=no&:embed=y"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(_CAPTURE_JS)
        js_path = tmp.name
    try:
        proc = subprocess.run(
            [node, js_path, url, str(out_path)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        log.info("playwright: %s", (proc.stdout or proc.stderr).strip()[:200])
    except subprocess.TimeoutExpired:
        log.error("playwright capture timed out for %s", url)
        return None
    finally:
        Path(js_path).unlink(missing_ok=True)
    if not _is_valid_png(out_path):
        log.error("playwright produced no valid PNG (likely an error/consent page, not the viz)")
        return None
    return {
        "provider": "public_playwright",
        # A public full-page scrape is a good layout/text reference but NOT validation-grade ground
        # truth (canvas render, page chrome, single default state).
        "capabilities": [CAP_LAYOUT, CAP_TEXT],
    }


def extract_embedded_thumbnail(twb_or_twbx: Path, out_dir: Path) -> list[dict] | None:
    """Extract <thumbnails> images baked into a .twb/.twbx. Layout hint only; low-res and possibly stale."""
    try:
        if twb_or_twbx.suffix.lower() == ".twbx":
            with zipfile.ZipFile(twb_or_twbx) as archive:
                members = [n for n in archive.namelist() if n.lower().endswith(".twb")]
                if not members:
                    return None
                xml = archive.read(members[0]).decode("utf-8", "ignore")
        else:
            xml = twb_or_twbx.read_text(encoding="utf-8", errors="ignore")
    except (OSError, zipfile.BadZipFile):
        return None
    thumbs = re.findall(r"<thumbnail\b[^>]*\bname='([^']+)'[^>]*>\s*([A-Za-z0-9+/=\s]+?)\s*</thumbnail>", xml)
    if not thumbs:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for name, payload in thumbs:
        try:
            raw = base64.b64decode("".join(payload.split()))
        except (ValueError, TypeError):
            continue
        target = out_dir / f"{_slug_for(name)}.png"
        target.write_bytes(raw)
        records.append({"name": name, "image": target, "provider": "embedded_thumbnail", "capabilities": [CAP_LAYOUT]})
    return records or None


def collect_manual(reference_dir: Path) -> list[Path]:
    """Return user-dropped source screenshots already present (tableau-*.png), excluding our outputs."""
    if not reference_dir.is_dir():
        return []
    return sorted(
        p for p in reference_dir.glob("tableau-*.png") if p.stat().st_size > 20000 and _png_dimensions(p) is not None
    )


def capture_server_rest(_slug_dir: Path) -> list[dict] | None:
    """NOT WIRED: Tableau Server/Cloud REST image export. The transport already exists elsewhere.

    ``capture_tableau_oracle.py --images`` calls the same ``/views/{id}/image?resolution=high``
    endpoint against a live site, with ``401002`` re-auth, classified transient/credential failures
    and jittered backoff. Do not reimplement it here; what is missing is this provider's *contract*.
    """
    raise NotImplementedError(
        "server_rest is not wired into this provider chain -- but the REST image transport IS "
        "implemented and live-tested. Use: python scripts/capture_tableau_oracle.py --out <dir> --images "
        "(same /views/{id}/image?resolution=high endpoint, with retry/re-auth hardening). "
        "What is still missing HERE is the provider contract, not the endpoint: the provenance "
        "manifest (layout_grade/text_readable/state_reproducible/revision_bound/validation_grade, "
        "without which the validator will not grade visual fidelity) and state-pinning via "
        "?vf_<field>=<value>. See docs/reference-capture.md and issue #194."
    )


def _write_manifest(reference_dir: Path, workbook_sha: str | None, dashboards: list[dict]) -> Path:
    reference_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "captured_at": _utcnow(),
        "source_workbook_sha256": workbook_sha,
        "dashboards": dashboards,
    }
    path = reference_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def _dashboard_record(name: str, image: Path, reference_dir: Path, state_rec: dict) -> dict:
    dims = _png_dimensions(image) or {}
    return {
        "name": name,
        "states": [
            {
                "state_slug": "default",
                "state": {},  # TODO: pin from parser parameter defaults (see docs/reference-capture.md)
                "image": str(image.relative_to(reference_dir)).replace("\\", "/"),
                "provider": state_rec["provider"],
                "capabilities": state_rec["capabilities"],
                "dimensions": dims,
                "sha256": _sha256(image),
                "numeric_oracle": None,
            }
        ],
    }


def _manual_capabilities(args: argparse.Namespace) -> list[str]:
    """Capabilities for a user-dropped screenshot: layout+text, and `validation_grade` only on request.

    A dropped file is UN-PROVENANCED by construction. Nothing here knows its resolution, whether its
    filters were pinned, whether it came from the handed-over workbook or a newer published revision,
    or even that it is a screenshot of this dashboard. This used to hardcode `validation_grade` - the
    top tier, the one `pbi-migration-validator` requires before it may sign off visual fidelity - so
    any PNG someone happened to leave in `reference/` silently outranked a live Tableau Server REST
    render (`capture_tableau_oracle.py --images`, which is honestly graded layout+text because it is
    captured in the view's DEFAULT STATE with no `?vf_` filter pinning).

    That inverted the whole point of grading by fitness, and it failed OPEN: the weakest-provenance
    provider claimed the strongest guarantee, with no operator action and no log line.

    A human who genuinely captured a full-resolution, state-pinned render can still say so with
    `--manual-validation-grade`. The difference is that the claim is now explicit, attributable and
    logged, instead of a default nobody chose.
    """
    if getattr(args, "manual_validation_grade", False):
        log.warning(
            "--manual-validation-grade: recording user-supplied screenshots as %s on YOUR assertion. "
            "Nothing verified resolution, state pinning or source revision.",
            CAP_VALIDATION,
        )
        return [CAP_LAYOUT, CAP_TEXT, CAP_VALIDATION]
    log.info(
        "user-supplied screenshots recorded as layout+text only (un-provenanced); pass "
        "--manual-validation-grade if you captured them full-resolution with filters pinned"
    )
    return [CAP_LAYOUT, CAP_TEXT]


def _run_providers(
    args: argparse.Namespace, reference_dir: Path, workbook: Path | None, dashboards: list[str]
) -> list[dict]:
    """Try providers in fitness order (public -> thumbnail -> manual); return records (may be empty)."""
    records: list[dict] = []
    if args.public_url and args.view:
        out = reference_dir / _slug_for(dashboards[0]) / "default.png"
        state_rec = capture_public_playwright(args.public_url, args.view, out)
        if state_rec:
            records.append(_dashboard_record(dashboards[0], out, reference_dir, state_rec))
    if not records and workbook:
        for rec in extract_embedded_thumbnail(workbook, reference_dir / "_thumbnails") or []:
            records.append(_dashboard_record(rec["name"], rec["image"], reference_dir, rec))
        if records:
            log.warning("using embedded thumbnails - LAYOUT HINT ONLY, not validation-grade")
    if not records:
        for img in collect_manual(reference_dir):
            rec = {"provider": "manual", "capabilities": _manual_capabilities(args)}
            records.append(_dashboard_record(img.stem, img, reference_dir, rec))
        if records:
            log.info("using %d user-supplied reference screenshot(s)", len(records))
    return records


def _capture_requested_server(slug_dir: Path) -> tuple[int, list[dict] | None]:
    """Run an explicit Server capture request; return an exit code plus records on success."""
    try:
        server_records = capture_server_rest(slug_dir)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log.error("Server capture requested (--server-rest) but: %s", exc)
        return 3, None
    if not server_records:
        log.error("Server capture requested (--server-rest) but no records were returned")
        return 3, None
    return 0, server_records


def resolve_and_capture(args: argparse.Namespace) -> int:
    """Run the fitness-ordered providers, write the manifest, and fail closed if nothing is produced."""
    slug_dir = Path(args.slug_dir).resolve()
    if not slug_dir.is_dir():
        log.error("not a directory: %s", slug_dir)
        return 2
    reference_dir = slug_dir / "reference"
    workbook = _source_workbook(slug_dir)
    workbook_sha = _sha256(workbook) if workbook else None
    dashboards = _dashboard_names(slug_dir) or ["dashboard"]

    # A requested-but-unavailable Server must HALT, not silently fall through to a lower-fidelity
    # source. Merely inheriting credentials from an unrelated `.env` is not a capture request.
    if args.server_rest and not args.structural_only:
        status, server_records = _capture_requested_server(slug_dir)
        if status:
            return status
        manifest = _write_manifest(reference_dir, workbook_sha, server_records)
        log.info("wrote %s (%d dashboard state(s))", manifest, len(server_records))
        return 0

    records: list[dict] = _run_providers(args, reference_dir, workbook, dashboards)

    if records:
        manifest = _write_manifest(reference_dir, workbook_sha, records)
        log.info("wrote %s (%d dashboard state(s))", manifest, len(records))
        return 0

    # Nothing produced a reference. FAIL CLOSED unless explicitly told to proceed structure-only.
    if args.structural_only:
        reference_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest(reference_dir, workbook_sha, [])
        log.warning(
            "STRUCTURAL-ONLY: no reference image. Visual fidelity cannot be claimed or "
            "signed off; downstream validator must be told gestalt grading is impossible."
        )
        return 0

    log.error(
        "No reference image could be produced and --structural-only was not set. Provide a source:\n"
        "  * Tableau Public: --public-url <workbookRepoUrl> --view <viewName>\n"
        "  * Tableau Server/Cloud: use capture_tableau_oracle.py --out <dir> --images; --server-rest "
        "records an explicit request but this provider is a fail-closed stub today\n"
        "  * Manual: drop tableau-<name>.png screenshots into %s\n"
        "Refusing to build a report blind. See docs/reference-capture.md.",
        reference_dir,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Acquire a provenance-stamped Tableau reference image.")
    parser.add_argument("slug_dir", help="path to <tree>/<slug> (e.g. migrations/workbooks/my-dash)")
    parser.add_argument("--public-url", help="Tableau Public workbookRepoUrl (demo provider)")
    parser.add_argument(
        "--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials (default .env)"
    )
    parser.add_argument(
        "--server-rest",
        action="store_true",
        help="request Server/Cloud REST capture (currently halts because the provider is not wired)",
    )
    parser.add_argument("--view", help="Tableau Public view name (with --public-url)")
    parser.add_argument(
        "--structural-only", action="store_true", help="proceed without a reference (cannot claim visual fidelity)"
    )
    parser.add_argument("--force", action="store_true", help="re-capture even if a manifest exists")
    parser.add_argument(
        "--manual-validation-grade",
        action="store_true",
        help=(
            "record user-supplied screenshots in reference/ as validation_grade. Off by default: a "
            "dropped file is un-provenanced, so it cannot claim the tier the validator signs off on. "
            "Pass this only if YOU captured them full-resolution with filters pinned."
        ),
    )
    args = parser.parse_args(argv)

    manifest = Path(args.slug_dir) / "reference" / "manifest.json"
    if manifest.is_file() and not args.force and not (args.server_rest and not args.structural_only):
        log.info("%s already exists - use --force to re-capture", manifest)
        return 0
    return resolve_and_capture(args)


if __name__ == "__main__":
    sys.exit(main())
