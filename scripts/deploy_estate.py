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

    def __init__(self, path: Path) -> None:
        self.path = path
        self.done: dict[tuple[str, str], dict[str, Any]] = {}
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn final line is expected after a hard kill
                key = (row.get("item", ""), row.get("type", ""))
                if row.get("phase") == "outcome" and row.get("status") == "Succeeded":
                    self.done[key] = row
                    self.pending.pop(key, None)
                elif row.get("phase") == "intent":
                    self.pending[key] = row

    def _write(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
            handle.flush()

    def intent(self, item: Item, action: str) -> None:
        """Record what we are about to do, before doing it."""
        self._write(
            {
                "phase": "intent",
                "item": item.name,
                "type": item.item_type,
                "action": action,
                "definition_sha256": item.digest,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    def outcome(self, item: Item, status: str, item_id: str | None, operation: str | None, detail: str = "") -> None:
        """Record how it went, including the operation id a resume may need to poll."""
        self._write(
            {
                "phase": "outcome",
                "item": item.name,
                "type": item.item_type,
                "status": status,
                "itemId": item_id,
                "operationId": operation,
                "definition_sha256": item.digest,
                "detail": detail[:400],
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    def already_deployed(self, item: Item) -> dict[str, Any] | None:
        """Return the recorded success ONLY if the definition is byte-identical to what we now hold.

        The hash is the point. 'An item with this name exists' is the check that silently ships a
        half-uploaded item; 'the content I intend is there' is the one that does not.
        """
        row = self.done.get((item.name, item.item_type))
        if row and row.get("definition_sha256") == item.digest:
            return row
        return None

    def unfinished(self, item: Item) -> dict[str, Any] | None:
        """An intent with no matching success - the crash-in-flight case worth polling, not retrying."""
        return self.pending.get((item.name, item.item_type))


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
        """Take the lock. Returns (ok, message)."""
        if self.path.exists() and not force:
            try:
                held = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                held = {}
            age = time.time() - self.path.stat().st_mtime
            return False, (
                f"another deploy holds {self.path.name} (pid {held.get('pid', '?')}, "
                f"started {held.get('at', '?')}, {age / 60:.0f} min ago). Wait for it, or re-run with "
                "--force-unlock if you are certain it is dead. Two concurrent runs create DUPLICATE "
                "items: Fabric does not reject a repeated report/model name."
            )
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
            encoding="utf-8",
        )
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


def _deploy_report(target: Target, name: str, model: Item, report: Item, model_id: str | None) -> str | None:
    """Deploy one report, rebound to its model. Returns a failure description or None."""
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


def _print_plan(pairs: list[tuple[str, Item, Item | None]], workspace_name: str, planned: int) -> int:
    """Report what would be created, and the item count that carries the customer's cost."""
    LOG.info("--dry-run: nothing will be created. Plan:")
    for name, _model, report in pairs:
        LOG.info("  %-44s %s%s", name, MODEL_TYPE, f" + {REPORT_TYPE}" if report else " (model only)")
    LOG.info(
        "%d item(s) would be created in %r. Each item carries a cost in the customer's capacity and "
        "licensing terms, so this is the number to agree BEFORE deploying.",
        planned,
        workspace_name,
    )
    return EXIT_OK


def _run_all(target: Target, pairs: list[tuple[str, Item, Item | None]]) -> list[str]:
    """Deploy every pair model-first, collecting failures rather than aborting on the first."""
    failures: list[str] = []
    for name, model, report in pairs:
        model_id, failure = _deploy_model(target, name, model)
        if failure:
            failures.append(failure)
            continue
        if report:
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


def _acquire(bundle: Path, options: argparse.Namespace) -> tuple[RunLock, Journal] | None:
    """Take the run lock and open the journal, or report why we must not start."""
    journal_path = options.journal or (bundle / "deploy-journal.jsonl")
    lock = RunLock(journal_path.with_suffix(".lock"))
    acquired, message = lock.acquire(force=options.force_unlock)
    if not acquired:
        LOG.error("%s", message)
        return None
    return lock, Journal(journal_path)


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
        return _print_plan(pairs, workspace_name, planned)

    held = _acquire(bundle, options)
    if held is None:
        return EXIT_PREFLIGHT
    lock, journal = held
    try:
        failures = _run_all(Target(workspace, workspace_name, tok, journal), pairs)
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return deploy(args.bundle, args.workspace, token(args.tenant), args)


if __name__ == "__main__":
    raise SystemExit(main())
