"""An in-process fake of the Fabric REST API, faithful where we have measured it.

Why
---
Verifying ``scripts/deploy_estate.py`` against a real tenant costs 10-25 minutes per run and mutates
a real workspace. This reproduces the service well enough to rehearse and regression-test offline.

The seam
--------
``deploy_estate`` reaches the network through exactly two module-level functions::

    call(method, url, tok, body=None) -> (status, headers, body)   # adds the TokenExpired retry
    _request(method, url, bearer, body=None) -> (status, headers, body)   # the bare HTTP call

Substitute **``_request``**, not ``call`` (:func:`install`). Patching ``call`` would replace the
token-renewal retry with the mock's own answer, so the one piece of client policy that a long deploy
depends on would stop being exercised. ``FabricService.call`` exists anyway for callers that want the
documented seam, and simply delegates to ``FabricService.request``.

Fidelity discipline
-------------------
A mock kinder than the service manufactures false confidence. So:

* Behaviour recorded here as MEASURED was observed against a real tenant (the evidence for each is
  cited on the code that implements it, and collected in ``docs/offline-mock-harness.md``).
* Everything else is implemented as the **strictest plausible** behaviour and listed in the
  "ASSUMED, NOT MEASURED" section of that document. Error *codes* for assumed rejections are
  plausible names, not observed ones - assert on status and behaviour, not on an assumed code.

The single most important measured behaviour is that a repeated ``Report``/``SemanticModel``
``displayName`` is **accepted**, creating a duplicate. Nothing downstream catches it, which is why
the deployer's journal, run-lock and "ask the service first" rules are load-bearing.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

API = "https://api.fabric.microsoft.com/v1"

# MEASURED: a workspace holds at most 1000 items (folders do not count).
WORKSPACE_ITEM_LIMIT = 1000

# MEASURED: nesting deeper than 10 levels is refused with `FolderDepthOutOfRange`.
MAX_FOLDER_DEPTH = 10

# MEASURED against a live workspace: these are REJECTED in a folder display name (the API validates
# rather than coercing), and `.` is rejected ANYWHERE in the name, not merely at the end.
FOLDER_REJECTED_CHARS = frozenset('&/\\:?*"|<#%.')
# ASSUMED (strictest plausible): `>` was never tried, and a name that rejects `<` almost certainly
# rejects its partner. Rejecting it here can only make the harness harsher than the service.
FOLDER_ASSUMED_REJECTED_CHARS = frozenset(">")

# The item types this harness knows how to validate. Both are Power BI items, so a landing zone needs
# no F capacity - only an appropriately licensed identity.
MODEL_TYPE = "SemanticModel"
REPORT_TYPE = "Report"
KNOWN_ITEM_TYPES = frozenset({MODEL_TYPE, REPORT_TYPE, "Notebook", "Lakehouse", "DataPipeline", "Warehouse"})

# PBIR schema 2.0.0 declares `additionalProperties: false` on `byConnection` and allows exactly this
# one property. MEASURED: sending the widely-quoted five-field 1.0.0 shape is rejected with
# `Workload_FailedToParseFile` naming each extra property.
BYCONNECTION_ALLOWED = frozenset({"connectionString"})

_SEMANTIC_MODEL_ID_RE = re.compile(r"semanticModelId=([0-9a-fA-F-]{6,})")


class MockFabricError(AssertionError):
    """Raised for harness misuse (a bad URL, an unroutable request) - never for a service verdict."""


@dataclass
class Item:
    """One item in the fake workspace."""

    id: str
    display_name: str
    item_type: str
    workspace_id: str = ""
    description: str = ""
    folder_id: str | None = None
    parts: list[dict[str, str]] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        """The shape a list/get returns.

        MEASURED: ``folderId`` is **omitted entirely** for an item at the workspace root - it is not
        present-and-null. Client code that reads ``row["folderId"]`` rather than ``row.get(...)``
        breaks only against the real service, so the mock must omit it too.
        """
        row: dict[str, Any] = {
            "id": self.id,
            "displayName": self.display_name,
            "type": self.item_type,
            "workspaceId": self.workspace_id,
            "description": self.description,
        }
        if self.folder_id:
            row["folderId"] = self.folder_id
        return row


@dataclass
class Folder:
    """One folder in the fake workspace."""

    id: str
    display_name: str
    parent_folder_id: str | None = None

    def row(self) -> dict[str, Any]:
        """MEASURED: rows carry ``id``, ``displayName``, ``parentFolderId``.

        ASSUMED: ``parentFolderId`` is omitted at the root, mirroring the item behaviour above
        (that half IS measured). Omitting is the stricter of the two shapes.
        """
        row: dict[str, Any] = {"id": self.id, "displayName": self.display_name}
        if self.parent_folder_id:
            row["parentFolderId"] = self.parent_folder_id
        return row


@dataclass
class Operation:
    """A long-running operation, as returned by a ``202 Accepted`` create/update."""

    id: str
    status: str = "Running"
    item_id: str | None = None
    error: dict[str, Any] | None = None
    polls_before_terminal: int = 1
    polls: int = 0

    def poll(self) -> dict[str, Any]:
        """Advance and report. A `Running` operation is indistinguishable from a failed one to a
        client that never reads `status` - which is exactly the measured defect this models."""
        self.polls += 1
        if self.status == "Running" and self.polls_before_terminal >= 0 and self.polls >= self.polls_before_terminal:
            self.status = "Succeeded" if self.error is None else "Failed"
        body: dict[str, Any] = {"id": self.id, "status": self.status, "percentComplete": 100}
        if self.status == "Failed" and self.error:
            body["error"] = self.error
        return body


@dataclass
class Rule:
    """One injected failure. Consumed ``times`` times, then it stops matching."""

    status: int
    method: str | None = None
    contains: str | None = None
    body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    times: int = 1
    network: bool = False

    def matches(self, method: str, url: str) -> bool:
        """Whether this rule claims the request."""
        if self.times <= 0:
            return False
        if self.method and self.method.upper() != method.upper():
            return False
        return not (self.contains and self.contains not in url)


def _error(status: int, code: str, message: str) -> tuple[int, dict, dict]:
    """A Fabric error response.

    The shape matters: ``deploy_estate`` reads ``resp["errorCode"]`` first and
    ``resp["error"]["errorCode"]`` second, and its token-renewal test is a substring match on the
    whole serialized body.
    """
    body = {
        "requestId": str(uuid.uuid4()),
        "errorCode": code,
        "message": message,
        "error": {"errorCode": code, "message": message},
    }
    return status, {"x-ms-public-api-error-code": code}, body


class FabricService:
    """An in-memory Fabric tenant: workspaces, items, folders, operations - and injectable failures.

    Usage::

        service = FabricService()
        service.install(monkeypatch, deploy_estate)
        deploy_estate.deploy(bundle, service.workspace_id, service.issue_token(), options)
        assert service.item_names(MODEL_TYPE) == ["Sales"]
    """

    def __init__(
        self,
        *,
        workspace_id: str = "ws-0000",
        workspace_name: str = "Landing Zone",
        page_size: int = 100,
        async_create: bool = False,
        item_limit: int = WORKSPACE_ITEM_LIMIT,
    ) -> None:
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        # MEASURED: 68 items came back in ONE page with no continuation token, so paging is only
        # reachable deliberately. Small page sizes in a test are how it gets exercised at all.
        self.page_size = page_size
        self.async_create = async_create
        self.item_limit = item_limit

        self.items: dict[str, Item] = {}
        self.folders: dict[str, Folder] = {}
        self.operations: dict[str, Operation] = {}
        self.forbidden_workspaces: set[str] = set()

        self._tokens: set[str] = set()
        self._expired: set[str] = set()
        self._rules: list[Rule] = []
        self._stalled_operations = 0
        self._failed_operations: list[dict[str, Any]] = []
        self._seq = 0
        self.requests: list[tuple[str, str, dict | None]] = []

    # ----------------------------------------------------------------- tokens

    def issue_token(self) -> str:
        """Mint a token this service will accept."""
        self._seq += 1
        value = f"token-{self._seq}"
        self._tokens.add(value)
        return value

    def expire_tokens(self) -> None:
        """Expire every token issued so far.

        MEASURED: a real deploy of 66 items outlived its token and every remaining call answered
        ``401 TokenExpired``, leaving a half-deployed workspace. ``deploy_estate.call`` renews once
        and retries - which is only exercised if the mock is installed at ``_request``.
        """
        self._expired |= self._tokens
        self._tokens = set()

    # ------------------------------------------------------- failure injection

    def fail_next(
        self,
        status: int,
        *,
        method: str | None = None,
        contains: str | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        times: int = 1,
    ) -> Rule:
        """Make the next matching request fail. Returns the rule so a test can inspect it."""
        rule = Rule(status=status, method=method, contains=contains, body=body, headers=headers, times=times)
        self._rules.append(rule)
        return rule

    def throttle(self, *, retry_after: str | int = 1, contains: str | None = None, times: int = 1) -> Rule:
        """Answer 429 with a ``Retry-After`` header.

        MEASURED both forms: an integer number of seconds, and the RFC 9110 HTTP-date. The date form
        raised ``ValueError`` in ``float()`` and killed a deploy mid-estate, so pass a date string
        here to reproduce that exact input.
        """
        rule = self.fail_next(
            429,
            contains=contains,
            times=times,
            headers={"retry-after": str(retry_after)},
            body={"errorCode": "TooManyRequests", "message": "Too many requests"},
        )
        return rule

    def drop_network(self, *, contains: str | None = None, times: int = 1) -> Rule:
        """Simulate a client-side network failure (DNS/route), not a service verdict.

        ``_request`` turns ``URLError`` into ``(0, {}, {"error": {"message": ...}})`` and the
        deployer treats a detail containing ``HTTP 0`` as "we could not reach the host", stopping
        after three consecutive ones instead of marking the rest of the estate failed.
        """
        rule = Rule(status=0, contains=contains, times=times, network=True)
        self._rules.append(rule)
        return rule

    def add_rule(self, rule: Rule) -> Rule:
        """Register a pre-built rule (for a caller that needs more than ``fail_next`` expresses)."""
        self._rules.append(rule)
        return rule

    def stall_operations(self, times: int = 1) -> None:
        """Make the next ``times`` long-running operations never reach a terminal state.

        MEASURED shape: our poll gives up and records ``Timeout`` while the operation completes
        server-side, so the item IS there on the next listing. Reproducing it is what proves a
        resume adopts the item instead of creating a second copy.

        ASSUMED: **when** the item becomes visible. Here it is visible immediately, because that is
        the case a client can actually get right; a client that cannot see it has no way to avoid
        the duplicate, so testing against the invisible variant would only assert a known hazard.
        """
        self._stalled_operations += times

    def fail_next_operation(self, code: str = "PowerBIItemCreateFailed", message: str = "operation failed") -> None:
        """Make the next long-running operation reach ``Failed``, creating nothing.

        MEASURED: a first probe reported the model deployed and the report fine; the report had in
        fact FAILED and the workspace held one item. A client that never polls ``status`` cannot
        tell the two apart, so this is the case that justifies polling at all.
        """
        self._failed_operations.append({"errorCode": code, "message": message})

    # --------------------------------------------------------------- installation

    def install(self, monkeypatch, module) -> None:
        """Point a ``deploy_estate``-shaped module at this service.

        Patches ``_request`` deliberately: everything ``call`` adds (today, the one-shot renewal on
        ``401 TokenExpired``) stays under test. Also neutralises ``time.sleep`` so a 429 wait or an
        operation poll costs nothing - the deployer's own back-off arithmetic still runs.
        """
        monkeypatch.setattr(module, "_request", self.request)
        monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    def token_for(self, module) -> Any:
        """A real ``deploy_estate.Token`` bound to this service instead of the Azure CLI.

        ``call``'s renewal path is guarded by ``isinstance(tok, Token)``, so a duck-typed stand-in
        would silently skip the very behaviour a token-expiry test is about.
        """
        token = module.Token.__new__(module.Token)
        token.tenant = None
        token._value = self.issue_token()  # noqa: SLF001  # pylint: disable=protected-access
        token._mint = self.issue_token  # noqa: SLF001  # pylint: disable=protected-access
        return token

    # --------------------------------------------------------------- inspection

    def item_names(self, item_type: str | None = None) -> list[str]:
        """Display names currently in the workspace, sorted - duplicates included, deliberately."""
        return sorted(i.display_name for i in self.items.values() if item_type in (None, i.item_type))

    def duplicates(self) -> list[tuple[str, str]]:
        """Every ``(displayName, type)`` present more than once. The mock does NOT prevent these."""
        seen: dict[tuple[str, str], int] = {}
        for item in self.items.values():
            seen[(item.display_name, item.item_type)] = seen.get((item.display_name, item.item_type), 0) + 1
        return sorted(key for key, count in seen.items() if count > 1)

    def folder_paths(self) -> dict[str, str]:
        """``folder id -> "A/B/C"`` for every folder, so a test can assert the mirrored tree."""
        out = {}
        for folder in self.folders.values():
            parts, node = [], folder
            while node is not None:
                parts.append(node.display_name)
                node = self.folders.get(node.parent_folder_id or "")
            out[folder.id] = "/".join(reversed(parts))
        return out

    def item_folder_paths(self) -> dict[str, str]:
        """``displayName (type) -> folder path`` (``""`` for the workspace root)."""
        paths = self.folder_paths()
        return {
            f"{item.display_name} ({item.item_type})": paths.get(item.folder_id or "", "")
            for item in self.items.values()
        }

    def part(self, item_id: str, path: str) -> bytes | None:
        """The decoded bytes of one definition part of a stored item."""
        for candidate in self.items[item_id].parts:
            if candidate["path"] == path:
                return base64.b64decode(candidate["payload"])
        return None

    def model_binding(self, report_name: str) -> str | None:
        """The ``semanticModelId`` a stored report is bound to, read back out of its PBIR."""
        for item in self.items.values():
            if item.display_name == report_name and item.item_type == REPORT_TYPE:
                raw = self.part(item.id, "definition.pbir")
                if raw is None:
                    return None
                reference = json.loads(raw).get("datasetReference") or {}
                connection = (reference.get("byConnection") or {}).get("connectionString", "")
                found = _SEMANTIC_MODEL_ID_RE.search(connection)
                return found.group(1) if found else None
        return None

    # ------------------------------------------------------------------ the seam

    def call(self, method: str, url: str, tok: Any, body: dict | None = None) -> tuple[int, dict, dict]:
        """The documented ``deploy_estate.call`` seam. Prefer :meth:`install`, which patches lower."""
        return self.request(method, url, str(tok), body)

    def request(self, method: str, url: str, bearer: str, body: dict | None = None) -> tuple[int, dict, dict]:
        """The ``deploy_estate._request`` seam: one HTTP call, no retry policy of its own."""
        self.requests.append((method.upper(), url, body))

        for rule in self._rules:
            if rule.matches(method, url):
                rule.times -= 1
                if rule.network:
                    return 0, {}, {"error": {"message": "<urlopen error [Errno 11001] getaddrinfo failed>"}}
                return rule.status, dict(rule.headers or {}), dict(rule.body or {"error": {"message": "injected"}})

        if bearer in self._expired:
            # MEASURED: the expiry answer names itself, which is what lets a client tell it apart
            # from a genuine authorization failure and renew instead of aborting.
            return _error(401, "TokenExpired", "Access token has expired, resubmit with a new access token")
        if bearer not in self._tokens:
            return _error(401, "Unauthorized", "The request lacks valid authentication credentials")

        return self._route(method.upper(), url, body or {})

    # --------------------------------------------------------------- routing

    def _route(self, method: str, url: str, body: dict) -> tuple[int, dict, dict]:
        parsed = urlparse(url)
        path = parsed.path
        if not path.startswith("/v1/"):
            raise MockFabricError(f"not a Fabric v1 URL: {url}")
        segments = path[len("/v1/") :].strip("/").split("/")

        if segments[0] == "operations":
            return self._operations(method, segments[1:])
        if segments[0] != "workspaces" or len(segments) < 2:
            raise MockFabricError(f"unroutable Fabric URL: {url}")

        workspace = segments[1]
        if workspace in self.forbidden_workspaces:
            return _error(403, "InsufficientPrivileges", "The caller does not have the required permissions")
        if workspace != self.workspace_id:
            return _error(404, "WorkspaceNotFound", f"Workspace {workspace} not found")

        rest = segments[2:]
        if not rest:
            if method != "GET":
                return _error(405, "MethodNotAllowed", f"{method} is not allowed here")
            return 200, {}, {"id": self.workspace_id, "displayName": self.workspace_name, "type": "Workspace"}
        if rest[0] == "items":
            return self._items(method, rest[1:], body, parse_qs(parsed.query))
        if rest[0] == "folders":
            return self._folders(method, rest[1:], body, parse_qs(parsed.query))
        raise MockFabricError(f"unroutable Fabric URL: {url}")

    # ----------------------------------------------------------------- items

    def _items(self, method: str, rest: list[str], body: dict, query: dict) -> tuple[int, dict, dict]:
        if not rest:
            if method == "GET":
                return self._page([i.row() for i in self.items.values()], query, "items")
            if method == "POST":
                return self._create_item(body)
            return _error(405, "MethodNotAllowed", f"{method} is not allowed on items")

        item_id = rest[0]
        item = self.items.get(item_id)
        if item is None:
            return _error(404, "ItemNotFound", f"Item {item_id} not found")
        action = rest[1] if len(rest) > 1 else ""
        if action == "updateDefinition" and method == "POST":
            return self._update_definition(item, body)
        if action == "move" and method == "POST":
            return self._move_item(item, body)
        if not action and method == "PATCH":
            return self._patch_item(item, body)
        if not action and method == "GET":
            return 200, {}, item.row()
        raise MockFabricError(f"unroutable item action: {method} {'/'.join(rest)}")

    def _create_item(self, body: dict) -> tuple[int, dict, dict]:
        name = str(body.get("displayName") or "")
        item_type = str(body.get("type") or "")
        parts = ((body.get("definition") or {}).get("parts")) or []

        problem = self._reject_item(name, item_type, parts, body.get("folderId"))
        if problem:
            return problem
        # MEASURED: a repeated Report/SemanticModel displayName is NOT rejected - Fabric happily
        # creates a duplicate, and nothing downstream notices. This is the single behaviour most
        # worth reproducing: it is why duplicates went undetected in a real run.
        if len(self.items) >= self.item_limit:
            return _error(400, "WorkspaceItemLimitExceeded", f"A workspace holds at most {self.item_limit} items")

        item = Item(
            id=str(uuid.uuid4()),
            display_name=name,
            item_type=item_type,
            workspace_id=self.workspace_id,
            description=str(body.get("description") or ""),
            folder_id=body.get("folderId") or None,
            parts=[dict(p) for p in parts],
        )
        self.items[item.id] = item
        if not self.async_create:
            return 201, {}, item.row()

        # MEASURED: `202 Accepted` carries an EMPTY body, so a failed operation is indistinguishable
        # from a successful one until `/operations/{id}` is polled for `status`.
        return 202, *self._start_operation(item.id)

    def _reject_item(
        self, name: str, item_type: str, parts: list[dict], folder_id: str | None
    ) -> tuple[int, dict, dict] | None:
        """Every reason a create is refused, or None. Order matters only for the message."""
        if not name:
            return _error(400, "InvalidItemDisplayName", "displayName is required")
        # ASSUMED (strictest plausible): length cap and no surrounding whitespace. Deliberately NOT
        # the folder character rules - item names with dots are routine, so importing that rule here
        # would invent a failure the service does not have.
        if len(name) > 256 or name != name.strip():
            return _error(400, "InvalidItemDisplayName", f"{name!r} is not a valid item display name")
        if item_type not in KNOWN_ITEM_TYPES:  # ASSUMED
            return _error(400, "UnsupportedItemType", f"{item_type!r} is not a supported item type")
        if folder_id and folder_id not in self.folders:  # ASSUMED
            return _error(400, "InvalidFolderId", f"folder {folder_id} does not exist in this workspace")
        return self._reject_definition(item_type, parts)

    def _reject_definition(self, item_type: str, parts: list[dict]) -> tuple[int, dict, dict] | None:
        """Validate the definition payload the way the service does where we have measured it."""
        by_path = {}
        for part in parts:
            if part.get("payloadType") != "InlineBase64":  # ASSUMED
                return _error(400, "InvalidItemDefinition", "only InlineBase64 payloads are supported")
            try:
                by_path[part["path"]] = base64.b64decode(part["payload"], validate=True)
            except (KeyError, ValueError, binascii.Error):  # ASSUMED
                return _error(400, "InvalidItemDefinition", f"part {part.get('path')!r} is not valid base64")

        if item_type == MODEL_TYPE:
            if "definition.pbism" not in by_path:  # ASSUMED
                return _error(400, "InvalidItemDefinition", "a SemanticModel definition needs definition.pbism")
            return None
        if item_type != REPORT_TYPE:
            return None
        return self._reject_report(by_path)

    def _reject_report(self, by_path: dict[str, bytes]) -> tuple[int, dict, dict] | None:
        """The report rules, three of which are measured and each of which cost a real run."""
        if "definition.pbir" not in by_path:  # ASSUMED
            return _error(400, "InvalidItemDefinition", "a Report definition needs definition.pbir")
        try:
            pbir = json.loads(by_path["definition.pbir"])
        except json.JSONDecodeError:
            return _error(400, "Workload_FailedToParseFile", "definition.pbir is not valid JSON")

        # MEASURED: a report with no pages is refused, and the message is this opaque - which is why
        # `deploy_estate.report_is_empty` detects it locally instead.
        try:
            pages = json.loads(by_path.get("definition/pages/pages.json", b"{}"))
        except json.JSONDecodeError:
            pages = {}
        if not pages.get("pageOrder"):
            return _error(400, "PowerBIItemCreateFailed", "Content provider provided invalid package content stream")

        reference = pbir.get("datasetReference") or {}
        if "byPath" in reference:
            # MEASURED: byPath is a Git-integration mechanism; the service cannot resolve a path, so
            # deploying a migrated PBIP unchanged fails. (Rejection measured; error CODE assumed.)
            return _error(400, "InvalidItemDefinition", "datasetReference.byPath cannot be resolved by the service")
        connection = reference.get("byConnection")
        if not isinstance(connection, dict):
            return _error(400, "InvalidConnectionInformation", "datasetReference.byConnection is required")
        # MEASURED: PBIR schema 2.0.0 sets `additionalProperties: false` here, and the five-field
        # 1.0.0 shape is rejected with each extra property named.
        extra = sorted(set(connection) - BYCONNECTION_ALLOWED)
        if extra:
            return _error(
                400,
                "Workload_FailedToParseFile",
                "definition.pbir: property "
                + ", ".join(repr(name) for name in extra)
                + " is not allowed by the 2.0.0 schema",
            )
        found = _SEMANTIC_MODEL_ID_RE.search(str(connection.get("connectionString") or ""))
        if not found:
            # MEASURED: the model's identity has nowhere to go except inside the connection string;
            # omitting `semanticModelId=<guid>` is answered with InvalidConnectionInformation.
            return _error(400, "InvalidConnectionInformation", "connectionString carries no semanticModelId")
        target = self.items.get(found.group(1))
        if target is None or target.item_type != MODEL_TYPE:
            # ASSUMED (strictest plausible): a binding to a guid that is not a semantic model in this
            # workspace is refused rather than stored. This is what makes "bound to the duplicate" or
            # "bound to a deleted model" a test failure instead of a silent success.
            return _error(400, "InvalidConnectionInformation", f"semanticModelId {found.group(1)} does not resolve")
        return None

    def _update_definition(self, item: Item, body: dict) -> tuple[int, dict, dict]:
        parts = ((body.get("definition") or {}).get("parts")) or []
        problem = self._reject_definition(item.item_type, parts)
        if problem:
            return problem
        # MEASURED: updateDefinition replaces the definition and does NOT change description or
        # folderId. The deployer therefore has to re-stamp and re-move separately; a mock that
        # helpfully carried those across would hide both bugs.
        item.parts = [dict(p) for p in parts]
        if not self.async_create:
            return 200, {}, {}
        return 202, *self._start_operation(item.id)

    def _patch_item(self, item: Item, body: dict) -> tuple[int, dict, dict]:
        # MEASURED: PATCH with {"description": ...} updates the description.
        unknown = sorted(set(body) - {"description", "displayName"})  # ASSUMED: unknown keys refused
        if unknown:
            return _error(400, "InvalidParameter", f"unsupported propert(y/ies): {', '.join(unknown)}")
        if "displayName" in body:
            item.display_name = str(body["displayName"])
        if "description" in body:
            item.description = str(body["description"])
        return 200, {}, item.row()

    def _move_item(self, item: Item, body: dict) -> tuple[int, dict, dict]:
        # MEASURED: an empty body means the workspace root.
        target = body.get("targetFolderId")
        if target and target not in self.folders:
            return _error(400, "InvalidFolderId", f"folder {target} does not exist in this workspace")
        item.folder_id = target or None
        return 200, {}, item.row()

    # --------------------------------------------------------------- folders

    def _folders(self, method: str, rest: list[str], body: dict, query: dict) -> tuple[int, dict, dict]:
        if rest:
            raise MockFabricError(f"unroutable folder action: {method} folders/{'/'.join(rest)}")
        if method == "GET":
            return self._page([f.row() for f in self.folders.values()], query, "folders")
        if method != "POST":
            return _error(405, "MethodNotAllowed", f"{method} is not allowed on folders")

        name = str(body.get("displayName") or "")
        parent = body.get("parentFolderId") or None
        problem = self._reject_folder(name, parent)
        if problem:
            return problem
        folder = Folder(id=str(uuid.uuid4()), display_name=name, parent_folder_id=parent)
        self.folders[folder.id] = folder
        return 201, {}, folder.row()

    def _reject_folder(self, name: str, parent: str | None) -> tuple[int, dict, dict] | None:
        if parent and parent not in self.folders:
            return _error(400, "InvalidFolderId", f"parent folder {parent} does not exist")
        # MEASURED: the API REJECTS an invalid folder display name rather than coercing it, and a
        # leading or trailing space is invalid. Accepted: - _ + ( ), interior spaces, non-ASCII.
        bad = sorted(set(name) & (FOLDER_REJECTED_CHARS | FOLDER_ASSUMED_REJECTED_CHARS))
        if not name or name != name.strip() or bad:
            return _error(
                400,
                "InvalidFolderDisplayName",
                f"{name!r} is not a valid folder name" + (f" (rejected: {''.join(bad)})" if bad else ""),
            )
        depth = 1
        node = self.folders.get(parent or "")
        while node is not None:
            depth += 1
            node = self.folders.get(node.parent_folder_id or "")
        # MEASURED: deeper than 10 levels answers FolderDepthOutOfRange.
        if depth > MAX_FOLDER_DEPTH:
            return _error(400, "FolderDepthOutOfRange", f"folders nest at most {MAX_FOLDER_DEPTH} levels")
        # ASSUMED (strictest plausible): two siblings cannot share a display name.
        siblings = [f for f in self.folders.values() if (f.parent_folder_id or None) == parent]
        if any(f.display_name.casefold() == name.casefold() for f in siblings):
            return _error(400, "FolderDisplayNameAlreadyInUse", f"a sibling folder is already called {name!r}")
        return None

    # ------------------------------------------------------------ operations

    def _start_operation(self, item_id: str) -> tuple[dict, dict]:
        """Register a long-running operation for an item and return (headers, empty body)."""
        operation = Operation(id=str(uuid.uuid4()), item_id=item_id)
        if self._stalled_operations > 0:
            self._stalled_operations -= 1
            operation.polls_before_terminal = -1
        if self._failed_operations:
            operation.error = self._failed_operations.pop(0)
            # A create whose operation FAILS leaves nothing behind. Keeping the item would be the
            # kind of leniency that lets a client "succeed" on an item the service never made.
            self.items.pop(item_id, None)
            operation.item_id = None
        self.operations[operation.id] = operation
        return {"location": f"{API}/operations/{operation.id}", "x-ms-operation-id": operation.id}, {}

    def _operations(self, method: str, rest: list[str]) -> tuple[int, dict, dict]:
        if method != "GET" or not rest:
            raise MockFabricError(f"unroutable operation call: {method} operations/{'/'.join(rest)}")
        operation = self.operations.get(rest[0])
        if operation is None:
            return _error(404, "OperationNotFound", f"operation {rest[0]} not found")
        if len(rest) > 1 and rest[1] == "result":
            if operation.status != "Succeeded":
                return _error(400, "OperationNotCompleted", "the operation has not succeeded")
            return 200, {}, self.items[operation.item_id].row()
        return 200, {}, operation.poll()

    # ------------------------------------------------------------------ paging

    def _page(self, rows: list[dict], query: dict, collection: str) -> tuple[int, dict, dict]:
        """One page of a collection, with a continuation token when there is more.

        MEASURED: a real listing of 68 items came back in ONE page with no token - which is exactly
        why reading only page one survived a live run and still duplicated items on a bigger estate.
        """
        start = 0
        token = (query.get("continuationToken") or [None])[0]
        if token is not None:
            if not token.startswith("offset-"):
                return _error(400, "InvalidContinuationToken", "the continuation token is not recognised")
            start = int(token.split("-", 1)[1])
        page = rows[start : start + self.page_size]
        body: dict[str, Any] = {"value": page}
        if start + self.page_size < len(rows):
            next_token = f"offset-{start + self.page_size}"
            body["continuationToken"] = next_token
            body["continuationUri"] = (
                f"{API}/workspaces/{self.workspace_id}/{collection}?continuationToken={next_token}"
            )
        return 200, {}, body


def install_predicate(service: FabricService, predicate: Callable[[str, str], bool], response: tuple) -> None:
    """Escape hatch: fail whatever ``predicate(method, url)`` selects, with a canned response."""
    rule = Rule(status=response[0], times=10**6, headers=response[1], body=response[2])
    rule.matches = lambda method, url: predicate(method, url)  # type: ignore[method-assign]
    service.add_rule(rule)
