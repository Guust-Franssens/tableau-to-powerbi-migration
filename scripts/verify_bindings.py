"""
purpose: prove every Report in a Fabric workspace binds to a Semantic Model IN THAT WORKSPACE, by
         POLLING getDefinition instead of believing the 202 envelope it answers with.
usage:   python scripts/verify_bindings.py --workspace <id>
         python scripts/verify_bindings.py --workspace <id> --tenant <id> --json bindings.json
         python scripts/verify_bindings.py --workspace <id> --subscription <name> --timeout 300

Exit codes: 0 = every report resolved, 1 = at least one finding, 2 = the check could not be
performed at all (workspace unreadable, or nothing to check).

Why this script exists
----------------------
The operator runbook's check 8 - "every report resolves its model" - is written as *open each report
in the portal*. With 36 reports nobody performs that, so an operator reaches for the API, where the
obvious call fabricates a convincing false defect:

    POST /v1/workspaces/{ws}/items/{id}/getDefinition  ->  202 Accepted, EMPTY BODY

Parsing that body yields `byPath=False semanticModelId=NONE` for every report, which reads exactly
like an estate bound to nothing. It is not. It is the *"202 tells you nothing"* trap that
`deploy_estate.py`'s docstring documents for **create**, applying just as hard to **getDefinition**:
the answer only exists after polling `Location` to a terminal state and reading `/result`. Measured
2026-08-13, that trap caught two independent operators on the same day and one of them briefly
reported it as a critical finding. That is a property of the tooling, not of the operators - hence a
script rather than a note in a document.

Verified against a real deployed estate on 2026-08-13: 74 items, 36 reports, 38 models, **36/36
resolving byConnection to a model in the same workspace**, in 124s.

Three distinctions this makes that the naive call cannot
--------------------------------------------------------
1. **A failed or timed-out READ is not a binding defect.** Both produce "no definition", and
   collapsing them into `byPath` is precisely how the false alarm is manufactured. A report whose
   getDefinition operation failed is reported as UNREADABLE, with "re-run" as the action - never as
   a report bound to nothing.
2. **`byConnection` alone is not resolution.** The guid inside the connection string must name a
   `SemanticModel` **in this workspace**; a report carrying a guid from somewhere else looks
   perfectly healthy field-by-field. That is checked, and a guid that is not here is reported as
   such rather than silently counted.
3. **A vacuous pass is a failure.** A workspace with zero reports exits 2, not 0: a check that
   proves nothing must not look like a check that passed.

What it does NOT prove
----------------------
That a report **renders**. Binding is a property of `definition.pbir`; whether a visual draws with
data is a different question, needing a refreshed model and a real query. Do not let a green run
here be quoted as "the reports work".

On the duplicated HTTP layer
----------------------------
`Token`/`call`/`list_all` deliberately mirror `deploy_estate.py` rather than importing it. `scripts/`
is a set of INDEPENDENT one-shot CLI tools (the reason `pyproject.toml` disables pylint's
`duplicate-code` for this folder), and a read-only verifier that imports the deployer inherits its
whole surface - journals, locks, sqlite - to make three GETs. The behaviours worth copying are the
ones each cost a real run: re-mint once on `401 TokenExpired`, follow `continuationToken` to the last
page, and never raise on an HTTP error.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
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

LOG = logging.getLogger("verify_bindings")

API = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"

MODEL_TYPE = "SemanticModel"
REPORT_TYPE = "Report"

# How long one report's getDefinition may take before it is reported UNREADABLE. Generous, because
# reporting a slow read as a defect is the failure mode this script exists to prevent.
DEFAULT_TIMEOUT = 120.0
# Look quickly first - measured, the operation is usually already done - then back off toward the
# service's own Retry-After, which for this endpoint is 20s.
FIRST_POLL = 1.0
MAX_POLL = 20.0

RESOLVED = "resolved"
BY_PATH = "byPath"
UNRESOLVED = "unresolved"
UNREADABLE = "unreadable"

# The action each finding needs. A finding that does not say what to do next sends an operator back
# to the portal, which is where this check started.
ACTIONS = {
    BY_PATH: (
        "still bound BY PATH, which is a Git-integration mechanism the service cannot resolve. "
        "Re-deploy with scripts/deploy_estate.py, which rebinds byPath -> byConnection against the "
        "model's object id after the model exists."
    ),
    UNRESOLVED: (
        "its semanticModelId is not a SemanticModel in THIS workspace. The model may live in another "
        "workspace, or have been deleted after the report was deployed. Re-deploy the model and its "
        "report together so the report is rebound to the model that is actually here."
    ),
    UNREADABLE: (
        "the definition could not be READ - this is a failed or unfinished API call, NOT evidence "
        "that the report is unbound. Re-run; if it persists, check this identity's permission on the "
        "item and the service status."
    ),
}

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_CANNOT_CHECK = 2


class CannotCheck(RuntimeError):
    """The check could not be performed at all - distinct from 'performed, and found something'.

    Kept separate from a finding on purpose: an unreadable workspace exiting 1 alongside a genuine
    unbound report invites exactly the conflation this script exists to prevent.
    """


class Token:
    """A Fabric bearer token that re-mints itself when the service says it has expired.

    Same shape as the deployer's: an estate-sized read outliving its token would otherwise turn into
    a page of `401 TokenExpired` findings that look like defects in the estate.
    """

    def __init__(self, tenant: str | None = None, subscription: str | None = None) -> None:
        self.tenant = tenant
        self.subscription = subscription
        self._value = self._mint()

    def _mint(self) -> str:
        cmd = ["az", "account", "get-access-token", "--resource", FABRIC_RESOURCE]
        if self.tenant:
            cmd += ["--tenant", self.tenant]
        elif self.subscription:
            # `az` rejects --tenant together with --subscription, so these are alternatives, not a
            # pair - which is why the CLI makes them mutually exclusive.
            cmd += ["--subscription", self.subscription]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise CannotCheck(
                "Could not get a Fabric token from the Azure CLI. Run `az login`"
                + (f" --tenant {self.tenant}" if self.tenant else "")
                + ".\n"
                + "  If your signed-in identity is HOMED in another tenant, --tenant cannot mint for "
                + "the target\n  (measured: AADSTS90072). Either sign in as an identity in that "
                + "tenant, or name one of its\n  subscriptions with --subscription <name-or-id>, "
                + "which `az account list --all` will show.\n"
                + f"  {detail.strip()[:300]}"
            ) from exc
        return json.loads(out.stdout)["accessToken"]

    def refresh(self) -> str:
        """Mint a new token. Called when the service reports the current one expired."""
        LOG.info("access token expired - renewing and continuing")
        self._value = self._mint()
        return self._value

    @property
    def tenant_id(self) -> str:
        """The tenant the token is actually FOR, which is not always the one you meant."""
        return token_tenant(self._value)

    def __str__(self) -> str:
        return self._value


def token_tenant(bearer: str) -> str:
    """Read `tid` out of a JWT's payload segment. Returns "" if it cannot be read.

    Decode only - no signature check, and nothing but `tid` is ever looked at or logged. This exists
    for one measured failure: `az account get-access-token` can succeed with a token for the WRONG
    tenant, and the API then answers `WorkspaceNotFound` for a workspace that plainly exists. That
    presents as "the deploy went somewhere else" and cost 15 minutes on a real run. The tenant a
    token is for is not a secret; the token is, and never leaves this function.
    """
    parts = bearer.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
    except (binascii.Error, ValueError):
        return ""
    tid = claims.get("tid", "")
    return tid if isinstance(tid, str) else ""


def call(method: str, url: str, tok: Any, body: dict | None = None) -> tuple[int, dict, dict]:
    """One REST call. Returns (status, headers, parsed-body); never raises on an HTTP error.

    Retries ONCE on `401 TokenExpired` with a freshly minted token; any other 401 is a real
    authorization problem and is returned as-is rather than ground against.
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


def list_all(workspace: str, tok: Any, collection: str = "items") -> tuple[int, list[dict[str, Any]]]:
    """Read EVERY page of a Fabric list endpoint. Returns (status, rows).

    Fabric pages a collection once it outgrows one response, and a report sitting past the boundary
    is simply absent from a first-page-only read - so the check would report a smaller estate than
    exists and still exit 0. A landing zone holds ~2 items per workbook, so a customer estate reaches
    the boundary long before a 36-report test does.
    """
    url = f"{API}/workspaces/{workspace}/{collection}"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    while True:
        status, _, body = call("GET", url, tok)
        if status != 200:
            return status, rows
        rows.extend(body.get("value", []))
        next_page = body.get("continuationToken")
        # A service that keeps handing back the same token would otherwise loop forever.
        if not next_page or next_page in seen:
            return status, rows
        seen.add(next_page)
        url = (
            body.get("continuationUri")
            or f"{API}/workspaces/{workspace}/{collection}?continuationToken={quote(next_page)}"
        )


def _now() -> float:
    """Monotonic clock, isolated so tests can drive the poll loop without waiting."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Wait between polls, isolated so tests can drive the poll loop without waiting."""
    time.sleep(seconds)


def retry_after(headers: dict, default: float) -> float:
    """Seconds to wait, from a `Retry-After` header that may be a delay OR an HTTP-date."""
    raw = str(headers.get("retry-after", "")).strip()
    if not raw:
        return default
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


def await_operation(op_url: str, tok: Any, timeout: float = DEFAULT_TIMEOUT, hint: float = 0.0) -> tuple[str, dict]:
    """Poll a long-running operation to a terminal state. Returns (state, body).

    States are the service's own - `Succeeded`, `Failed`, `Undetermined` - plus two of ours:
    `Timeout` when the deadline passes with the operation still running, and `Unreadable` when the
    poll itself cannot be read. All three non-success states are kept DISTINCT from any answer about
    the report, because "I could not find out" and "it is broken" are different sentences.

    **The FIRST look is quick even when the service asks for a long wait.** Measured against a real
    workspace: `getDefinition` answers `202` with `Retry-After: 20` for an operation whose own
    `createdTimeUtc`/`lastUpdatedTimeUtc` are 0.3s apart. Obeying that literally cost 20s per report
    and turned a 36-report check into a 12-minute one - and a check nobody is willing to run is
    exactly what this script exists to replace. Every look AFTER the first honours the hint, which is
    what it is really for: a service that is genuinely busy.
    """
    deadline = _now() + timeout
    wait = min(hint, FIRST_POLL) if hint else FIRST_POLL
    while True:
        _sleep(wait)
        status, headers, body = call("GET", op_url, tok)
        hint = retry_after(headers, hint)
        if status in (0, 429) or status >= 500:
            # Transient by nature: back off and look again until the deadline says otherwise.
            wait = min(max(hint, wait * 2, 1.0), MAX_POLL)
        elif status != 200:
            return "Unreadable", {"error": {"message": f"HTTP {status} polling the operation"}}
        else:
            state = str(body.get("status") or body.get("Status") or "").strip()
            if state in ("Succeeded", "Failed", "Undetermined"):
                return state, body
            wait = min(max(hint, wait * 2, FIRST_POLL), MAX_POLL)
        if _now() >= deadline:
            return "Timeout", body


@dataclass
class Definition:
    """The outcome of asking for one item's definition: parts, or WHY there are none."""

    parts: list[dict[str, str]] = field(default_factory=list)
    error: str = ""


def fetch_definition(workspace: str, item_id: str, tok: Any, timeout: float = DEFAULT_TIMEOUT) -> Definition:
    """GET an item's real definition. **Never reads the 202 envelope** - that is the whole bug.

    `getDefinition` is a long-running operation: the POST answers `202 Accepted` with an EMPTY body
    and a `Location` header. Treating that body as the definition yields "no parts" for every item,
    which downstream reads as "bound to nothing" - a clean-looking, entirely fabricated defect. The
    truth is at `Location` -> `/result`, once the operation reports `Succeeded`.
    """
    status, headers, body = call("POST", f"{API}/workspaces/{workspace}/items/{item_id}/getDefinition", tok)
    if status == 200:
        # Some responses complete inline; that body IS the definition.
        return Definition(list(body.get("definition", {}).get("parts", [])))
    if status != 202:
        return Definition(error=f"HTTP {status} from getDefinition: {_detail(body)}")

    operation = headers.get("x-ms-operation-id", "")
    op_url = headers.get("location") or (f"{API}/operations/{operation}" if operation else "")
    if not op_url:
        return Definition(error="202 Accepted with no Location and no operation id - nothing to poll")

    state, op_body = await_operation(op_url, tok, timeout=timeout, hint=retry_after(headers, 0.0))
    if state != "Succeeded":
        return Definition(error=f"getDefinition operation {state}: {_detail(op_body)}")

    status, _, result = call("GET", f"{op_url.rstrip('/')}/result", tok)
    if status != 200:
        return Definition(error=f"operation Succeeded but its result was HTTP {status}: {_detail(result)}")
    return Definition(list(result.get("definition", {}).get("parts", [])))


def _detail(body: dict) -> str:
    """A short, safe rendering of an error body for an operator-facing line."""
    if not body:
        return "no detail returned"
    err = body.get("error") if isinstance(body.get("error"), dict) else None
    if err:
        return str(err.get("message") or err.get("errorCode") or json.dumps(err))[:200]
    return json.dumps(body)[:200]


def pbir_of(parts: list[dict[str, str]]) -> dict[str, Any] | None:
    """Decode `definition.pbir` out of a definition's parts, or None if it is absent/undecodable."""
    part = next((p for p in parts if p.get("path") == "definition.pbir"), None)
    if not part:
        return None
    try:
        text = base64.b64decode(part.get("payload", "")).decode("utf-8", "replace")
        loaded = json.loads(text)
    except (binascii.Error, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def semantic_model_id(connection_string: str) -> str:
    """Pull `semanticModelId` out of a byConnection connection string. "" when it is absent.

    The guid travels INSIDE the connection string because PBIR schema 2.0.0 allows exactly one
    property there (see `deploy_estate.py`). Keys are case-insensitive and the product quotes some
    values, so both are handled rather than assumed away.
    """
    for segment in connection_string.split(";"):
        key, sep, value = segment.partition("=")
        if sep and key.strip().lower() == "semanticmodelid":
            return value.strip().strip('"').strip()
    return ""


@dataclass
class Check:
    """One report's verdict, in the form a summary line and a JSON artifact both need."""

    name: str
    item_id: str
    status: str
    detail: str = ""
    model_id: str = ""
    model_name: str = ""

    @property
    def ok(self) -> bool:
        """True only for a report proven to resolve a model in this workspace."""
        return self.status == RESOLVED


def classify(item: dict[str, Any], definition: Definition, models: dict[str, str]) -> Check:
    """Turn one report's definition into a verdict. `models` maps lowercased guid -> display name."""
    name = item.get("displayName", "?")
    item_id = item.get("id", "?")
    if definition.error:
        return Check(name, item_id, UNREADABLE, definition.error)

    pbir = pbir_of(definition.parts)
    if pbir is None:
        return Check(name, item_id, UNREADABLE, f"no readable definition.pbir among {len(definition.parts)} part(s)")

    reference = pbir.get("datasetReference")
    if not isinstance(reference, dict):
        return Check(name, item_id, UNREADABLE, "definition.pbir has no datasetReference object")
    status, detail, model_id, model_name = _verdict(reference, models)
    return Check(name, item_id, status, detail, model_id, model_name)


def _verdict(reference: dict[str, Any], models: dict[str, str]) -> tuple[str, str, str, str]:
    """Judge one `datasetReference`. Returns (status, detail, model id, model name).

    `models` is the workspace's own semantic models, which is what makes this a RESOLUTION check
    rather than a shape check: `byConnection` with a guid from somewhere else is well-formed and
    still does not resolve here.
    """
    if "byPath" in reference:
        return BY_PATH, f"byPath -> {(reference.get('byPath') or {}).get('path', '?')}", "", ""
    connection = (reference.get("byConnection") or {}).get("connectionString", "")
    if not connection:
        return UNRESOLVED, f"neither byPath nor byConnection: {sorted(reference)}", "", ""
    guid = semantic_model_id(connection)
    if not guid:
        return UNRESOLVED, "byConnection carries no semanticModelId", "", ""
    model_name = models.get(guid.lower(), "")
    if not model_name:
        return UNRESOLVED, f"semanticModelId {guid} is not a {MODEL_TYPE} in this workspace", guid, ""
    return RESOLVED, "", guid, model_name


def read_workspace(workspace: str, tok: Any) -> tuple[str, list[dict[str, Any]]]:
    """Read a workspace's name and every item in it, or raise `CannotCheck` with a usable diagnosis."""
    status, _, body = call("GET", f"{API}/workspaces/{workspace}", tok)
    if status == 404:
        raise CannotCheck(_wrong_tenant_hint(workspace, tok))
    if status == 403:
        raise CannotCheck(f"No access to workspace {workspace} - this identity needs at least the Viewer role.")
    if status != 200:
        raise CannotCheck(f"Could not read workspace {workspace}: HTTP {status} {_detail(body)}")

    name = body.get("displayName", "?")
    status, rows = list_all(workspace, tok)
    if status != 200:
        raise CannotCheck(f"Could not list items in {name!r}: HTTP {status} - is this identity a Viewer here?")
    return name, rows


def _wrong_tenant_hint(workspace: str, tok: Any) -> str:
    """A 404 is usually a real absence and sometimes the wrong tenant - say both, name the tenant.

    `az account get-access-token` happily returns a token for whatever tenant the CLI is pointed at,
    and Fabric then answers `WorkspaceNotFound` for a workspace that exists. Without this line the
    symptom reads as "the deploy landed somewhere else", which is a 15-minute detour.
    """
    tid = getattr(tok, "tenant_id", "")
    lines = [f"Workspace {workspace} not found (or this identity cannot see it)."]
    if tid:
        lines.append(f"  This token is for tenant {tid}.")
        lines.append("  If the workspace lives in another tenant, re-run with --tenant <that tenant id>")
        lines.append("  (`az account get-access-token` can succeed with a token for the wrong tenant,")
        lines.append("   and the API then reports a workspace that plainly exists as missing).")
        lines.append("  If your signed-in identity is HOMED elsewhere, --tenant cannot mint for the target;")
        lines.append("  use --subscription <one in that tenant>, from `az account list --all`.")
    else:
        lines.append("  Could not read the token's tenant; check `az account show` points at the right one.")
    return "\n".join(lines)


def verify(workspace: str, tok: Any, timeout: float = DEFAULT_TIMEOUT) -> tuple[list[Check], dict[str, Any]]:
    """Check every report in the workspace. Returns (checks, summary-facts)."""
    name, items = read_workspace(workspace, tok)
    models = {i["id"].lower(): i.get("displayName", "?") for i in items if i.get("type") == MODEL_TYPE and i.get("id")}
    reports = [i for i in items if i.get("type") == REPORT_TYPE]
    LOG.info("%r: %d item(s), %d report(s), %d model(s)", name, len(items), len(reports), len(models))

    started = _now()
    checks: list[Check] = []
    for index, report in enumerate(reports, start=1):
        definition = fetch_definition(workspace, report.get("id", ""), tok, timeout=timeout)
        check = classify(report, definition, models)
        checks.append(check)
        LOG.info("  [%d/%d] %-8s %s", index, len(reports), check.status, check.name)
    facts = {
        "workspace": workspace,
        "workspace_name": name,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": len(items),
        "reports": len(reports),
        "models": len(models),
        "elapsed_seconds": round(_now() - started, 1),
    }
    return checks, facts


def print_summary(checks: list[Check], facts: dict[str, Any]) -> int:
    """Print the summary and the findings. Returns the process exit code."""
    total = len(checks)
    resolved = [c for c in checks if c.ok]
    LOG.info("")
    LOG.info("byConnection AND resolving to a model in this workspace: %d/%d", len(resolved), total)
    LOG.info("checked in %.0fs", facts.get("elapsed_seconds", 0.0))

    findings = [c for c in checks if not c.ok]
    if not findings:
        if total == 0:
            LOG.error("")
            LOG.error("NO REPORTS in this workspace - this check proves nothing.")
            LOG.error("Verify the workspace id, or run it after deploy_estate.py has landed the estate.")
            return EXIT_CANNOT_CHECK
        LOG.info("")
        LOG.info("PASS: every report resolves a model in this workspace.")
        LOG.info("NOTE: this proves the reports BIND. It does not prove any visual RENDERS with data.")
        return EXIT_OK

    LOG.error("")
    LOG.error("FINDINGS (%d of %d):", len(findings), total)
    for check in findings:
        LOG.error("  [%s] %s (id %s)", check.status, check.name, check.item_id)
        LOG.error("      %s", check.detail or "no detail")
        LOG.error("      -> %s", ACTIONS[check.status])
    if any(c.status == UNREADABLE for c in findings):
        LOG.error("")
        LOG.error("At least one report could not be READ. That is not evidence of a broken binding;")
        LOG.error("re-run before reporting it as a defect.")
    return EXIT_FINDINGS


def write_json(path: Path, checks: list[Check], facts: dict[str, Any]) -> None:
    """Write the machine-readable evidence an operator can attach to a runbook run."""
    payload = {
        **facts,
        "resolved": sum(1 for c in checks if c.ok),
        "results": [
            {
                "name": c.name,
                "id": c.item_id,
                "status": c.status,
                "detail": c.detail,
                "semantic_model_id": c.model_id,
                "semantic_model_name": c.model_name,
            }
            for c in checks
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOG.info("wrote %s", path)


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface."""
    parser = argparse.ArgumentParser(
        description=(
            "Verify every Report in a Fabric workspace binds to a Semantic Model in that workspace. "
            "Read-only: it lists items and reads definitions, and writes nothing to the service."
        )
    )
    parser.add_argument("--workspace", required=True, help="workspace id to verify (read-only)")
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--tenant", help="Entra tenant id; omit to use the Azure CLI default")
    identity.add_argument(
        "--subscription",
        help="mint the token via this subscription instead - the way in when your default `az` "
        "identity is homed in another tenant, where --tenant cannot help",
    )
    parser.add_argument("--json", type=Path, help="also write the per-report results to this file")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"seconds to poll one report's getDefinition before calling it UNREADABLE (default {DEFAULT_TIMEOUT:.0f})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the exit code documented in the module docstring."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    try:
        tok = Token(args.tenant, args.subscription)
        if tok.tenant_id:
            LOG.info("token tenant: %s", tok.tenant_id)
        if args.tenant and tok.tenant_id and tok.tenant_id.lower() != args.tenant.lower():
            raise CannotCheck(
                f"Asked for tenant {args.tenant} but the token is for tenant {tok.tenant_id}.\n"
                f"  Run `az login --tenant {args.tenant}` and try again."
            )
        checks, facts = verify(args.workspace, tok, timeout=args.timeout)
    except CannotCheck as exc:
        LOG.error("%s", exc)
        return EXIT_CANNOT_CHECK

    code = print_summary(checks, facts)
    if args.json:
        write_json(args.json, checks, facts)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
