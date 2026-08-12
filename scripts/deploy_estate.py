"""
purpose: deploy a migrated estate into a Fabric LANDING ZONE workspace - models first, reports
         rebound to them - and survive a crash without redeploying or silently skipping anything.
usage:   python scripts/deploy_estate.py --bundle <dir> --workspace <id> [--dry-run]
         python scripts/deploy_estate.py --bundle <dir> --workspace <id> --tenant <id>

Why a landing zone
------------------
It separates two questions that are otherwise tangled: *does it work?* (verified in a throwaway
workspace where nobody cares who can see what) and *who may see it?* (decided once, in the real
workspace, against content already known to be correct). Getting IAM right BEFORE the content is
correct means redoing it after every fix round.

The three facts this script exists to encode
--------------------------------------------
All three were measured against a real Fabric tenant, and each one silently produces a broken or
absent deployment if you get it wrong.

1. **A migrated PBIP binds its report to its model BY PATH, and the service cannot resolve a path.**
   `definition.pbir` arrives as ``{"byPath": {"path": "../X.SemanticModel"}}``, which is a Git
   integration mechanism. Deploying it unchanged fails. The report must be rebound to
   ``byConnection`` against the model's object id *after* the model exists - which is what forces
   the model-first ordering below.

2. **The `byConnection` shape that most sources give you is the WRONG ONE.** The widely-quoted form
   carries five fields (`pbiServiceModelId`, `pbiModelVirtualServerName`, `pbiModelDatabaseName`,
   `connectionType`, `name`). That is PBIR schema **1.0.0**. Schema **2.0.0** - what the engine
   emits - declares ``additionalProperties: false`` and allows exactly one property,
   ``connectionString``; sending the five-field form is rejected with `Workload_FailedToParseFile`
   naming each extra property. The model's identity then has nowhere to go except *inside* the
   connection string, as ``semanticModelId=<guid>``. Omit it and the service answers
   `InvalidConnectionInformation`.

3. **`202 Accepted` tells you nothing.** Create returns an empty body, and a FAILED operation is
   indistinguishable from a successful one unless you poll ``/operations/{id}`` and read `status`.
   Measured: a first probe reported the model deployed and the report fine; the report had in fact
   failed, and the workspace held one item.

Crash safety
------------
A run journal records **intent before the mutation and outcome after**, with a hash of the exact
definition deployed. Intent-first is what closes the window between "we called create" and "we
recorded that we called create" - an outcome-only log cannot tell a crash there from never having
called. The hash is what closes the more dangerous case: an item that exists but whose upload was
partial. A name-only "does it exist?" check answers *present* and a resume skips it, silently
shipping a broken item. Resume therefore skips an item only when the journal says done AND the hash
matches what is about to be deployed.

There is no idempotency key on the Fabric item APIs, so this is entirely the client's job. A
duplicate `displayName` + type is rejected (`ItemDisplayNameNotAvailableYet`), which is a safety net
rather than a solution - and its wording is the language of eventual consistency, so absence from a
listing is not proof of absence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error, request

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

LOG = logging.getLogger("deploy_estate")

API = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"

# We create only these two, and both are POWER BI items rather than Fabric items - so the landing
# zone does NOT need an F capacity, only an appropriately licensed identity. Worth knowing before
# asking a customer to provision one.
MODEL_TYPE = "SemanticModel"
REPORT_TYPE = "Report"

# Folders nest up to 10 levels in Fabric; beyond that the API answers `FolderDepthOutOfRange`.
# Tableau does not enforce that limit, so a deep estate is a matter of time rather than a
# hypothetical - the overflow is flattened into a compound name and reported, never dropped.
MAX_FOLDER_DEPTH = 10

# Fabric enforces 1,000 items per workspace (folders do not count). A workbook lands as ~2 items
# plus one per datasource, so this bites at roughly 450-500 workbooks - but a customer-supplied
# landing zone is not necessarily empty, so the budget is 1000 MINUS what is already there.
WORKSPACE_ITEM_LIMIT = 1000
# Do not fill to the brim: a later re-deploy may create before deleting, and the customer will add
# their own content.
HEADROOM = 0.10

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PREFLIGHT = 2


@dataclass
class Item:
    """One deployable item: a folder of definition parts plus where it goes."""

    name: str
    item_type: str
    folder: Path
    parts: list[dict[str, str]] = field(default_factory=list)
    folder_id: str | None = None

    @property
    def digest(self) -> str:
        """Hash of the exact bytes about to be deployed, so a resume can tell 'done' from 'stale'."""
        sha = hashlib.sha256()
        for part in self.parts:
            sha.update(part["path"].encode("utf-8"))
            sha.update(part["payload"].encode("ascii"))
        return sha.hexdigest()


def token(tenant: str | None) -> str:
    """Mint a Fabric-scoped bearer token via the Azure CLI.

    `az` rather than `fab`: the same code path serves an interactive user (`az login`) and a service
    principal (`az login --service-principal`), so an unattended run needs no second mechanism.
    """
    cmd = ["az", "account", "get-access-token", "--resource", FABRIC_RESOURCE]
    if tenant:
        cmd += ["--tenant", tenant]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "Could not get a Fabric token from the Azure CLI. Run `az login`"
            + (f" --tenant {tenant}" if tenant else "")
            + f".\n  {exc.stderr.strip()[:300]}"
        ) from exc
    return json.loads(out.stdout)["accessToken"]


def call(method: str, url: str, tok: str, body: dict | None = None) -> tuple[int, dict, dict]:
    """One REST call. Returns (status, headers, parsed-body); never raises on an HTTP error."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            status = resp.status
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        status = exc.code
    except error.URLError as exc:
        return 0, {}, {"error": {"message": str(exc.reason)}}
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        parsed = {"raw": raw[:400]}
    return status, headers, parsed


def await_operation(op_url: str, tok: str, *, timeout: float = 300.0) -> tuple[str, dict]:
    """Poll a long-running operation to a terminal state. Returns (status, body).

    This is not optional politeness: create returns `202` with an empty body, and a FAILED operation
    looks exactly like a successful one until this is read.
    """
    deadline = time.monotonic() + timeout
    delay = 3.0
    while time.monotonic() < deadline:
        time.sleep(delay)
        status, _, body = call("GET", op_url, tok)
        state = body.get("status") or body.get("Status") or ""
        if state in ("Succeeded", "Failed", "Undetermined"):
            return state, body
        if status == 429:
            delay = min(delay * 2, 30.0)
    return "Timeout", {}


def parts_for(folder: Path) -> list[dict[str, str]]:
    """Every file under an item folder as an InlineBase64 definition part."""
    out: list[dict[str, str]] = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            out.append(
                {
                    "path": path.relative_to(folder).as_posix(),
                    "payload": base64.b64encode(path.read_bytes()).decode("ascii"),
                    "payloadType": "InlineBase64",
                }
            )
    return out


def rebind(parts: list[dict[str, str]], workspace_name: str, model_name: str, model_id: str) -> list[dict[str, str]]:
    """Replace a report's byPath dataset reference with a service byConnection one.

    See the module docstring: schema 2.0.0 allows ONLY `connectionString` here, so the model's guid
    travels inside it as `semanticModelId`. Anything else is rejected before the item is created.
    """
    pbir = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json"
        ),
        "version": "4.0",
        "datasetReference": {
            "byConnection": {
                "connectionString": (
                    f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspace_name};"
                    f"Initial Catalog={model_name};Integrated Security=ClaimsToken;"
                    f"semanticModelId={model_id}"
                )
            }
        },
    }
    payload = base64.b64encode(json.dumps(pbir, indent=2).encode("utf-8")).decode("ascii")
    if not any(part["path"] == "definition.pbir" for part in parts):
        # Silently adding one would invent a binding for a report whose shape we do not understand.
        raise ValueError("report has no definition.pbir - refusing to guess its semantic-model binding")
    return [{**part, "payload": payload} if part["path"] == "definition.pbir" else part for part in parts]


def discover(bundle: Path) -> list[tuple[str, Item, Item | None]]:
    """Find each workbook's (model, report) pair under `<bundle>/pbip/`.

    Returns model-first pairs; a workbook whose PBIP was skipped by the engine simply is not here,
    which is why the caller reports the count against the bundle's own workbook total rather than
    assuming everything present was everything expected.
    """
    pbip = bundle / "pbip"
    if not pbip.is_dir():
        raise SystemExit(f"no pbip/ folder in {bundle} - was this bundle produced by run_estate.py?")
    found: list[tuple[str, Item, Item | None]] = []
    for folder in sorted(p for p in pbip.iterdir() if p.is_dir()):
        models = list(folder.glob("*.SemanticModel"))
        reports = list(folder.glob("*.Report"))
        if not models:
            LOG.warning("%s has no .SemanticModel folder - skipping", folder.name)
            continue
        model = Item(folder.name, MODEL_TYPE, models[0])
        report = Item(folder.name, REPORT_TYPE, reports[0]) if reports else None
        found.append((folder.name, model, report))
    return found


class Journal:
    """Append-only record of intent and outcome, so a crashed run resumes instead of restarting.

    One JSON object per line, written and flushed immediately. Intent is recorded BEFORE the call so
    a crash between the request and its outcome is still visible; the outcome carries the operation
    id, which lets a resume poll the operation that was already started rather than issuing a second
    create against a name the service may already hold.

    **The journal is the only protection against duplicates.** Measured against a real tenant:
    Fabric does NOT reject a second `Report` or `SemanticModel` with the same `displayName` and type
    -- two identical pairs sat side by side in the workspace afterwards. The documented
    `ItemDisplayNameNotAvailableYet` rejection is therefore not a safety net we can lean on for these
    types, which makes both the journal and the lock below load-bearing rather than conveniences.
    """

    def __init__(self, path: Path, workspace: str = "") -> None:
        self.path = path
        self.workspace = workspace
        self.done: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.pending: dict[tuple[str, str, str], dict[str, Any]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn final line is expected after a hard kill
                # Keyed by WORKSPACE too. Without it, deploying the same bundle to a second
                # workspace - which is exactly what promotion from a landing zone to a secured
                # workspace is - would find every item "already deployed" and create nothing, while
                # reporting success.
                key = (row.get("workspace", ""), row.get("item", ""), row.get("type", ""))
                if row.get("phase") == "outcome" and row.get("status") == "Succeeded":
                    self.done[key] = row
                    self.pending.pop(key, None)
                elif row.get("phase") == "intent":
                    self.pending[key] = row

    def _key(self, item: Item) -> tuple[str, str, str]:
        return (self.workspace, item.name, item.item_type)

    def _write(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
            handle.flush()

    def intent(self, item: Item, action: str) -> None:
        """Record what we are about to do, before doing it."""
        self._write(
            {
                "phase": "intent",
                "workspace": self.workspace,
                "item": item.name,
                "type": item.item_type,
                "action": action,
                "definition_sha256": item.digest,
                "folderId": item.folder_id,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    def outcome(self, item: Item, status: str, item_id: str | None, operation: str | None, detail: str = "") -> None:
        """Record how it went, including the operation id a resume may need to poll."""
        self._write(
            {
                "phase": "outcome",
                "workspace": self.workspace,
                "item": item.name,
                "type": item.item_type,
                "status": status,
                "itemId": item_id,
                "operationId": operation,
                "definition_sha256": item.digest,
                "folderId": item.folder_id,
                "detail": detail[:400],
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    def already_deployed(self, item: Item) -> dict[str, Any] | None:
        """Return the recorded success ONLY if the definition is byte-identical to what we now hold.

        The hash is the point. 'An item with this name exists' is the check that silently ships a
        half-uploaded item; 'the content I intend is there' is the one that does not. The folder is
        compared too, so moving a project in Tableau re-places its items rather than leaving them
        where last year's tree put them.
        """
        row = self.done.get(self._key(item))
        if row and row.get("definition_sha256") == item.digest and row.get("folderId") == item.folder_id:
            return row
        return None

    def unfinished(self, item: Item) -> dict[str, Any] | None:
        """An intent with no matching success - the crash-in-flight case worth polling, not retrying."""
        return self.pending.get(self._key(item))


class RunLock:
    """A lock beside the journal, so two deploys cannot run against the same bundle at once.

    Not hypothetical: measured. A first deploy was believed finished (its shell had returned, but the
    Python process was still running), a second was started, and each read a journal that did not yet
    contain the other's outcomes -- so both created the same items and the workspace ended with
    duplicate models and reports. Since Fabric does not reject a duplicate name for these types, the
    journal alone cannot prevent that; the runs have to be prevented from overlapping.

    Deliberately advisory and simple: a stale lock from a hard kill is reported with its age and can
    be overridden, because a lock that cannot be cleared is worse than none in a live session.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def acquire(self, *, force: bool = False) -> tuple[bool, str]:
        """Take the lock atomically. Returns (ok, message).

        `O_CREAT | O_EXCL` rather than exists()-then-write: the check-then-act version has a window
        two simultaneous starts can both pass, which is exactly the failure this lock exists to
        prevent.
        """
        if force:
            self.path.unlink(missing_ok=True)
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                held = {}
            age = (time.time() - self.path.stat().st_mtime) / 60 if self.path.exists() else 0
            return False, (
                f"another deploy holds {self.path.name} (pid {held.get('pid', '?')}, "
                f"started {held.get('at', '?')}, {age:.0f} min ago). Wait for it, or re-run with "
                "--force-unlock if you are certain it is dead. Two concurrent runs create DUPLICATE "
                "items: Fabric does not reject a repeated report/model name."
            )
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, stream)
        return True, "lock acquired"

    def release(self) -> None:
        """Best-effort release; a leftover lock is recoverable, a crash mid-deploy is not."""
        self.path.unlink(missing_ok=True)


def preflight(workspace: str, tok: str, planned: int) -> tuple[bool, str, dict[str, Any]]:
    """Check everything cheap BEFORE the first write. Returns (ok, message, workspace-info).

    Each failure here is one a run would otherwise hit partway through, leaving a half-deployed
    estate. Note `not found` and `no access` are reported differently on purpose: conflating them
    costs an afternoon of looking for the wrong problem.
    """
    status, _, body = call("GET", f"{API}/workspaces/{workspace}", tok)
    if status == 404:
        return False, f"workspace {workspace} does not exist (or this identity cannot see it)", {}
    if status == 403:
        return False, f"no access to workspace {workspace} - this identity needs the Contributor role", {}
    if status != 200:
        return False, f"could not read workspace {workspace}: HTTP {status} {json.dumps(body)[:200]}", {}

    name = body.get("displayName", "?")
    status, _, items = call("GET", f"{API}/workspaces/{workspace}/items", tok)
    if status != 200:
        return False, f"could not list items in {name!r}: HTTP {status} - is this identity a Contributor?", body
    existing = len(items.get("value", []))
    budget = int(WORKSPACE_ITEM_LIMIT * (1 - HEADROOM)) - existing
    if planned > budget:
        return (
            False,
            f"{planned} item(s) planned but only {budget} fit in {name!r} "
            f"({existing} already there, {WORKSPACE_ITEM_LIMIT}-item limit, {int(HEADROOM * 100)}% headroom kept). "
            "Supply a second workspace or narrow the scope.",
            body,
        )
    return True, f"{name!r}: {existing} existing item(s), {planned} planned, {budget - planned} spare", body


def _post_item(workspace: str, tok: str, item: Item) -> tuple[int, dict, dict]:
    """POST one item definition, honouring a 429 `Retry-After` once.

    One retry, not a loop: 429 carries the service's own instruction, so waiting exactly that long
    and trying once is a bounded response. Anything persistent is a real problem to report, not to
    grind against.
    """
    body = {"displayName": item.name, "type": item.item_type, "definition": {"parts": item.parts}}
    if item.folder_id:
        # Placed AT CREATION - `folderId` is a field on Create Item, so there is no create-then-move
        # dance and no window where the item sits in the wrong place.
        body["folderId"] = item.folder_id
    status, headers, resp = call("POST", f"{API}/workspaces/{workspace}/items", tok, body)
    if status == 429:
        wait = float(headers.get("retry-after", "20"))
        LOG.warning("rate limited creating %s; waiting %.0fs as instructed", item.name, wait)
        time.sleep(wait)
        status, headers, resp = call("POST", f"{API}/workspaces/{workspace}/items", tok, body)
    return status, headers, resp


def create_item(workspace: str, tok: str, item: Item, journal: Journal) -> tuple[str, str | None, str]:
    """Create one item and wait for its operation. Returns (status, item_id, detail).

    Records intent first, then the outcome including the operation id. `ItemDisplayNameNotAvailableYet`
    is treated as "already there, go verify" rather than as fatal: the wording is the language of
    eventual consistency, and on a resume it is the expected answer, not an error.
    """
    journal.intent(item, "create")
    status, headers, resp = _post_item(workspace, tok, item)
    operation = headers.get("x-ms-operation-id")

    if status == 201:
        item_id = resp.get("id")
        journal.outcome(item, "Succeeded", item_id, operation)
        return "Succeeded", item_id, ""

    if status == 202:
        location = headers.get("location") or (f"{API}/operations/{operation}" if operation else "")
        if not location:
            journal.outcome(item, "Failed", None, operation, "202 with no operation to poll")
            return "Failed", None, "202 with no operation to poll"
        state, op_body = await_operation(location, tok)
        if state == "Succeeded":
            _, _, result = call("GET", f"{location}/result", tok)
            item_id = result.get("id")
            journal.outcome(item, "Succeeded", item_id, operation)
            return "Succeeded", item_id, ""
        detail = json.dumps(op_body.get("error", op_body))[:400]
        journal.outcome(item, state, None, operation, detail)
        return state, None, detail

    error_code = (resp.get("errorCode") or resp.get("error", {}).get("errorCode") or "").strip()
    detail = f"HTTP {status} {error_code} {json.dumps(resp)[:300]}"
    if error_code in ("ItemDisplayNameNotAvailableYet", "ItemDisplayNameAlreadyInUse"):
        journal.outcome(item, "AlreadyExists", None, operation, detail)
        return "AlreadyExists", None, detail
    journal.outcome(item, "Failed", None, operation, detail)
    return "Failed", None, detail


def find_existing(workspace: str, tok: str, name: str, item_type: str) -> str | None:
    """Look up an item's id by name+type, for reconciling a journal that fell behind reality."""
    status, _, body = call("GET", f"{API}/workspaces/{workspace}/items", tok)
    if status != 200:
        return None
    for entry in body.get("value", []):
        if entry.get("displayName") == name and entry.get("type") == item_type:
            return entry.get("id")
    return None


# Fabric folder names are far more restrictive than Tableau project names, and the API answers
# `InvalidFolderDisplayName` rather than silently coercing. Measured against a live workspace:
#
#   rejected: &  /  \  :  ?  *  "  |  <  #  %  .     and a leading or trailing space
#   accepted: -  _  +  (  )  spaces in the middle, and non-ASCII (`Ventes françaises` was fine)
#
# The dot is the one that will actually bite: it is rejected ANYWHERE, not merely at the end, and
# Tableau project names carry dots routinely (`v1.2`, `Q1.2026`).
#
# This is an ALLOW-list rather than a deny-list of the characters measured above, deliberately: an
# untested character silently replaced is a cosmetic surprise, whereas an untested character
# rejected by the API is a failed deploy in front of the customer. Fail safe, not fail clever.
_FOLDER_SAFE_EXTRA = frozenset(" -_+()")


def folder_display_name(name: str) -> str:
    """Coerce a Tableau project name into something Fabric will accept as a folder name."""
    cleaned = "".join(char if (char.isalnum() or char in _FOLDER_SAFE_EXTRA) else "-" for char in name)
    # Collapse runs introduced by substitution, then strip the leading/trailing spaces Fabric rejects.
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip(" -") or "folder"


def unique_siblings(names: list[str]) -> dict[str, str]:
    """Map original -> final folder name, guaranteeing uniqueness AMONG SIBLINGS.

    `R&D`, `R/D` and `R.D` all sanitise to `R-D`. Letting them collide would silently pool three
    Tableau projects' content into one folder, and the customer would have no way to tell. A numeric
    suffix is ugly and honest; a silent merge is neither.
    """
    used: dict[str, str] = {}
    taken: set[str] = set()
    for name in names:
        base = folder_display_name(name)
        candidate, counter = base, 2
        while candidate.casefold() in taken:
            candidate = f"{base} ({counter})"
            counter += 1
        taken.add(candidate.casefold())
        used[name] = candidate
    return used


def project_parents(estate_db: Path | None) -> dict[str, str]:
    """Map project name -> parent project name, so Tableau's nesting survives the migration.

    Without this every project becomes a root folder: correct, but flatter than the customer's own
    structure, which is the whole reason for mirroring it. `estate.db` records `parent_luid`, and it
    was being collected and discarded.

    **Known limitation, deliberately reported rather than silently resolved.** The map is keyed by
    NAME because that is all `workbook.project` gives us to join on. Tableau estates reuse project
    names under different parents (`Reports`, `Archive`, `Test`), and two such projects are
    indistinguishable here - so their content would pool under whichever parent won. Where that is
    detected, BOTH are dropped from the tree and their content lands at the workspace root, which is
    wrong-but-visible rather than wrong-and-hidden. Fixing it properly needs luid identity carried
    through `workbook.project`, which is an assessment-layer change.
    """
    if not (estate_db and estate_db.is_file()):
        return {}
    try:
        with sqlite3.connect(f"file:{estate_db}?mode=ro", uri=True) as conn:
            rows = list(conn.execute("select luid, name, parent_luid from project"))
    except sqlite3.Error as exc:
        LOG.warning("could not read the project tree from %s (%s) - folders will be flat", estate_db, exc)
        return {}

    names = {luid: name for luid, name, _parent in rows}
    duplicated = {name for name in names.values() if list(names.values()).count(name) > 1}
    if duplicated:
        LOG.warning(
            "project name(s) %s appear more than once in the tree; they cannot be told apart from a "
            "workbook's project name, so their content lands at the workspace root rather than "
            "risking a merge under one parent",
            ", ".join(sorted(duplicated)),
        )
    return {
        name: names[parent]
        for _luid, name, parent in rows
        if parent and parent in names and name not in duplicated and names[parent] not in duplicated
    }


def _slug(name: str) -> str:
    """Normalise a workbook name for matching across the two naming conventions.

    Tableau keeps the original (`Meridian Multi-Source (3 systems)`); the engine sanitises it into a
    folder name (`Meridian_Multi-Source__3_systems_`). An exact-match join therefore silently placed
    only 8 of 33 workbooks and sent the rest to the root - which looks like "we had no project data"
    rather than like a bug, so it is worth naming here.
    """
    return "".join(char.lower() if char.isalnum() else "_" for char in name).strip("_")


def project_map(bundle: Path, estate_db: Path | None) -> dict[str, str]:
    """Map workbook SLUG -> its Tableau project, from whichever source can answer.

    Two sources, deliberately in this order:

    * ``estate.db`` from `assess_estate.py` - authoritative and complete, because it read the site.
    * ``source-provenance.json`` in the bundle - present without an assessment, but only covers
      workbooks that were matched back to the site. Measured on a real bundle: 10 of 58 rows carried
      an origin, the rest being local-only inputs.

    A workbook we cannot place goes to the workspace ROOT and is reported as such. Inventing a folder
    for it would be worse: the customer would look for content under a heading Tableau never had.
    """
    mapping: dict[str, str] = {}

    provenance = bundle / "source-provenance.json"
    if provenance.is_file():
        try:
            rows = json.loads(provenance.read_text(encoding="utf-8")).get("inputs", [])
        except (json.JSONDecodeError, OSError):
            rows = []
        for row in rows:
            origin = row.get("origin") or {}
            name = origin.get("workbook_name")
            if name and origin.get("project"):
                mapping[_slug(name)] = origin["project"]

    if estate_db and estate_db.is_file():
        try:
            with sqlite3.connect(f"file:{estate_db}?mode=ro", uri=True) as conn:
                rows = list(conn.execute("select name, project from workbook where project is not null"))
        except sqlite3.Error as exc:
            LOG.warning("could not read %s (%s) - falling back to provenance only", estate_db, exc)
            rows = []
        # Two workbooks whose names differ only by a separator or case slug to the same key
        # (`Q1 Report` vs `Q1.Report`). Silently letting the last one win files the other under the
        # wrong project, which is invisible afterwards. Refuse to place either, and say so.
        seen: dict[str, tuple[str, str]] = {}
        ambiguous: set[str] = set()
        for name, project in rows:
            key = _slug(name)
            previous = seen.get(key)
            if previous and previous[1] != project:
                ambiguous.add(key)
                LOG.warning(
                    "workbook names %r and %r normalise to the same key but sit in different "
                    "projects (%r vs %r) - both land at the workspace root rather than risk filing "
                    "one under the other's project",
                    previous[0],
                    name,
                    previous[1],
                    project,
                )
            seen[key] = (name, project)
            mapping[key] = project  # the site is authoritative; overwrite provenance
        for key in ambiguous:
            mapping.pop(key, None)

    return mapping


def folder_plan(projects: dict[str, str], parents: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Turn per-workbook projects into a folder PATH per workbook, deepest ancestry first.

    ``parents`` maps project -> parent project, so nesting is preserved: `91 - Calc Gauntlet` lands
    under `90 - Migration Torture Chamber` rather than beside it. Without it every project is a root
    folder, which is still correct - just flatter than Tableau was.
    """
    parents = parents or {}
    plan: dict[str, list[str]] = {}
    for workbook, project in projects.items():
        path: list[str] = []
        seen: set[str] = set()
        node: str | None = project
        while node and node not in seen:
            seen.add(node)
            path.append(node)
            node = parents.get(node)
        path.reverse()
        if len(path) > MAX_FOLDER_DEPTH:
            # Flatten the overflow into the last permitted folder rather than dropping it.
            kept = path[: MAX_FOLDER_DEPTH - 1]
            kept.append(" - ".join(path[MAX_FOLDER_DEPTH - 1 :]))
            LOG.warning("%s is %d project(s) deep; flattening below level %d", workbook, len(path), MAX_FOLDER_DEPTH)
            path = kept
        plan[workbook] = path
    return plan


def _existing_folder_paths(workspace: str, tok: str) -> dict[tuple[str, ...], str] | None:
    """Read the workspace's current folders as path-tuple -> id, or None if the API is unavailable."""
    status, _, body = call("GET", f"{API}/workspaces/{workspace}/folders", tok)
    if status != 200:
        LOG.warning("folders API unavailable (HTTP %s) - deploying flat", status)
        return None
    by_id = {f["id"]: f for f in body.get("value", [])}
    existing: dict[tuple[str, ...], str] = {}
    for folder in by_id.values():
        parts, node = [], folder
        while node:
            parts.append(node.get("displayName", ""))
            node = by_id.get(node.get("parentFolderId") or "")
        existing[tuple(reversed(parts))] = folder["id"]
    return existing


def _display_names(wanted: list[tuple[str, ...]]) -> dict[tuple[str, ...], str]:
    """Sanitized folder name per path, unique within each parent. Logs every rename."""
    by_parent: dict[tuple[str, ...], list[str]] = {}
    for path in wanted:
        by_parent.setdefault(path[:-1], []).append(path[-1])
    display: dict[tuple[str, ...], str] = {}
    for parent, children in by_parent.items():
        for original, final in unique_siblings(children).items():
            display[parent + (original,)] = final
            if final != original:
                LOG.info("folder name %r is not valid in Fabric - using %r", original, final)
    return display


def ensure_folders(workspace: str, tok: str, paths: list[list[str]]) -> dict[tuple[str, ...], str]:
    """Create every folder in the plan, parents before children. Returns path-tuple -> folder id.

    Names are sanitized per SIBLING GROUP, so two Tableau projects that coerce to the same Fabric
    name get distinct folders instead of silently pooling their content. Every rename is logged: a
    customer looking for `R&D` needs to be able to find where `R-D` came from.

    Existing folders are reused rather than duplicated, so a re-run into a partly-populated landing
    zone is safe.
    """
    existing = _existing_folder_paths(workspace, tok)
    if existing is None:
        return {}

    wanted = sorted({tuple(path[: i + 1]) for path in paths for i in range(len(path))}, key=len)
    display = _display_names(wanted)
    resolved: dict[tuple[str, ...], str] = {}

    for path in wanted:
        final_path = tuple(display.get(path[: i + 1], part) for i, part in enumerate(path))
        if final_path in existing:
            resolved[path] = existing[final_path]
            continue
        payload: dict[str, Any] = {"displayName": display.get(path, path[-1])}
        parent_id = resolved.get(path[:-1]) if len(path) > 1 else None
        if parent_id:
            payload["parentFolderId"] = parent_id
        status, _, created = call("POST", f"{API}/workspaces/{workspace}/folders", tok, payload)
        if status in (200, 201) and created.get("id"):
            resolved[path] = existing[final_path] = created["id"]
            LOG.info("folder created: %s", "/".join(final_path))
        else:
            LOG.warning("could not create folder %s (HTTP %s) - its items go to the root", "/".join(path), status)
    # `resolved`, NOT `existing`: resolved is keyed by the ORIGINAL project path, which is what the
    # caller holds. `existing` is keyed by the SANITIZED display path, so returning it silently
    # missed every project whose name had to be changed - creating the folder, leaving it empty, and
    # dumping its content at the workspace root while --dry-run promised otherwise. That failed
    # precisely for the names this sanitizer exists to handle (`v1.2`, `R&D`).
    return resolved


@dataclass
class Target:
    """Where a deploy is going, and the state it carries. Keeps the per-item helpers small."""

    workspace: str
    workspace_name: str
    token: str
    journal: Journal


def _deploy_model(target: Target, name: str, model: Item) -> tuple[str | None, str | None]:
    """Deploy (or recognise) one semantic model. Returns (model_id, failure)."""
    model.parts = parts_for(model.folder)
    recorded = target.journal.already_deployed(model)
    if recorded:
        LOG.info("%-44s model already deployed, unchanged - skipping", name)
        return recorded.get("itemId") or find_existing(target.workspace, target.token, model.name, MODEL_TYPE), None

    if target.journal.unfinished(model):
        LOG.info("%-44s model was in flight when the last run stopped; reconciling", name)
    state, model_id, detail = create_item(target.workspace, target.token, model, target.journal)
    if state == "AlreadyExists":
        LOG.info("%-44s model already present in the workspace", name)
        return find_existing(target.workspace, target.token, model.name, MODEL_TYPE), None
    if state != "Succeeded":
        LOG.error("%-44s MODEL %s: %s", name, state, detail)
        return None, f"{name} ({MODEL_TYPE}): {detail}"
    LOG.info("%-44s model deployed", name)
    return model_id, None


def report_is_empty(folder: Path) -> bool:
    """True when a report has no pages, so there is genuinely nothing to deploy.

    Fabric rejects an empty report with `Content provider provided invalid package content stream`,
    which tells an operator nothing. Measured on a real estate: 2 of 33 reports had
    `pageOrder: []` because the source workbook had no convertible worksheets, and both failed with
    that message. Detecting it here turns an opaque service error into a statement of fact.
    """
    pages = folder / "definition" / "pages" / "pages.json"
    if not pages.is_file():
        return True
    try:
        return not json.loads(pages.read_text(encoding="utf-8")).get("pageOrder")
    except (json.JSONDecodeError, OSError):
        return False  # unreadable is not the same as empty; let the service judge it


def _deploy_report(target: Target, name: str, model: Item, report: Item, model_id: str | None) -> str | None:
    """Deploy one report, rebound to its model. Returns a failure description or None."""
    if report_is_empty(report.folder):
        LOG.warning("%-44s report has NO PAGES - skipping (the model is still deployed)", name)
        return None

    if not model_id:
        LOG.error("%-44s model id unknown - refusing to deploy an unbindable report", name)
        return f"{name}: model id unknown, cannot bind the report"

    # Rebind BEFORE hashing: the digest must describe the bytes actually deployed, or a resume
    # compares against something that was never sent.
    report.parts = rebind(parts_for(report.folder), target.workspace_name, model.name, model_id)
    if target.journal.already_deployed(report):
        LOG.info("%-44s report already deployed, unchanged - skipping", name)
        return None

    state, _, detail = create_item(target.workspace, target.token, report, target.journal)
    if state == "AlreadyExists":
        LOG.info("%-44s report already present in the workspace", name)
        return None
    if state != "Succeeded":
        LOG.error("%-44s REPORT %s: %s", name, state, detail)
        return f"{name} ({REPORT_TYPE}): {detail}"
    LOG.info("%-44s report deployed and bound", name)
    return None


def _print_plan(
    pairs: list[tuple[str, Item, Item | None]],
    workspace_name: str,
    planned: int,
    placement: dict[str, list[str]],
) -> int:
    """Report what would be created, where it would land, and the item count that carries the cost."""
    LOG.info("--dry-run: nothing will be created. Plan:")
    for name, _model, report in pairs:
        where = "/".join(placement.get(_slug(name), [])) or "(workspace root)"
        LOG.info("  %-40s %-28s %s", name, where, "model + report" if report else "model only")
    if placement:
        LOG.info(
            "%d folder(s) would be created to mirror the Tableau project tree.",
            len({tuple(p) for p in placement.values()}),
        )
    unplaced = [name for name, _m, _r in pairs if _slug(name) not in placement]
    if unplaced:
        LOG.info("%d workbook(s) have no known Tableau project and would land at the root.", len(unplaced))
    LOG.info(
        "%d item(s) would be created in %r. Each item carries a cost in the customer's capacity and "
        "licensing terms, so this is the number to agree BEFORE deploying.",
        planned,
        workspace_name,
    )
    return EXIT_OK


def _run_all(target: Target, pairs: list[tuple[str, Item, Item | None]], folders: dict[str, str]) -> list[str]:
    """Deploy every pair model-first, collecting failures rather than aborting on the first."""
    failures: list[str] = []
    for name, model, report in pairs:
        model.folder_id = folders.get(_slug(name))
        model_id, failure = _deploy_model(target, name, model)
        if failure:
            failures.append(failure)
            continue
        if report:
            report.folder_id = folders.get(_slug(name))
            report_failure = _deploy_report(target, name, model, report, model_id)
            if report_failure:
                failures.append(report_failure)
    return failures


def _report_failures(failures: list[str], planned: int, workspace_name: str) -> int:
    """Print the outcome and return the exit code. A partial deploy must not exit 0."""
    if failures:
        LOG.error("%d item(s) failed:", len(failures))
        for line in failures:
            LOG.error("  %s", line)
        LOG.error("Re-run the same command to resume; deployed items are skipped by content hash.")
        return EXIT_FAILED
    LOG.info("all %d item(s) deployed into %r", planned, workspace_name)
    return EXIT_OK


def _acquire(bundle: Path, workspace: str, options: argparse.Namespace) -> tuple[RunLock, Journal] | None:
    """Take the run lock and open the journal, or report why we must not start."""
    journal_path = options.journal or (bundle / "deploy-journal.jsonl")
    lock = RunLock(journal_path.with_suffix(".lock"))
    acquired, message = lock.acquire(force=options.force_unlock)
    if not acquired:
        LOG.error("%s", message)
        return None
    return lock, Journal(journal_path, workspace)


def _resolve_folders(bundle: Path, workspace: str, tok: str, options: argparse.Namespace) -> dict[str, str]:
    """Build the folder tree and return workbook name -> folder id (absent = workspace root)."""
    if options.no_folders:
        LOG.info("--no-folders: everything lands at the workspace root")
        return {}
    projects = project_map(bundle, options.estate_db)
    if not projects:
        LOG.info("no Tableau project information found - deploying flat (pass --estate-db to mirror the tree)")
        return {}
    plan = folder_plan(projects, project_parents(options.estate_db))
    created = ensure_folders(workspace, tok, list(plan.values()))
    placed = {wb: created[tuple(path)] for wb, path in plan.items() if tuple(path) in created}
    LOG.info(
        "folders: %d project(s) mirrored, %d workbook(s) placed (the rest go to the root)",
        len({tuple(p) for p in plan.values()}),
        len(placed),
    )
    return placed


def _planned_placement(bundle: Path, options: argparse.Namespace) -> dict[str, list[str]]:
    """The folder path each workbook would get, without contacting the service."""
    if options.no_folders:
        return {}
    return folder_plan(project_map(bundle, options.estate_db), project_parents(options.estate_db))


def deploy(bundle: Path, workspace: str, tok: str, options: argparse.Namespace) -> int:
    """Deploy every workbook in the bundle, models first. Returns a process exit code."""
    pairs = discover(bundle)
    planned = sum(1 + (1 if report else 0) for _, _, report in pairs)
    LOG.info("%d workbook(s) in %s -> %d item(s)", len(pairs), bundle, planned)

    ok, message, info = preflight(workspace, tok, planned)
    LOG.info("preflight: %s", message)
    if not ok:
        return EXIT_PREFLIGHT
    workspace_name = info.get("displayName", workspace)

    if options.dry_run:
        return _print_plan(pairs, workspace_name, planned, _planned_placement(bundle, options))

    held = _acquire(bundle, workspace, options)
    if held is None:
        return EXIT_PREFLIGHT
    lock, journal = held
    try:
        folders = _resolve_folders(bundle, workspace, tok, options)
        failures = _run_all(Target(workspace, workspace_name, tok, journal), pairs, folders)
    finally:
        lock.release()
    return _report_failures(failures, planned, workspace_name)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path, help="estate bundle from run_estate.py")
    parser.add_argument("--workspace", required=True, help="EXISTING landing-zone workspace id (never created here)")
    parser.add_argument("--tenant", help="Entra tenant id; omit to use the Azure CLI default")
    parser.add_argument("--dry-run", action="store_true", help="report the plan and item count, create nothing")
    parser.add_argument(
        "--force-unlock", action="store_true", help="take the run lock even if another deploy appears to hold it"
    )
    parser.add_argument("--journal", type=Path, help="run journal path (default <bundle>/deploy-journal.jsonl)")
    parser.add_argument(
        "--estate-db",
        type=Path,
        help="assess_estate.py estate.db, so the Tableau project tree is mirrored as workspace folders",
    )
    parser.add_argument("--no-folders", action="store_true", help="deploy everything to the workspace root")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return deploy(args.bundle, args.workspace, token(args.tenant), args)


if __name__ == "__main__":
    raise SystemExit(main())
