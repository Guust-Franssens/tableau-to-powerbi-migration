"""
purpose: stamp a migration input with where it came from, so a finding filed weeks later is still
         reproducible - and so a reader can tell whether their copy is the same build as ours.
usage:   python scripts/stamp_tableau_provenance.py --input <folder-or-.twbx> [--env .env] [--out PATH]

Why this exists
---------------
The deterministic engine already records the LOCAL half in ``input_manifest.json`` - name, size,
sha256, mtime, staged path, ``source_kind: "LocalFilesSource"``. What no artifact records is the
UPSTREAM half: which Tableau site the file came from, which workbook LUID, which project, who owns
it, when it was last published, and which Tableau build produced it.

That gap is not theoretical. Filing three defects against Tableau's **Superstore** sample required
reconstructing all of it by hand, and it mattered: Tableau's samples differ between releases and
between the Desktop-bundled copy and the Cloud *Samples* project copy, so "we tested on Superstore"
is not a reproducible statement. Figures cited in a defect report - a row count, a column total - do
not reproduce against a different build, and the reader cannot tell that is what happened.

What it emits
-------------
Two independent layers, so the file is useful even with no Tableau access at all:

* **fingerprint** (always) - size, sha256, and for a ``.twbx`` the inner zip entries with their CRCs.
  The entry CRCs are the useful part for a third party: they can compare their own copy member by
  member without either side redistributing a vendor's sample workbook, which matters when the other
  repo is a clean room that deliberately commits no third-party content.
* **origin** (when credentials are supplied and the workbook is found on the site) - server, site,
  workbook LUID, project, owner, ``updatedAt``, plus the Tableau product and REST API versions.

Matching is by **workbook name**, confirmed by re-downloading and comparing the sha256, because a
name alone is not identity - a point this toolchain has now been bitten by four separate times. When
the hash does not match, that is recorded as ``origin.match: "name_only"`` rather than silently
claimed as the source: a same-named workbook that is a different build is exactly the situation this
file exists to make visible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

LOG = logging.getLogger("provenance")

WORKBOOK_SUFFIXES = (".twb", ".twbx")


def load_env(path: Path) -> dict[str, str]:
    """Read a git-ignored KEY=VALUE file. Secrets are never echoed into the output."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def fingerprint(path: Path) -> dict[str, Any]:
    """Size + sha256, plus per-member CRCs for a ``.twbx``.

    The members matter more than the outer hash: a ``.twbx`` is a zip, and zip metadata (timestamps,
    compression) can differ between two downloads of the same content, so two identical workbooks can
    hash differently. Member CRCs compare the content itself.
    """
    raw = path.read_bytes()
    record: dict[str, Any] = {
        "file": path.name,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if path.suffix.lower() == ".twbx" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            record["members"] = [
                {"name": info.filename, "size_bytes": info.file_size, "crc32": f"{info.CRC:08x}"}
                for info in sorted(archive.infolist(), key=lambda i: i.filename)
            ]
    return record


class TableauLookup:
    """Minimal read-only REST client, used only to identify a workbook we already hold."""

    def __init__(self, env: dict[str, str]) -> None:
        self.base = env["TABLEAU_SERVER_URL"].rstrip("/")
        self.version = env.get("TABLEAU_REST_API_VERSION", "3.21")
        self.site = env["TABLEAU_SITE"]
        self.product_version = env.get("TABLEAU_PRODUCT_VERSION")
        self._pat = (env["TABLEAU_PAT_NAME"], env["TABLEAU_PAT_SECRET"])
        self.token: str | None = None
        self.site_id: str | None = None

    def _call(self, method: str, path: str, body: dict | None = None, accept: str | None = None):
        request = urllib.request.Request(
            f"{self.base}/api/{self.version}{path}",
            data=json.dumps(body).encode() if body else None,
            method=method,
        )
        if accept:
            request.add_header("Accept", accept)
        if body:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("X-Tableau-Auth", self.token)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def sign_in(self) -> None:
        """Exchange the PAT for a session token."""
        status, payload = self._call(
            "POST",
            "/auth/signin",
            accept="application/json",
            body={
                "credentials": {
                    "personalAccessTokenName": self._pat[0],
                    "personalAccessTokenSecret": self._pat[1],
                    "site": {"contentUrl": self.site},
                }
            },
        )
        if status != 200:
            raise RuntimeError(f"Tableau sign-in failed: HTTP {status}")
        creds = json.loads(payload)["credentials"]
        self.token, self.site_id = creds["token"], creds["site"]["id"]

    def sign_out(self) -> None:
        """Best-effort release of the session."""
        if self.token:
            self._call("POST", "/auth/signout")
            self.token = None

    def workbooks(self) -> list[dict[str, Any]]:
        """Every workbook on the site (first page is enough to identify one by name)."""
        status, payload = self._call("GET", f"/sites/{self.site_id}/workbooks?pageSize=1000", accept="application/json")
        if status != 200:
            raise RuntimeError(f"listing workbooks failed: HTTP {status}")
        return json.loads(payload).get("workbooks", {}).get("workbook", [])

    def content_sha256(self, workbook_id: str) -> str | None:
        """sha256 of the workbook as the server would hand it to us, or ``None`` if it cannot be read."""
        status, payload = self._call(
            "GET", f"/sites/{self.site_id}/workbooks/{workbook_id}/content?includeExtract=True"
        )
        return hashlib.sha256(payload).hexdigest() if status == 200 else None


def find_origin(lookup: TableauLookup, stem: str, local_sha: str) -> dict[str, Any] | None:
    """Identify a local workbook on the site by name, then CONFIRM by content hash.

    Returns ``None`` when no workbook of that name exists. When one does, ``match`` records how
    strongly it was confirmed -- ``"sha256"`` when the bytes agree, ``"name_only"`` when they do not.
    A name-only match is still worth recording: it says "a workbook of this name exists there and it
    is NOT this build", which is precisely the ambiguity that makes a cited figure irreproducible.
    """
    candidates = [wb for wb in lookup.workbooks() if wb.get("name") == stem]
    if not candidates:
        return None
    workbook = candidates[0]
    remote_sha = lookup.content_sha256(workbook["id"])
    return {
        "server": lookup.base,
        "site": lookup.site,
        "workbook_luid": workbook["id"],
        "workbook_name": workbook.get("name"),
        "project": (workbook.get("project") or {}).get("name"),
        "owner_luid": (workbook.get("owner") or {}).get("id"),
        "created_at": workbook.get("createdAt"),
        "updated_at": workbook.get("updatedAt"),
        "tableau_product_version": lookup.product_version,
        "rest_api_version": lookup.version,
        "match": "sha256" if remote_sha == local_sha else "name_only",
        "remote_sha256": remote_sha,
        "same_name_count": len(candidates),
    }


def collect_inputs(target: Path) -> list[Path]:
    """The workbook(s) to stamp: one file, or every workbook in a folder."""
    if target.is_file():
        return [target]
    return sorted(p for p in target.iterdir() if p.suffix.lower() in WORKBOOK_SUFFIXES)


def build(target: Path, env: dict[str, str]) -> dict[str, Any]:
    """Fingerprint every input, and attach its Tableau origin when credentials allow."""
    inputs = collect_inputs(target)
    lookup: TableauLookup | None = None
    if env.get("TABLEAU_SERVER_URL") and env.get("TABLEAU_PAT_NAME"):
        try:
            lookup = TableauLookup(env)
            lookup.sign_in()
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            LOG.warning("no Tableau lookup (%s: %s) - fingerprints only", type(exc).__name__, str(exc)[:120])
            lookup = None

    records = []
    for path in inputs:
        record = {"input": fingerprint(path)}
        if lookup is not None:
            try:
                origin = find_origin(lookup, path.stem, record["input"]["sha256"])
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                origin, record["lookup_error"] = None, f"{type(exc).__name__}: {str(exc)[:150]}"
            record["origin"] = origin
            if origin is None:
                record["origin_note"] = "no workbook of this name on the site - local-only input"
            elif origin["match"] == "name_only":
                record["origin_note"] = (
                    "a workbook of this name exists on the site but is a DIFFERENT build - "
                    "figures measured here will not reproduce against it"
                )
        records.append(record)
    if lookup is not None:
        lookup.sign_out()

    return {
        "schema": "tableau-source-provenance/1",
        "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "input_count": len(records),
        "inputs": records,
    }


def main() -> int:
    """Stamp provenance. Exit 1 if there was nothing to stamp."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help=".twb/.twbx file, or a folder of them")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials file")
    parser.add_argument("--out", type=Path, help="output JSON (default: source-provenance.json beside the input)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = build(args.input, load_env(args.env))
    if not result["input_count"]:
        LOG.error("no .twb/.twbx found under %s", args.input)
        return 1

    out = args.out or ((args.input if args.input.is_dir() else args.input.parent) / "source-provenance.json")
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    for record in result["inputs"]:
        origin = record.get("origin")
        where = f"{origin['site']} / {origin['project']} ({origin['match']})" if origin else "local only"
        LOG.info("  %-34s %s", record["input"]["file"], where)
        if record.get("origin_note"):
            LOG.warning("      %s", record["origin_note"])
    LOG.info("stamped %d input(s) -> %s", result["input_count"], out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
