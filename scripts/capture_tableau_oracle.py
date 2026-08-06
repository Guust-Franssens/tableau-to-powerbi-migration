"""
purpose: capture Tableau's OWN computed values per view (the numeric oracle) plus a durable
         view-identity manifest keyed by view LUID, from a live Tableau Cloud/Server site.
usage:   python scripts/capture_tableau_oracle.py --out _oracle [--workbook "Superstore"] [--images]

Why this exists
---------------
Two sockets in the migration toolchain are empty, and only a live site can fill them:

1. **Tableau's own numbers.** The deterministic engine's ``fidelity_oracle.py`` has a value tier, but
   it reads *Power BI* values from a local Analysis Services instance -- there is no Tableau-side
   number anywhere in the pipeline. ``/views/{id}/data`` returns the aggregated, as-displayed values
   Tableau itself computed, which is strictly better evidence than re-deriving them ourselves: it is
   immune to the self-consistency trap where a shared assumption hides in both sides of a comparison.

2. **A durable view identity.** ``migrate_estate.py`` keys by workbook name and carries
   ``workbook_luid`` (its ``by_workbook_luid`` index maps to the emitted report folder), but nothing
   persists a **view** LUID -- reference images land as ``<worksheet name>.png``. View LUID is exactly
   the join key ``/views/{id}/data`` needs, so it has to survive capture or the oracle cannot bind a
   Tableau number back to the visual it came from.

Capture is deliberately **raw**: values are stored exactly as Tableau returned them. ``/data`` yields
*display-formatted* text (``"19.5%"``, ``"$12"``), not raw floats, and includes Tableau-generated
fields (``Latitude (generated)``) that have no counterpart in a migrated model. Normalising here would
bake a comparison decision into the evidence; instead the manifest records advisory format hints and
leaves normalisation to whoever compares.

Tableau Cloud session behaviour (measured, see repo memory + upstream issue #97)
-------------------------------------------------------------------------------
A single REST session starts returning ``401002 Unauthorized Access`` on view-export endpoints after
an unpredictable number of exports (observed 1, 2, 3 and 6; no consistent count or elapsed time).
Once it starts, even metadata calls on that token fail. Re-authenticating per export succeeded 8/8.
This script therefore re-authenticates on ``401002`` and **records every re-auth in the manifest** --
silent recovery is how a truncated capture looks complete.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOG = logging.getLogger("tableau-oracle")

REST_TIMEOUT_SEC = 180
SESSION_LOST_CODE = "401002"
MAX_REAUTH_PER_VIEW = 2

_PERCENT = re.compile(r"^-?[\d,.]+%$")
_CURRENCY = re.compile(r"^-?[$£€¥]\s?[\d,.]+$")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


def load_env(path: Path) -> dict[str, str]:
    """Read a git-ignored KEY=VALUE file. Secrets are never logged."""
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


class ExportFailed(RuntimeError):
    """A view export did not return data. ``kind`` classifies what a caller should do about it."""

    def __init__(self, message: str, kind: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def classify_export_error(status: int, text: str) -> tuple[str, str]:
    """Map a Tableau error body to an actionable class.

    The distinction is the whole point. ``401002`` is our session dying and is worth a retry.
    A ``FederatedDataSourceException`` naming an expired OAuth token or a connection that "needs
    attention" is Tableau itself being unable to query the underlying source -- **a missing credential
    is not transient**, so retrying it burns time and still cannot succeed. Only a human can fix it,
    which is exactly the case the repo's credential rule exists for.
    """
    if SESSION_LOST_CODE in text:
        return "session_lost", ""
    credential_markers = (
        "FederatedDataSourceException",
        "OAuth refresh token",
        "need attention",
        "needs attention",
        "Invalid username or password",
        "authentication",
    )
    if any(marker.lower() in text.lower() for marker in credential_markers):
        match = re.search(r"([\w.-]+\.(?:com|net|io|azuredatabricks\.net)[^:\s]*):\s*(Tableau[^<\n]{0,180})", text)
        detail = f"{match.group(1)}: {match.group(2).strip()}" if match else text[:200]
        return "source_credential", detail.split("tableau_error_source=")[0].strip()
    return "failed", f"HTTP {status}: {text[:200]}"


class TableauSession:
    """Minimal stdlib Tableau REST client that survives mid-loop session loss."""

    def __init__(self, base: str, site: str, pat_name: str, pat_secret: str, version: str) -> None:
        self._base = base.rstrip("/")
        self._site = site
        self._pat = (pat_name, pat_secret)
        self.version = version
        self.token: str | None = None
        self.site_id: str | None = None
        self.reauth_count = 0

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        accept: str | None = None,
        authed: bool = True,
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(
            f"{self._base}/api/{self.version}{path}",
            data=json.dumps(body).encode() if body else None,
            method=method,
        )
        if accept:
            req.add_header("Accept", accept)
        if body:
            req.add_header("Content-Type", "application/json")
        if authed and self.token:
            req.add_header("X-Tableau-Auth", self.token)
        try:
            with urllib.request.urlopen(req, timeout=REST_TIMEOUT_SEC) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def sign_in(self) -> None:
        """Exchange the PAT for a session token. The secret is never logged or returned."""
        status, payload = self._request(
            "POST",
            "/auth/signin",
            accept="application/json",
            authed=False,
            body={
                "credentials": {
                    "personalAccessTokenName": self._pat[0],
                    "personalAccessTokenSecret": self._pat[1],
                    "site": {"contentUrl": self._site},
                }
            },
        )
        if status != 200:
            raise RuntimeError(f"Tableau sign-in failed: HTTP {status}. Check PAT name AND secret (two values).")
        creds = json.loads(payload)["credentials"]
        self.token, self.site_id = creds["token"], creds["site"]["id"]

    def sign_out(self) -> None:
        """Release the session. Best-effort; a failed sign-out is not worth aborting a capture."""
        if self.token:
            self._request("POST", "/auth/signout")
            self.token = None

    def get_json(self, path: str) -> dict[str, Any]:
        """GET a metadata endpoint as JSON, raising on any non-200."""
        status, payload = self._request("GET", path, accept="application/json")
        if status != 200:
            raise RuntimeError(f"GET {path} -> HTTP {status}")
        return json.loads(payload)

    def export(self, path: str) -> tuple[bytes, float, int]:
        """GET a content-export endpoint, re-authenticating on session loss.

        Returns ``(body, elapsed_sec, reauths_used)``. Raises on a non-401002 failure so a genuinely
        broken view is never silently recorded as empty.
        """
        reauths = 0
        while True:
            started = time.perf_counter()
            status, payload = self._request("GET", path)
            elapsed = time.perf_counter() - started
            if status == 200:
                return payload, elapsed, reauths
            text = payload.decode("utf-8", "replace")
            kind, detail = classify_export_error(status, text)
            if kind == "session_lost" and reauths < MAX_REAUTH_PER_VIEW:
                reauths += 1
                self.reauth_count += 1
                LOG.debug("session lost (401002); re-authenticating (%d)", self.reauth_count)
                self.sign_in()
                continue
            raise ExportFailed(f"GET {path} -> HTTP {status}", kind, detail or text[:200])


def list_views(session: TableauSession) -> list[dict[str, Any]]:
    """Every view on the site, with the identity fields the oracle needs to bind results back."""
    payload = session.get_json(f"/sites/{session.site_id}/views?pageSize=1000")
    return payload.get("views", {}).get("view", [])


def detect_format(values: list[str]) -> str | None:
    """Advisory hint: does this column arrive display-formatted rather than as a raw number?"""
    sample = [v for v in values if v][:50]
    if not sample:
        return None
    if all(_PERCENT.match(v) for v in sample):
        return "percent"
    if all(_CURRENCY.match(v) for v in sample):
        return "currency"
    if all(_THOUSANDS.match(v) for v in sample):
        return "thousands_separated"
    return None


def summarise_csv(payload: bytes) -> dict[str, Any]:
    """Row/column shape plus per-column format hints, so a capture can be proven non-empty."""
    text = payload.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {"row_count": 0, "columns": [], "format_hints": {}}
    header, body = rows[0], rows[1:]
    hints = {}
    for idx, name in enumerate(header):
        fmt = detect_format([r[idx] for r in body if idx < len(r)])
        if fmt:
            hints[name] = fmt
    return {"row_count": len(body), "columns": header, "format_hints": hints}


def safe_slug(text: str) -> str:
    """Filesystem-safe stem. Lossy by design, which is why the LUID is appended by the caller."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:60] or "view"


def capture_view(session: TableauSession, view: dict[str, Any], out_dir: Path, want_images: bool) -> dict[str, Any]:
    """Capture one view's data (and optionally its rendered image), keyed by view LUID."""
    view_luid = view["id"]
    workbook = view.get("workbook", {}) or {}
    stem = f"{safe_slug(view.get('name', ''))}__{view_luid[:8]}"
    record: dict[str, Any] = {
        "view_luid": view_luid,
        "view_name": view.get("name"),
        "view_url_name": view.get("viewUrlName"),
        "content_url": view.get("contentUrl"),
        "workbook_luid": workbook.get("id"),
        "project": (view.get("project") or {}).get("name"),
        "updated_at": view.get("updatedAt"),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    data_path = out_dir / "data" / f"{stem}.csv"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload, elapsed, reauths = session.export(f"/sites/{session.site_id}/views/{view_luid}/data")
    except ExportFailed as exc:
        record["data"] = {"status": exc.kind, "error": str(exc), "detail": exc.detail}
        return record
    data_path.write_bytes(payload)
    record["data"] = {
        "status": "ok",
        "path": str(data_path.relative_to(out_dir)).replace("\\", "/"),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "elapsed_sec": round(elapsed, 2),
        "reauths": reauths,
        **summarise_csv(payload),
    }

    if want_images:
        image_path = out_dir / "images" / f"{stem}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            png, elapsed, reauths = session.export(f"/sites/{session.site_id}/views/{view_luid}/image?resolution=high")
        except ExportFailed as exc:
            record["image"] = {"status": exc.kind, "error": str(exc), "detail": exc.detail}
            return record
        image_path.write_bytes(png)
        record["image"] = {
            "status": "ok",
            "path": str(image_path.relative_to(out_dir)).replace("\\", "/"),
            "bytes": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "elapsed_sec": round(elapsed, 2),
            "reauths": reauths,
        }
    return record


def build_parser() -> argparse.ArgumentParser:
    """CLI surface."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="output directory (should be git-ignored)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials file")
    parser.add_argument("--workbook", action="append", default=None, help="workbook name filter (repeatable)")
    parser.add_argument("--images", action="store_true", help="also capture /image?resolution=high per view")
    parser.add_argument("--limit", type=int, default=0, help="stop after N views (0 = all)")
    return parser


def select_views(
    session: TableauSession, workbooks: list[str] | None, limit: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Resolve the views to capture, plus a workbook-LUID -> name index for the manifest."""
    views = list_views(session)
    payload = session.get_json(f"/sites/{session.site_id}/workbooks?pageSize=1000")
    names = {wb["id"]: wb["name"] for wb in payload.get("workbooks", {}).get("workbook", [])}
    if workbooks:
        wanted = {w.lower() for w in workbooks}
        views = [v for v in views if names.get((v.get("workbook") or {}).get("id"), "").lower() in wanted]
    if limit:
        views = views[:limit]
    return views, names


def log_progress(index: int, total: int, record: dict[str, Any]) -> None:
    """One line per view: proof of rows captured, or a loud, classified failure."""
    data = record.get("data", {})
    name = (record.get("view_name") or "")[:34]
    status = data.get("status")
    if status == "ok":
        suffix = f"  (re-auth x{data['reauths']})" if data["reauths"] else ""
        LOG.info(
            "  %2d/%d  %-34s %5d rows  %6.1fs%s", index, total, name, data["row_count"], data["elapsed_sec"], suffix
        )
    elif status == "source_credential":
        LOG.warning("  %2d/%d  %-34s NEEDS CREDENTIAL: %s", index, total, name, data.get("detail"))
    else:
        LOG.warning("  %2d/%d  %-34s FAILED (%s): %s", index, total, name, status, data.get("detail"))


def write_manifest(
    records: list[dict[str, Any]], session: TableauSession, env: dict[str, str], out_dir: Path, started: float
) -> int:
    """Write the manifest and return the process exit code (0 ok / 1 failure / 2 credential-blocked)."""
    ok = [r for r in records if r.get("data", {}).get("status") == "ok"]
    empty = [r for r in ok if r["data"]["row_count"] == 0]
    blocked = [r for r in records if r.get("data", {}).get("status") == "source_credential"]
    failed = [r for r in records if r.get("data", {}).get("status") not in {"ok", "source_credential"}]
    manifest = {
        "schema": "tableau-oracle/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": env["TABLEAU_SERVER_URL"],
        "site": env["TABLEAU_SITE"],
        "rest_api_version": env.get("TABLEAU_REST_API_VERSION"),
        "view_count": len(records),
        "data_ok": len(ok),
        "data_empty": len(empty),
        "credential_blocked": len(blocked),
        "failed": len(failed),
        "total_reauths": session.reauth_count,
        "elapsed_sec": round(time.perf_counter() - started, 1),
        "views": records,
    }
    manifest_path = out_dir / "oracle-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    LOG.info(
        "\n%d/%d captured (%d empty), %d credential-blocked, %d failed, %d re-auth(s), %.0fs -> %s",
        len(ok),
        len(records),
        len(empty),
        len(blocked),
        len(failed),
        session.reauth_count,
        manifest["elapsed_sec"],
        manifest_path,
    )
    if blocked:
        LOG.warning(
            "\n%d view(s) need a credential ON THE TABLEAU SIDE - no retry can fix this, a human must "
            "reauthorize the source in Tableau:",
            len(blocked),
        )
        for record in blocked:
            LOG.warning("  - %s (%s): %s", record["view_name"], record["workbook_name"], record["data"]["detail"])
    if failed:
        return 1
    return 2 if blocked else 0


def main() -> int:
    """Capture the oracle for every selected view.

    Exit codes: ``0`` all captured, ``1`` a hard failure, ``2`` some view needs a credential on the
    Tableau side (actionable only by a human -- never by a retry).
    """
    args = build_parser().parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = load_env(args.env)
    session = TableauSession(
        base=env["TABLEAU_SERVER_URL"],
        site=env["TABLEAU_SITE"],
        pat_name=env["TABLEAU_PAT_NAME"],
        pat_secret=env["TABLEAU_PAT_SECRET"],
        version=env.get("TABLEAU_REST_API_VERSION", "3.21"),
    )
    session.sign_in()
    LOG.info("signed in to site %r (api %s)", env["TABLEAU_SITE"], session.version)

    views, workbook_names = select_views(session, args.workbook, args.limit)
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("capturing %d view(s) -> %s", len(views), out_dir)

    records, started = [], time.perf_counter()
    for index, view in enumerate(views, 1):
        record = capture_view(session, view, out_dir, args.images)
        record["workbook_name"] = workbook_names.get(record["workbook_luid"])
        records.append(record)
        log_progress(index, len(views), record)

    exit_code = write_manifest(records, session, env, out_dir, started)
    session.sign_out()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
