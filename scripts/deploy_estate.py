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
# The deploy loop is one coherent procedure - preflight, folders, models, rebind, reports - and the
# long prose above each step is the measured knowledge that keeps it correct. Splitting it to satisfy
# a line count would scatter that, exactly as `parse_tableau.py` concluded.
# pylint: disable=too-many-lines

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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import quote

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

# Stamped into every item this tool creates, so a later run can ask the SERVICE what it owns rather
# than trusting a local journal that may be gone. Ownership matters because item identity in Fabric
# is only (displayName, type): without it, an unrelated customer report called `Sales` is
# indistinguishable from ours, and was silently overwritten.
PROVENANCE = "Deployed by tableau-to-powerbi-migration"
# The stamp also records WHICH estate an item came from. Without that, the marker only says "some
# run of this tool made this", and a second estate deployed into the same landing zone silently
# overwrote a same-named item from the first - one item where there should be two, the first
# project's folder left empty, and both runs exiting 0.
SOURCE_PREFIX = "| source:"

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

# A dropped network marks every remaining item "Failed" one by one, which is noise rather than
# information - and each one then needs reconciling on the resume. Measured: a laptop moved between
# networks mid-deploy and burned through the rest of the estate emitting `getaddrinfo failed`.
# Stop after a few consecutive connectivity errors and say so plainly instead.
MAX_CONSECUTIVE_NETWORK_FAILURES = 3


@dataclass
class Item:
    """One deployable item: a folder of definition parts plus where it goes."""

    name: str
    item_type: str
    folder: Path
    parts: list[dict[str, str]] = field(default_factory=list)
    folder_id: str | None = None
    # Which estate this came from, recorded in the item's stamp so a second estate cannot silently
    # overwrite a same-named item belonging to the first.
    source: str = ""

    @property
    def digest(self) -> str:
        """Hash of the exact bytes about to be deployed, so a resume can tell 'done' from 'stale'."""
        sha = hashlib.sha256()
        for part in self.parts:
            sha.update(part["path"].encode("utf-8"))
            sha.update(part["payload"].encode("ascii"))
        return sha.hexdigest()


class Token:
    """A Fabric bearer token that re-mints itself when the service says it has expired.

    Minting once at startup is fine for a probe and wrong for an estate: a real deploy of 66 items
    ran past the token's lifetime and every remaining call failed with
    `401 TokenExpired`, leaving a half-deployed workspace. The run was resumable, but an operator
    should not have to notice and re-run in front of a customer.
    """

    def __init__(self, tenant: str | None) -> None:
        self.tenant = tenant
        self._value = self._mint()

    def _mint(self) -> str:
        cmd = ["az", "account", "get-access-token", "--resource", FABRIC_RESOURCE]
        if self.tenant:
            cmd += ["--tenant", self.tenant]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=True)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                "Could not get a Fabric token from the Azure CLI. Run `az login`"
                + (f" --tenant {self.tenant}" if self.tenant else "")
                + f".\n  {exc.stderr.strip()[:300]}"
            ) from exc
        return json.loads(out.stdout)["accessToken"]

    def refresh(self) -> str:
        """Mint a new token. Called when the service reports the current one expired."""
        LOG.info("access token expired - renewing and continuing")
        self._value = self._mint()
        return self._value

    def __str__(self) -> str:
        return self._value


def token(tenant: str | None) -> Token:
    """Mint a Fabric-scoped bearer token via the Azure CLI.

    `az` rather than `fab`: the same code path serves an interactive user (`az login`) and a service
    principal (`az login --service-principal`), so an unattended run needs no second mechanism.
    """
    return Token(tenant)


def call(method: str, url: str, tok: Any, body: dict | None = None) -> tuple[int, dict, dict]:
    """One REST call. Returns (status, headers, parsed-body); never raises on an HTTP error.

    Retries ONCE on `401 TokenExpired` with a freshly minted token: a long deploy outlives its
    token, and the alternative is a half-deployed workspace and a puzzled operator. Any other 401 is
    a real authorization problem and is returned as-is rather than retried.
    """
    status, headers, parsed = _request(method, url, str(tok), body)
    if status == 401 and isinstance(tok, Token) and "TokenExpired" in json.dumps(parsed):
        status, headers, parsed = _request(method, url, tok.refresh(), body)
    return status, headers, parsed


def _request(method: str, url: str, bearer: str, body: dict | None = None) -> tuple[int, dict, dict]:
    """The bare HTTP call, with no retry policy of its own."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {bearer}")
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


def _preferred_model(folder: Path, models: list[Path], reports: list[Path]) -> Path:
    """Pick the model that belongs to this workbook when several sit side by side.

    The report names its own model in `definition.pbir`'s `byPath`; that is ground truth and beats
    any ordering heuristic. Falling back to the folder-named model beats alphabetical, which shipped
    an unrelated model under the workbook's name.
    """
    if reports:
        pbir = reports[0] / "definition.pbir"
        try:
            ref = json.loads(pbir.read_text(encoding="utf-8"))["datasetReference"]["byPath"]["path"]
            wanted = (reports[0].parent / ref).resolve()
            for candidate in models:
                if candidate.resolve() == wanted:
                    return candidate
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            pass  # no usable byPath; fall through to the name match
    for candidate in models:
        if candidate.stem == folder.name:
            return candidate
    return models[0]


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
        models = sorted(folder.glob("*.SemanticModel"))
        reports = sorted(folder.glob("*.Report"))
        if not models:
            LOG.warning("%s has no .SemanticModel folder - skipping", folder.name)
            continue
        # Which model? `models[0]` of an unsorted glob chose arbitrarily; sorting alone made that
        # choice deterministic AND deterministically wrong, shipping the alphabetically-first folder
        # under the workbook's name. Prefer the one the report's `definition.pbir` actually points
        # at, then the one named after the workbook, and only then fall back to the first.
        model_path = _preferred_model(folder, models, reports)
        for extra in [m for m in models if m != model_path] + reports[1:]:
            LOG.warning(
                "%s holds more than one item folder - using %s, ignoring %s",
                folder.name,
                model_path.name,
                extra.name,
            )
        model = Item(folder.name, MODEL_TYPE, model_path)
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
        self.attempts: set[tuple[str, str, str]] = set()
        self.item_ids: set[str] = set()
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
                if row.get("workspace") == self.workspace and row.get("itemId"):
                    # Every id this journal has ever seen in THIS workspace is an id we may claim.
                    # Ownership is what stops us overwriting an item the customer put here.
                    self.item_ids.add(row["itemId"])
                if row.get("phase") == "outcome" and row.get("status") == "Succeeded":
                    self.done[key] = row
                    self.pending.pop(key, None)
                elif row.get("phase") == "outcome":
                    self.attempts.add(key)  # Timeout/Failed: may still exist server-side
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

    def attempted(self, item: Item) -> bool:
        """True if we ever started this item without recording a clean success.

        Covers three states that look different in the log and identical to the service: an intent
        with no outcome (crash between the call and the record), a `Timeout` (our poll gave up while
        the operation continued), and a `Failed` that may still have created the item. All three
        require asking the service what exists before creating anything.
        """
        return self._key(item) in self.pending or self._key(item) in self.attempts


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


def preflight(
    workspace: str, tok: str, planned: int, planned_keys: list[tuple[str, str]] | None = None
) -> tuple[bool, str, dict[str, Any]]:
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
    status, rows = list_all(workspace, tok, "items")
    if status != 200:
        return False, f"could not list items in {name!r}: HTTP {status} - is this identity a Contributor?", body
    existing = len(rows)
    # Only items that would be CREATED consume budget. Counting the run's own items as both
    # "existing" and "planned" made every resume double-count them, so any estate above half the
    # budget could be deployed once and then never re-run - and resuming is this script's purpose.
    already = {(r.get("displayName"), r.get("type")) for r in rows}
    creating = len([key for key in planned_keys or [] if key not in already]) if planned_keys else planned
    budget = int(WORKSPACE_ITEM_LIMIT * (1 - HEADROOM)) - existing
    if creating > budget:
        return (
            False,
            f"{creating} new item(s) planned but only {budget} fit in {name!r} "
            f"({existing} already there, {WORKSPACE_ITEM_LIMIT}-item limit, {int(HEADROOM * 100)}% headroom kept). "
            "Supply a second workspace or narrow the scope.",
            body,
        )
    return (
        True,
        f"{name!r}: {existing} existing item(s), {planned} planned ({creating} new), {budget - creating} spare",
        body,
    )


def _retry_after(headers: dict, default: float = 20.0) -> float:
    """Seconds to wait from a `Retry-After` header.

    RFC 9110 allows an HTTP-date as well as a delay in seconds; `float()` on the date form raised
    `ValueError` and killed the deploy mid-estate.
    """
    raw = str(headers.get("retry-after", "")).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    if when is None:
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), 300.0))


def _post_item(workspace: str, tok: str, item: Item) -> tuple[int, dict, dict]:
    """POST one item definition, honouring a 429 `Retry-After` once.

    One retry, not a loop: 429 carries the service's own instruction, so waiting exactly that long
    and trying once is a bounded response. Anything persistent is a real problem to report, not to
    grind against.
    """
    body = {"displayName": item.name, "type": item.item_type, "definition": {"parts": item.parts}}
    # A marker the SERVICE can answer for. Ownership that lives only in a local journal is lost with
    # the journal - and then the deployer refuses to touch its own previous output, which is safe but
    # useless. Stamped here, a later run recognises what it deployed from any machine.
    body["description"] = stamp_for(item)
    if item.folder_id:
        # Placed AT CREATION - `folderId` is a field on Create Item, so there is no create-then-move
        # dance and no window where the item sits in the wrong place.
        body["folderId"] = item.folder_id
    status, headers, resp = call("POST", f"{API}/workspaces/{workspace}/items", tok, body)
    if status == 429:
        wait = _retry_after(headers)
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


def list_all(workspace: str, tok: str, collection: str) -> tuple[int, list[dict[str, Any]]]:
    """Read every page of a Fabric list endpoint.

    Fabric returns a `continuationToken` once a collection outgrows one page. Reading only the
    first page makes an item that sits past the boundary look ABSENT, and "absent" is precisely
    what makes this deployer create a second copy. A landing zone holds two items per workbook, so
    a customer estate reaches that boundary long before a 33-workbook test bundle does - the
    measured run above fitted in a single page and so could never have caught this.
    """
    url = f"{API}/workspaces/{workspace}/{collection}"
    rows: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    while True:
        status, _, body = call("GET", url, tok)
        if status != 200:
            return status, rows
        rows.extend(body.get("value", []))
        next_page = body.get("continuationToken")
        # A server that keeps handing back the same token would otherwise loop forever.
        if not next_page or next_page in seen_tokens:
            return status, rows
        seen_tokens.add(next_page)
        url = body.get("continuationUri") or (
            f"{API}/workspaces/{workspace}/{collection}?continuationToken={quote(next_page)}"
        )


class Landing:
    """What the landing zone already holds, read ONCE at the start of a run.

    Three separate defects shared one root cause: asking the service per item, and treating *"I
    could not ask"* as *"it is not there"*. `find_existing` returned `None` for a failed listing
    exactly as it did for a genuine absence, and absent is what makes this deployer create a second
    copy - so a single transient 500 or an unhandled 429 on the read produced a duplicate while the
    run still exited 0. A clean run issued one full paged listing PER ITEM, so a large estate offered
    hundreds of independent chances to hit it.

    Reading once, up front, converts that into a single failure the run can refuse to start on.

    It also carries the thing a name lookup cannot: **ownership**. A customer-supplied landing zone
    is not necessarily empty, and item identity here is only (displayName, type) - so an unrelated
    report called `Sales` was indistinguishable from ours and was overwritten in place, reported as
    "already existed - definition updated". We therefore keep the ids that were present BEFORE this
    run touched anything, and claim only those the journal can account for.
    """

    def __init__(self, rows: list[dict[str, Any]], adopt: bool = False) -> None:
        self.rows: dict[str, dict[str, Any]] = {r["id"]: r for r in rows if r.get("id")}
        self.preexisting: set[str] = set(self.rows)
        self.adopt = adopt

    @classmethod
    def read(cls, workspace: str, tok: str, adopt: bool = False) -> tuple[Landing | None, str]:
        """Read the workspace, or return the reason we must not proceed without it."""
        status, rows = list_all(workspace, tok, "items")
        if status != 200:
            return None, (
                f"could not read the contents of workspace {workspace} (HTTP {status}). "
                "Refusing to deploy: an unreadable workspace is indistinguishable from an empty one, "
                "and deploying into it would create a second copy of everything already there."
            )
        return cls(rows, adopt), ""

    def matching(self, name: str, item_type: str) -> list[dict[str, Any]]:
        """Every item in the workspace with this exact name and type."""
        return [r for r in self.rows.values() if r.get("displayName") == name and r.get("type") == item_type]

    def _is_stamped(self, row: dict[str, Any]) -> bool:
        """True if the SERVICE says a run of this tool created this item.

        Deliberately NOT `journal.attempted(item)`. That asked "did we ever start an item with this
        name?" without looking at the row at all, so a single failed create authorised overwriting
        ANY item of that name for the life of the journal - and since a create that failed is
        exactly what this deployer exists to resume from, that is a routine precondition, not an
        exotic one. It also destroyed a customer's content and then stamped it as ours, making the
        damage permanent.

        The stamp is strictly better evidence, and it covers the case `attempted()` was there for:
        `_post_item` sends the marker in the CREATE body, so an item that was created just before a
        crash is already stamped when we come back for it.
        """
        return str(row.get("description") or "").startswith(PROVENANCE)

    def source_of(self, row: dict[str, Any]) -> str:
        """Which estate this item came from, as recorded in its stamp ("" if unknown)."""
        text = str(row.get("description") or "")
        marker = f"{PROVENANCE} {SOURCE_PREFIX}"
        return text.split(marker, 1)[1].strip() if marker in text else ""

    def claim(self, item: Item, journal: Journal) -> tuple[str | None, str | None]:
        """Decide which existing item (if any) is OURS to update. Returns (item_id, refusal)."""
        candidates = self.matching(item.name, item.item_type)
        if not candidates:
            return None, None
        ours = [
            row
            for row in candidates
            if self.adopt  # the operator has explicitly taken responsibility for this workspace
            or row["id"] not in self.preexisting  # created during this very run
            or row["id"] in journal.item_ids  # recorded by this journal, in this workspace
            or self._is_stamped(row)  # marked by a previous run of this tool, in the SERVICE
        ]
        if not ours:
            return None, (
                f"{item.name} ({item.item_type}): an item of this name already exists in the workspace, "
                "it carries no marker from a previous deploy, and this run's journal has no record of "
                "creating it. Refusing to overwrite content that may not be ours. If this landing zone "
                "IS ours and the journal was lost, re-run with --adopt-existing; otherwise rename the "
                "workbook or use an empty workspace."
            )
        if len(ours) > 1:
            return None, (
                f"{item.name} ({item.item_type}): {len(ours)} items share this name, so which one to "
                "update is ambiguous. Remove the extras in the workspace, then re-run."
            )
        mine = ours[0]
        theirs = self.source_of(mine)
        if theirs and item.source and theirs != item.source and not self.adopt:
            # Two DIFFERENT workbooks that happen to share a name. Fabric identity is only
            # (displayName, type), so without this the second estate silently overwrote the first,
            # left its folder empty, and both runs exited 0.
            return None, (
                f"{item.name} ({item.item_type}): an item of this name in this workspace came from a "
                f"different source ({theirs!r}, not {item.source!r}). Overwriting it would merge two "
                "distinct workbooks into one item. Use a separate landing zone per estate, or rename."
            )
        return mine["id"], None

    def folder_of(self, item_id: str) -> str | None:
        """Which folder the workspace currently has this item in."""
        return self.rows.get(item_id, {}).get("folderId")

    def record(self, item_id: str, name: str, item_type: str, folder_id: str | None) -> None:
        """Remember something we just created, so later items see it without another listing."""
        self.rows[item_id] = {
            "id": item_id,
            "displayName": name,
            "type": item_type,
            "folderId": folder_id,
            "description": PROVENANCE,
        }

    def describe(self, item_id: str) -> str:
        """The item's description, which is where our ownership marker lives."""
        return str(self.rows.get(item_id, {}).get("description") or "")

    def mark(self, item_id: str, stamp: str = PROVENANCE) -> None:
        """Note that the service has accepted our ownership marker for this item."""
        if item_id in self.rows:
            self.rows[item_id]["description"] = stamp


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


def ambiguous_projects(estate_db: Path | None) -> set[str]:
    """Project names that occur more than once in the tree, and so cannot identify a folder.

    `workbook.project` records a NAME, so two projects called `Reports` under different parents are
    indistinguishable from a workbook's point of view. Placing either is a coin flip that silently
    pools both, and placing them in a shared root-level `Reports` folder pools them just the same -
    the first version of this fix only dropped the NESTING and left exactly that hole.
    """
    if not (estate_db and estate_db.is_file()):
        return set()
    try:
        with sqlite3.connect(f"file:{estate_db}?mode=ro", uri=True) as conn:
            names = [name for (name,) in conn.execute("select name from project")]
    except sqlite3.Error:
        return set()
    return {name for name in names if names.count(name) > 1}


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
    duplicated = ambiguous_projects(estate_db)
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
                # BOTH tables. A migrated estate contains published DATASOURCES as well as
                # workbooks, and they carry their own project - reading only `workbook` left every
                # datasource at the workspace root, which looks like a placement failure rather than
                # like a table nobody queried.
                rows = list(conn.execute("select name, project from workbook where project is not null"))
                rows += list(conn.execute("select name, project from datasource where project is not null"))
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
    status, rows = list_all(workspace, tok, "folders")
    if status != 200:
        LOG.warning("folders API unavailable (HTTP %s) - deploying flat", status)
        return None
    by_id = {f["id"]: f for f in rows}
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

    wanted = sorted({tuple(path[: i + 1]) for path in paths for i in range(len(path))}, key=lambda p: (len(p), p))
    display = _display_names(wanted)
    resolved: dict[tuple[str, ...], str] = {}
    failed: set[tuple[str, ...]] = set()

    for path in wanted:
        parent = path[:-1]
        if parent and parent in failed:
            # Creating the child anyway put it at the WORKSPACE ROOT under its own bare name, so
            # `Finance/Q1` became a root-level `Q1` indistinguishable from `HR/Q1` - while the log
            # said its items go to the root. Skip the subtree and say so once, accurately.
            failed.add(path)
            continue
        final_path = tuple(display.get(path[: i + 1], part) for i, part in enumerate(path))
        if final_path in existing:
            resolved[path] = existing[final_path]
            continue
        payload: dict[str, Any] = {"displayName": display.get(path, path[-1])}
        parent_id = resolved.get(parent) if len(path) > 1 else None
        if parent_id:
            payload["parentFolderId"] = parent_id
        status, _, created = call("POST", f"{API}/workspaces/{workspace}/folders", tok, payload)
        if status in (200, 201) and created.get("id"):
            resolved[path] = existing[final_path] = created["id"]
            LOG.info("folder created: %s", "/".join(final_path))
        else:
            failed.add(path)
            LOG.warning(
                "could not create folder %s (HTTP %s) - it and anything below it go to the root",
                "/".join(path),
                status,
            )
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
    landing: Landing = field(default_factory=lambda: Landing([]))
    source: str = ""


def update_item(workspace: str, tok: Any, item: Item, item_id: str, journal: Journal) -> tuple[str, str]:
    """Overwrite an existing item's definition in place. Returns (status, detail).

    The counterpart to `create_item`, and the reason a re-run cannot duplicate: an item that already
    exists is UPDATED, never created again. `updateDefinition` overwrites the definition and leaves
    the sensitivity label alone.
    """
    journal.intent(item, "update")
    status, headers, resp = call(
        "POST",
        f"{API}/workspaces/{workspace}/items/{item_id}/updateDefinition",
        tok,
        {"definition": {"parts": item.parts}},
    )
    operation = headers.get("x-ms-operation-id")
    if status in (200, 201):
        journal.outcome(item, "Succeeded", item_id, operation)
        return "Succeeded", ""
    if status == 202:
        location = headers.get("location") or (f"{API}/operations/{operation}" if operation else "")
        state, body = await_operation(location, tok) if location else ("Failed", {})
        detail = "" if state == "Succeeded" else json.dumps(body.get("error", body))[:400]
        journal.outcome(item, state, item_id if state == "Succeeded" else None, operation, detail)
        return state, detail
    detail = f"HTTP {status} {json.dumps(resp)[:300]}"
    journal.outcome(item, "Failed", None, operation, detail)
    return "Failed", detail


def move_item(workspace: str, tok: str, item_id: str, folder_id: str | None) -> tuple[bool, str]:
    """Re-place an existing item. Returns (moved, detail).

    `updateDefinition` overwrites content and ignores placement, so without this an item that
    changed folder stayed where it was while the journal recorded the folder we INTENDED. That
    false record then matched on every later run and skipped the item forever - two empty folders
    created, "2 workbook(s) placed" logged, and nothing actually moved.
    """
    body = {"targetFolderId": folder_id} if folder_id else {}
    status, _, resp = call("POST", f"{API}/workspaces/{workspace}/items/{item_id}/move", tok, body)
    if status in (200, 201, 202):
        return True, ""
    return False, f"HTTP {status} {json.dumps(resp)[:200]}"


def stamp_for(item: Item) -> str:
    """The description we write on an item we own, including which estate it came from."""
    return f"{PROVENANCE} {SOURCE_PREFIX} {item.source}".rstrip() if item.source else PROVENANCE


def stamp_item(workspace: str, tok: str, item_id: str, item: Item) -> bool:
    """Mark an item as ours. Returns True if the service accepted the mark.

    `updateDefinition` carries only the definition, so adopting an item left it unstamped and the
    NEXT run refused it again - the escape hatch would have been needed forever, which defeats the
    point of having a marker at all.
    """
    status, _, _ = call("PATCH", f"{API}/workspaces/{workspace}/items/{item_id}", tok, {"description": stamp_for(item)})
    return status in (200, 201)


def _update_existing(target: Target, item: Item, existing: str, kind: str, name: str) -> tuple[str | None, str | None]:
    """Refresh an item we already own, and re-place it if its folder changed."""
    state, detail = update_item(target.workspace, target.token, item, existing, target.journal)
    if state != "Succeeded":
        LOG.error("%-44s %s UPDATE %s: %s", name, kind.upper(), state, detail)
        return None, f"{name} ({item.item_type}): {detail}"
    if target.landing.describe(existing) != stamp_for(item):
        if stamp_item(target.workspace, target.token, existing, item):
            target.landing.mark(existing, stamp_for(item))
    if target.landing.folder_of(existing) != item.folder_id:
        moved, why = move_item(target.workspace, target.token, existing, item.folder_id)
        if moved:
            target.landing.record(existing, item.name, item.item_type, item.folder_id)
        else:
            # Record where it ACTUALLY is, so a later run retries the move instead of skipping - and
            # report it, because a run that exits 0 forever while the estate does not match the plan
            # is indistinguishable from one that worked.
            LOG.error("%-44s %s could not be moved to its folder: %s", name, kind.upper(), why)
            item.folder_id = target.landing.folder_of(existing)
            target.journal.outcome(item, "Succeeded", existing, None, "definition updated; move failed")
            return existing, f"{name} ({item.item_type}): updated, but could not be placed in its folder. {why}"
    LOG.info("%-44s %s already existed - definition updated in place", name, kind)
    return existing, None


def _deploy_one(target: Target, item: Item, kind: str, name: str) -> tuple[str | None, str | None]:
    """Create, update or adopt one item. Returns (item_id, failure).

    Never creates without first consulting what the workspace actually holds, and never treats an
    unreadable answer as an empty one. Each duplicate path below was measured against a real
    workspace rather than imagined:

      * a `Timeout` that our poll gave up on while the operation completed;
      * a crash between the POST and the outcome record;
      * a re-run after the item's PLACEMENT changed, where the content is identical but the folder
        is not - this one duplicated ten items, because the deployer treated a moved item as new;
      * a transient failure of the *existence check itself*, which read as "absent" and so created a
        second copy while the run still exited 0.

    Fabric does not reject a repeated `Report`/`SemanticModel` name, so nothing downstream would
    have caught any of them.
    """
    existing, refusal = target.landing.claim(item, target.journal)
    if refusal:
        LOG.error("%-44s %s REFUSED: %s", name, kind.upper(), refusal)
        return None, refusal

    if existing and target.journal.already_deployed(item) and target.landing.folder_of(existing) == item.folder_id:
        # The journal alone is not evidence the item is still there: deleting a broken item in the
        # portal and re-running is the obvious way to force a clean redeploy, and this fast path
        # used to report success over a workspace the item had been removed from, leaving its report
        # bound to a GUID that no longer resolved. The folder is compared against the WORKSPACE, not
        # the journal, so an item moved by hand in the portal is put back rather than declared fine.
        LOG.info("%-44s %s already deployed, unchanged - skipping", name, kind)
        return existing, None

    if existing:
        return _update_existing(target, item, existing, kind, name)
    return _create_new(target, item, kind, name)


def _create_new(target: Target, item: Item, kind: str, name: str) -> tuple[str | None, str | None]:
    """Create an item that the workspace genuinely does not have."""
    state, item_id, detail = create_item(target.workspace, target.token, item, target.journal)
    if state == "AlreadyExists":
        # The service says the name is taken but our own index does not know it. Re-read once: if it
        # still cannot be located, this is NOT a success - reporting it as one declared a complete
        # deploy over an empty workspace.
        fresh, why = Landing.read(target.workspace, target.token)
        adopted = None if fresh is None else (fresh.claim(item, target.journal)[0])
        if not adopted:
            LOG.error("%-44s %s name is taken but the item cannot be located: %s", name, kind.upper(), detail)
            return None, f"{name} ({item.item_type}): name already in use but no matching item is readable. {why}"
        target.landing.record(adopted, item.name, item.item_type, item.folder_id)
        LOG.info("%-44s %s already present in the workspace", name, kind)
        return adopted, None
    if state != "Succeeded":
        LOG.error("%-44s %s %s: %s", name, kind.upper(), state, detail)
        return None, f"{name} ({item.item_type}): {detail}"
    target.landing.record(item_id, item.name, item.item_type, item.folder_id)
    LOG.info("%-44s %s deployed", name, kind)
    return item_id, None


def _deploy_model(target: Target, name: str, model: Item) -> tuple[str | None, str | None]:
    """Deploy (or recognise) one semantic model. Returns (model_id, failure)."""
    model.parts = parts_for(model.folder)
    return _deploy_one(target, model, "model", name)


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
    _item_id, failure = _deploy_one(target, report, "report", name)
    return failure


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


def _refusals(target: Target, pairs: list[tuple[str, Item, Item | None]]) -> list[str]:
    """Every planned item we would refuse to touch, checked BEFORE anything is created.

    `preflight` classifies a name clash as "an update, not a new item", but `claim` may refuse that
    same name - so a run could create the model, then discover the report was foreign, and stop with
    an orphan in the customer's workspace that the refusal message never mentions. Checking the whole
    plan first makes that refusal free.
    """
    found: list[str] = []
    for _name, model, report in pairs:
        for item in (model, report):
            if item is None:
                continue
            _, refusal = target.landing.claim(item, target.journal)
            if refusal:
                found.append(refusal)
    return found


def _run_all(
    target: Target, pairs: list[tuple[str, Item, Item | None]], folders: dict[str, str]
) -> tuple[list[str], int]:
    """Deploy every pair model-first. Returns (failures, items skipped as empty)."""
    failures: list[str] = []
    skipped = 0
    offline = 0
    for name, model, report in pairs:
        for item in (model, report):
            if item is not None:
                item.folder_id = folders.get(_slug(name))
                item.source = target.source
    blocked = _refusals(target, pairs)
    if blocked:
        LOG.error("%d planned item(s) cannot be deployed into this workspace:", len(blocked))
        for line in blocked:
            LOG.error("  %s", line)
        LOG.error("Nothing was created - refusing up front rather than leaving a half-deployed estate.")
        return blocked, 0
    for name, model, report in pairs:
        model.folder_id = folders.get(_slug(name))
        model_id, failure = _deploy_model(target, name, model)
        if failure:
            failures.append(failure)
            # `HTTP 0` is our own client failing to resolve or reach the host, not a service verdict.
            offline = offline + 1 if "HTTP 0" in failure else 0
            if offline >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                LOG.error(
                    "network unreachable for %d consecutive item(s) - stopping rather than marking "
                    "the rest of the estate failed. Check connectivity, then re-run the same command "
                    "to resume; everything already deployed is skipped by content hash.",
                    offline,
                )
                break
            continue
        offline = 0
        if report:
            report.folder_id = folders.get(_slug(name))
            report_failure = _deploy_report(target, name, model, report, model_id)
            if report_failure:
                failures.append(report_failure)
            elif report_is_empty(report.folder):
                skipped += 1
    return failures, skipped


def _report_failures(failures: list[str], planned: int, workspace_name: str, skipped: int = 0) -> int:
    """Print the outcome and return the exit code. A partial deploy must not exit 0.

    Reports what actually happened rather than what was planned: saying "all 66 deployed" when two
    empty reports were skipped is a small lie that erodes trust in every other number we print.
    """
    if failures:
        LOG.error("%d item(s) failed:", len(failures))
        for line in failures:
            LOG.error("  %s", line)
        LOG.error("Re-run the same command to resume; deployed items are skipped by content hash.")
        return EXIT_FAILED
    deployed = planned - skipped
    if skipped:
        LOG.info("%d item(s) deployed into %r; %d skipped as empty", deployed, workspace_name, skipped)
    else:
        LOG.info("all %d item(s) deployed into %r", deployed, workspace_name)
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


def _placement_plan(bundle: Path, options: argparse.Namespace) -> dict[str, list[str]]:
    """Folder path per workbook, with unplaceable ones omitted entirely.

    A workbook whose project name is ambiguous is left OUT rather than filed under a same-named
    folder: sharing a root-level `Reports` folder pools two different projects' content just as
    surely as nesting it under the wrong parent.
    """
    if options.no_folders:
        return {}
    projects = project_map(bundle, options.estate_db)
    ambiguous = ambiguous_projects(options.estate_db)
    if ambiguous:
        LOG.warning(
            "project name(s) %s occur more than once in the Tableau tree and cannot be told apart "
            "from a workbook's project name - their content lands at the workspace ROOT rather than "
            "risking two projects pooling into one folder",
            ", ".join(sorted(ambiguous)),
        )
        projects = {wb: project for wb, project in projects.items() if project not in ambiguous}
    return folder_plan(projects, project_parents(options.estate_db))


def _resolve_folders(bundle: Path, workspace: str, tok: str, options: argparse.Namespace) -> dict[str, str]:
    """Build the folder tree and return workbook name -> folder id (absent = workspace root)."""
    if options.no_folders:
        LOG.info("--no-folders: everything lands at the workspace root")
        return {}
    plan = _placement_plan(bundle, options)
    if not plan:
        LOG.info("no Tableau project information found - deploying flat (pass --estate-db to mirror the tree)")
        return {}
    created = ensure_folders(workspace, tok, list(plan.values()))
    placed = {wb: created[tuple(path)] for wb, path in plan.items() if tuple(path) in created}
    LOG.info(
        "folders: %d project path(s) mirrored, %d workbook(s) placed (the rest go to the root)",
        len({tuple(p) for p in plan.values()}),
        len(placed),
    )
    return placed


def _execute(
    bundle: Path, workspace: str, workspace_name: str, tok: Any, options: argparse.Namespace
) -> tuple[list[str], int] | None:
    """Take the lock, build the folders, deploy. None when the lock could not be taken."""
    held = _acquire(bundle, workspace, options)
    if held is None:
        return None
    lock, journal = held
    try:
        landing, why = Landing.read(workspace, tok, adopt=getattr(options, "adopt_existing", False))
        if landing is None:
            LOG.error("%s", why)
            return None
        if landing.adopt:
            LOG.warning("--adopt-existing: same-named items already in this workspace WILL be overwritten")
        folders = _resolve_folders(bundle, workspace, tok, options)
        target = Target(workspace, workspace_name, tok, journal, landing, bundle.name)
        return _run_all(target, pairs=discover(bundle), folders=folders)
    finally:
        lock.release()


def deploy(bundle: Path, workspace: str, tok: Any, options: argparse.Namespace) -> int:
    """Deploy every workbook in the bundle, models first. Returns a process exit code."""
    pairs = discover(bundle)
    planned = sum(1 + (1 if report else 0) for _, _, report in pairs)
    planned_keys = [(name, MODEL_TYPE) for name, _, _ in pairs]
    planned_keys += [(name, REPORT_TYPE) for name, _, report in pairs if report]
    LOG.info("%d workbook(s) in %s -> %d item(s)", len(pairs), bundle, planned)

    ok, message, info = preflight(workspace, tok, planned, planned_keys)
    LOG.info("preflight: %s", message)
    if not ok:
        return EXIT_PREFLIGHT
    workspace_name = info.get("displayName", workspace)

    if options.dry_run:
        return _print_plan(pairs, workspace_name, planned, _placement_plan(bundle, options))

    outcome = _execute(bundle, workspace, workspace_name, tok, options)
    if outcome is None:
        return EXIT_PREFLIGHT
    failures, skipped = outcome
    return _report_failures(failures, planned, workspace_name, skipped)


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
    parser.add_argument(
        "--adopt-existing",
        action="store_true",
        help=(
            "take ownership of same-named items already in the workspace. Only for a landing zone "
            "that IS ours whose journal was lost - it overwrites those items in place."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return deploy(args.bundle, args.workspace, token(args.tenant), args)


if __name__ == "__main__":
    raise SystemExit(main())
