"""A loopback fake of the Tableau Server/Cloud REST + Metadata (GraphQL) APIs.

Why a real socket
-----------------
The front half of the pipeline reaches Tableau three different ways, and only one of them can be
patched in-process:

* ``scripts/assess_estate.py`` and ``scripts/tableau_lineage.py`` build ``urllib`` requests directly;
* the deterministic engine's ``estate_survey.py`` / ``fetch_tds.py`` run in a **subprocess**, which no
  monkeypatch can reach.

So this serves the API over ``127.0.0.1`` on an ephemeral port. That is loopback, not network: no
packet leaves the machine and no real Tableau site is contacted. Point ``TABLEAU_SERVER_URL`` at the
URL :func:`serve` yields and every one of those callers exercises its real HTTP code path.

What it serves
--------------
``POST /api/<ver>/auth/signin`` and ``/auth/signout``; the paged site collections
(``workbooks``, ``views``, ``datasources``, ``projects``, ``groups``, ``flows``, ``subscriptions``,
``dataAlerts``, ``customviews``), ``groups/<id>/users``, ``<object>/<id>/permissions``,
``workbooks/<luid>/connections``, ``workbooks|datasources/<luid>/content`` (real ``.twbx``/``.tdsx``
bytes), and ``POST /api/metadata/graphql``.

Fidelity notes (see ``docs/offline-mock-harness.md`` for the full ASSUMED list)
------------------------------------------------------------------------------
* Pagination values are emitted as **strings** (``"totalAvailable": "3"``), which is what Tableau's
  JSON actually does - a client that does arithmetic on them without ``int()`` breaks here too.
* A lost session answers **401 with error code ``401002``**, the string ``assess_estate`` looks for
  before re-authenticating. :meth:`TableauSite.expire_session` is how a test provokes it.
* Downloads carry Tableau's non-standard ``Content-Disposition: name="X.twbx"`` (no ``filename=``),
  so a client that only understands ``filename=`` must fall back - as ``fetch_tds`` does.
* The GraphQL structure answer is DERIVED FROM THE SERVED BYTES (sheets, dashboards and calculated
  fields are read out of the workbook XML), so the Metadata API cannot disagree with the file the
  same site hands out. On a real site those two can drift; here a drift would be a harness bug.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

DEFAULT_REST_VERSION = "3.21"

# The error code Tableau returns for a lost/expired session. `assess_estate.Site.get` re-signs-in on
# seeing this string anywhere in the body, so the code has to appear literally.
SESSION_LOST = "401002"


def twbx_bytes(twb: Path, *, inner_name: str | None = None) -> bytes:
    """Package a real ``.twb`` into ``.twbx`` bytes, the way Tableau hands one back.

    Real bytes are the point: the parser under test must do real work, and a hand-written stub would
    only ever exercise the shapes we already thought of.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(inner_name or twb.name, twb.read_bytes())
    return buffer.getvalue()


def tdsx_bytes(tds: Path, *, inner_name: str | None = None) -> bytes:
    """The same, for a standalone data source."""
    return twbx_bytes(tds, inner_name=inner_name)


@dataclass
class Project:
    """A Tableau project. ``parent_luid`` is what makes the tree a tree."""

    luid: str
    name: str
    parent_luid: str | None = None
    content_permissions: str = "ManagedByOwner"

    def row(self) -> dict[str, Any]:
        """REST shape. ``parentProjectId`` is absent at the top level, as on a real site."""
        row = {
            "id": self.luid,
            "name": self.name,
            "contentPermissions": self.content_permissions,
            "description": "",
        }
        if self.parent_luid:
            row["parentProjectId"] = self.parent_luid
        return row


@dataclass
class Workbook:
    """A published workbook, including the bytes the site will hand back on download."""

    luid: str
    name: str
    project: Project
    content: bytes
    extension: str = ".twbx"
    owner_luid: str = "owner-1"
    size_mb: int = 1
    created_at: str = "2026-01-02T03:04:05Z"
    updated_at: str = "2026-02-03T04:05:06Z"
    connections: list[dict[str, Any]] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        """REST shape, matching what ``assess_estate`` reads."""
        return {
            "id": self.luid,
            "name": self.name,
            "contentUrl": self.name.replace(" ", ""),
            "size": str(self.size_mb),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "project": {"id": self.project.luid, "name": self.project.name},
            "owner": {"id": self.owner_luid},
        }


@dataclass
class Datasource:
    """A published data source."""

    luid: str
    name: str
    project: Project
    content: bytes
    is_certified: bool = False
    has_extracts: bool = False
    extract_last_refresh: str | None = None
    downstream: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        """REST shape."""
        return {
            "id": self.luid,
            "name": self.name,
            "type": "sqlproxy" if self.has_extracts else "excel-direct",
            "isCertified": self.is_certified,
            "project": {"id": self.project.luid, "name": self.project.name},
            "owner": {"id": "owner-1"},
        }


@dataclass
class View:
    """A view (sheet or dashboard) inside a workbook, with its usage counter."""

    luid: str
    name: str
    workbook_luid: str
    total_view_count: int = 0
    updated_at: str = "2026-02-03T04:05:06Z"

    def row(self) -> dict[str, Any]:
        """REST shape. ``usage`` is only present when ``includeUsageStatistics=true`` was asked for."""
        return {
            "id": self.luid,
            "name": self.name,
            "contentUrl": f"{self.workbook_luid}/sheets/{self.name}",
            "workbook": {"id": self.workbook_luid},
            "updatedAt": self.updated_at,
            "usage": {"totalViewCount": str(self.total_view_count)},
        }


@dataclass
class Group:
    """A Tableau group. ``domain`` "local" is the one with no Entra counterpart."""

    luid: str
    name: str
    domain: str = "local"
    members: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        """REST shape."""
        return {"id": self.luid, "name": self.name, "domain": {"name": self.domain}}


@dataclass
class Grant:
    """One permission row, as ``granteeCapabilities`` returns it."""

    object_type: str
    object_luid: str
    grantee_type: str
    grantee_luid: str
    capability: str
    mode: str = "Allow"


class TableauSite:
    """The estate this fake serves, plus the switches a test needs to make it misbehave."""

    def __init__(
        self,
        *,
        site_id: str = "site-0000",
        content_url: str = "mock",
        rest_version: str = DEFAULT_REST_VERSION,
        page_size: int | None = None,
        single_row_as_object: bool = False,
    ) -> None:
        self.site_id = site_id
        self.content_url = content_url
        self.rest_version = rest_version
        # A page size the SERVER enforces, independent of the client's `pageSize=` request. Tableau
        # caps the request; a small cap here is how a test proves the client follows pagination.
        self.page_size = page_size
        # ASSUMED: Tableau's JSON returns a single row as an object rather than a one-element list.
        # Both clients defend against it, so the switch exists; it is off by default because we have
        # not measured it.
        self.single_row_as_object = single_row_as_object

        self.projects: list[Project] = []
        self.workbooks: list[Workbook] = []
        self.datasources: list[Datasource] = []
        self.views: list[View] = []
        self.groups: list[Group] = []
        self.flows: list[dict[str, Any]] = []
        self.subscriptions: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.custom_views: list[dict[str, Any]] = []
        self.grants: list[Grant] = []

        self.tokens: set[str] = set()
        self.pat_credentials: dict[str, str] = {"mock-pat": "mock-secret"}
        self.requests: list[tuple[str, str]] = []
        self._expired: set[str] = set()
        self._forbidden_paths: set[str] = set()

    # ------------------------------------------------------------- authoring

    def project(self, name: str, parent: Project | None = None, **kwargs) -> Project:
        """Add a project and return it."""
        made = Project(luid=str(uuid.uuid4()), name=name, parent_luid=parent.luid if parent else None, **kwargs)
        self.projects.append(made)
        return made

    def workbook(
        self, name: str, project: Project, twb: Path, *, views: int = 1, usage: int = 10, **kwargs
    ) -> Workbook:
        """Add a workbook backed by a REAL ``.twb`` fixture, packaged as ``.twbx``."""
        made = Workbook(luid=str(uuid.uuid4()), name=name, project=project, content=twbx_bytes(twb), **kwargs)
        self.workbooks.append(made)
        for index in range(views):
            self.views.append(
                View(
                    luid=str(uuid.uuid4()),
                    name=f"{name} sheet {index + 1}",
                    workbook_luid=made.luid,
                    total_view_count=usage,
                )
            )
        return made

    def datasource(self, name: str, project: Project, tds: Path, **kwargs) -> Datasource:
        """Add a published data source backed by a REAL ``.tds`` fixture, packaged as ``.tdsx``."""
        made = Datasource(luid=str(uuid.uuid4()), name=name, project=project, content=tdsx_bytes(tds), **kwargs)
        self.datasources.append(made)
        return made

    def publish_dependency(self, workbook: Workbook, datasource: Datasource) -> None:
        """Make ``workbook`` depend on a PUBLISHED ``datasource``.

        This is the shape that makes a workbook's own calc count understate its real complexity: the
        calculated fields live in the data source, so the workbook alone reports fewer than it has.
        """
        workbook.connections.append(
            {
                "id": str(uuid.uuid4()),
                "type": "sqlproxy",
                "datasource": {"id": datasource.luid, "name": datasource.name},
                "datasourceName": datasource.name,
                "serverAddress": "",
            }
        )
        datasource.downstream.append(workbook.name)

    def grant(self, obj: Any, group: Group, capability: str, mode: str = "Allow") -> None:
        """Record one permission row on a project or workbook."""
        kind = "project" if isinstance(obj, Project) else "workbook"
        self.grants.append(Grant(kind, obj.luid, "group", group.luid, capability, mode))

    # ------------------------------------------------------------- misbehave

    def expire_session(self) -> None:
        """Invalidate every issued token, so the next call answers ``401002``.

        MEASURED on Tableau Cloud (and the reason ``harvest_estate_assets`` signs in per asset): a
        session drops mid-loop, and a client that treats that as a hard failure truncates the run.
        """
        self._expired |= self.tokens
        self.tokens = set()

    def forbid(self, path_fragment: str) -> None:
        """Answer 403 for any path containing this fragment (one workbook's permissions, say)."""
        self._forbidden_paths.add(path_fragment)

    def mint_token(self) -> str:
        """Issue a valid session token without the sign-in round trip (test convenience only)."""
        token = str(uuid.uuid4())
        self.tokens.add(token)
        return token

    # ------------------------------------------------------------- transport

    def handle(self, method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict, bytes]:
        """Route one request. Transport-agnostic, so it can be driven with or without a socket."""
        parsed = urlparse(url)
        path, query = parsed.path, parse_qs(parsed.query)
        self.requests.append((method, path + (f"?{parsed.query}" if parsed.query else "")))

        if path.endswith("/auth/signin") and method == "POST":
            return self._signin(body)
        if any(fragment in path for fragment in self._forbidden_paths):
            return self._fail(403, "403004", "Permission denied")

        token = headers.get("x-tableau-auth", "")
        if token in self._expired:
            return self._fail(401, SESSION_LOST, "Invalid authentication credentials were provided")
        if token not in self.tokens:
            return self._fail(401, "401001", "Signin Error")

        if path.endswith("/auth/signout"):
            self.tokens.discard(token)
            return 204, {}, b""
        if path.endswith("/api/metadata/graphql"):
            return self._graphql(body)
        marker = f"/api/{self.rest_version}/sites/{self.site_id}"
        if marker not in path:
            return self._fail(404, "404000", f"no route for {path}")
        return self._rest(path.split(marker, 1)[1], query)

    def call(self, path: str) -> dict[str, Any]:
        """The in-process seam the engine's ``survey_site(call, site_id)`` takes.

        Same router, no socket - so an engine function that accepts an injected caller can be driven
        directly, while its CLI still goes over loopback.
        """
        token = next(iter(self.tokens), "")
        url = f"http://127.0.0.1/api/{self.rest_version}{path}"
        status, _headers, payload = self.handle("GET", url, {"x-tableau-auth": token}, b"")
        if status != 200:
            raise RuntimeError(f"GET {path} failed ({status}): {payload[:200]!r}")
        return json.loads(payload)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _json(status: int, payload: dict) -> tuple[int, dict, bytes]:
        return status, {"Content-Type": "application/json;charset=utf-8"}, json.dumps(payload).encode("utf-8")

    def _fail(self, status: int, code: str, summary: str) -> tuple[int, dict, bytes]:
        return self._json(status, {"error": {"code": code, "summary": summary, "detail": summary}})

    def _signin(self, body: bytes) -> tuple[int, dict, bytes]:
        try:
            credentials = json.loads(body or b"{}").get("credentials") or {}
        except json.JSONDecodeError:
            return self._fail(400, "400006", "bad request body")
        name = credentials.get("personalAccessTokenName")
        secret = credentials.get("personalAccessTokenSecret")
        # Strict on purpose: a PAT is two values, and the failure when only one is right is the
        # single most common credential mistake this pipeline hits.
        if not name or self.pat_credentials.get(name) != secret:
            return self._fail(401, "401001", "Signin Error: invalid personal access token")
        site = (credentials.get("site") or {}).get("contentUrl", "")
        if site != self.content_url:
            return self._fail(404, "404000", f"site {site!r} not found")
        token = str(uuid.uuid4())
        self.tokens.add(token)
        return self._json(
            200,
            {
                "credentials": {
                    "token": token,
                    "site": {"id": self.site_id, "contentUrl": self.content_url},
                    "user": {"id": "user-1"},
                }
            },
        )

    def _page(self, rows: list[dict], collection: str, item: str, query: dict) -> tuple[int, dict, bytes]:
        """One page, with Tableau's string-valued pagination block."""
        size = int((query.get("pageSize") or ["100"])[0])
        if self.page_size:
            size = min(size, self.page_size)
        number = int((query.get("pageNumber") or ["1"])[0])
        window = rows[(number - 1) * size : number * size]
        payload = window[0] if (self.single_row_as_object and len(window) == 1) else window
        return self._json(
            200,
            {
                "pagination": {"pageNumber": str(number), "pageSize": str(size), "totalAvailable": str(len(rows))},
                collection: {item: payload},
            },
        )

    # ------------------------------------------------------------------ REST

    def _rest(self, path: str, query: dict) -> tuple[int, dict, bytes]:
        segments = [s for s in path.split("?")[0].strip("/").split("/") if s]
        if not segments:
            return self._fail(404, "404000", "no collection named")
        collection = segments[0]

        if len(segments) == 1:
            return self._collection(collection, query)
        luid, action = segments[1], (segments[2] if len(segments) > 2 else "")
        if action == "permissions":
            return self._permissions(collection.rstrip("s"), luid)
        if collection == "groups" and action == "users":
            group = next((g for g in self.groups if g.luid == luid), None)
            rows = [{"id": f"user-{n}", "name": n} for n in (group.members if group else [])]
            return self._page(rows, "users", "user", query)
        if collection == "workbooks" and action == "connections":
            workbook = next((w for w in self.workbooks if w.luid == luid), None)
            if workbook is None:
                return self._fail(404, "404011", "workbook not found")
            return self._json(200, {"connections": {"connection": workbook.connections}})
        if action == "content":
            return self._content(collection, luid)
        return self._fail(404, "404000", f"no route for {path}")

    def _collection(self, collection: str, query: dict) -> tuple[int, dict, bytes]:
        table: dict[str, tuple[list[dict], str, str]] = {
            "workbooks": ([w.row() for w in self.workbooks], "workbooks", "workbook"),
            "datasources": ([d.row() for d in self.datasources], "datasources", "datasource"),
            "projects": ([p.row() for p in self.projects], "projects", "project"),
            "groups": ([g.row() for g in self.groups], "groups", "group"),
            "flows": (self.flows, "flows", "flow"),
            "subscriptions": (self.subscriptions, "subscriptions", "subscription"),
            "dataAlerts": (self.alerts, "dataAlerts", "dataAlert"),
            "customviews": (self.custom_views, "customViews", "customView"),
        }
        if collection == "views":
            usage = (query.get("includeUsageStatistics") or ["false"])[0] == "true"
            rows = []
            for view in self.views:
                row = view.row()
                if not usage:
                    # Tableau omits the usage block unless it was asked for; a mock that always
                    # returned it would hide a client that forgot the flag.
                    row.pop("usage", None)
                rows.append(row)
            return self._page(rows, "views", "view", query)
        if collection not in table:
            return self._fail(404, "404000", f"unknown collection {collection!r}")
        rows, key, item = table[collection]
        return self._page(rows, key, item, query)

    def _permissions(self, object_type: str, luid: str) -> tuple[int, dict, bytes]:
        by_grantee: dict[str, list[dict]] = {}
        for grant in self.grants:
            if grant.object_type == object_type and grant.object_luid == luid:
                by_grantee.setdefault(grant.grantee_luid, []).append({"name": grant.capability, "mode": grant.mode})
        grantees = [
            {"group": {"id": grantee}, "capabilities": {"capability": capabilities}}
            for grantee, capabilities in by_grantee.items()
        ]
        return self._json(200, {"permissions": {"granteeCapabilities": grantees}})

    def _content(self, collection: str, luid: str) -> tuple[int, dict, bytes]:
        if collection == "workbooks":
            found = next((w for w in self.workbooks if w.luid == luid), None)
            name, payload = (found.name + found.extension, found.content) if found else ("", b"")
        else:
            found = next((d for d in self.datasources if d.luid == luid), None)
            name, payload = (found.name + ".tdsx", found.content) if found else ("", b"")
        if found is None:
            return self._fail(404, "404011", f"{collection[:-1]} {luid} not found")
        # Tableau's real header is the non-standard `name=`, with no `filename=`. Serving the
        # standard form instead would let a client that only understands `filename=` pass here and
        # fail against the site.
        headers = {"Content-Type": "application/octet-stream", "Content-Disposition": f'name="{name}"'}
        return 200, headers, payload

    # --------------------------------------------------------------- GraphQL

    def _graphql(self, body: bytes) -> tuple[int, dict, bytes]:
        try:
            query = json.loads(body or b"{}").get("query") or ""
        except json.JSONDecodeError:
            return self._json(200, {"errors": [{"message": "could not parse the request body"}]})
        if "downstreamWorkbooks" in query:
            return self._json(200, {"data": {"publishedDatasources": self._lineage()}})
        if "workbooks" in query:
            return self._json(
                200,
                {
                    "data": {
                        "workbooks": [self._structure(w) for w in self.workbooks],
                        "publishedDatasources": [
                            {
                                "name": d.name,
                                "isCertified": d.is_certified,
                                "hasExtracts": d.has_extracts,
                                "extractLastRefreshTime": d.extract_last_refresh,
                                "upstreamTables": [{"fullName": f"[dbo].[{d.name}]"}],
                            }
                            for d in self.datasources
                        ],
                    }
                },
            )
        # A query we do not serve is an ERROR, not an empty answer: silently returning `{}` is how a
        # caller concludes "this estate has no dependencies" and sequences the migration wrong.
        return self._json(200, {"errors": [{"message": "unsupported query for the mock Metadata API"}]})

    def _lineage(self) -> list[dict[str, Any]]:
        by_name = {w.name: w for w in self.workbooks}
        return [
            {
                "id": f"gid-{d.luid}",
                "luid": d.luid,
                "name": d.name,
                "projectName": d.project.name,
                "hasExtracts": d.has_extracts,
                "downstreamWorkbooks": [
                    {"luid": by_name[n].luid, "name": n, "projectName": by_name[n].project.name}
                    for n in d.downstream
                    if n in by_name
                ],
            }
            for d in self.datasources
        ]

    def _structure(self, workbook: Workbook) -> dict[str, Any]:
        """The Metadata API's view of a workbook, READ OUT OF the bytes this site serves."""
        node = structure_from_workbook(workbook.content)
        node["name"] = workbook.name
        node["projectName"] = workbook.project.name
        return node


def structure_from_workbook(content: bytes) -> dict[str, Any]:
    """Derive ``{sheets, dashboards, embeddedDatasources}`` from real workbook XML.

    Deliberately independent of ``parse_tableau``: if the mock's answers were produced by the parser
    under test, a parser bug would be invisible because both sides would share it.
    """
    xml = content
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            inner = next(n for n in archive.namelist() if n.endswith((".twb", ".tds")))
            xml = archive.read(inner)
    root = ElementTree.fromstring(xml)

    sources = []
    for datasource in root.findall("./datasources/datasource"):
        name = datasource.get("caption") or datasource.get("name") or ""
        if name == "Parameters":
            continue
        fields = []
        for column in datasource.findall("./column"):
            calculation = column.find("./calculation")
            formula = calculation.get("formula") if calculation is not None else None
            fields.append(
                {
                    "name": (column.get("caption") or column.get("name") or "").strip("[]"),
                    "__typename": "CalculatedField" if formula else "ColumnField",
                    "role": (column.get("role") or "dimension").upper(),
                    "dataType": (column.get("datatype") or "string").upper(),
                    **({"formula": formula} if formula else {}),
                }
            )
        sources.append(
            {
                "name": name,
                "hasUserReference": any("USER" in (f.get("formula") or "").upper() for f in fields),
                "fields": fields,
            }
        )
    return {
        "sheets": [{"name": w.get("name")} for w in root.findall("./worksheets/worksheet")],
        "dashboards": [{"name": d.get("name")} for d in root.findall("./dashboards/dashboard")],
        "embeddedDatasources": sources,
    }


class _Handler(BaseHTTPRequestHandler):
    """Adapts :meth:`TableauSite.handle` onto ``http.server``."""

    site: TableauSite

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, out_headers, payload = self.site.handle(self.command, self.path, headers, body)
        self.send_response(status)
        for key, value in out_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve
    do_PUT = _serve
    do_DELETE = _serve

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default stderr access log; ``site.requests`` is the record that matters."""


@contextlib.contextmanager
def serve(site: TableauSite):
    """Run ``site`` on an ephemeral loopback port. Yields the base URL (no trailing slash)."""
    handler = type("_BoundHandler", (_Handler,), {"site": site})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def env_for(site: TableauSite, base_url: str, *, pat_name: str = "mock-pat") -> dict[str, str]:
    """The environment variables every Tableau-auth script in this repo expects, pointed at the mock.

    Includes the ENGINE's ``TABLEAU_PAT_VALUE`` as well as our ``TABLEAU_PAT_SECRET``: the two tiers
    spell the secret differently, and an engine script that only knows its own name is exactly the
    caller that hangs on a hidden prompt when it is missing.
    """
    secret = site.pat_credentials[pat_name]
    return {
        "TABLEAU_SERVER_URL": base_url,
        "TABLEAU_SITE": site.content_url,
        "TABLEAU_PAT_NAME": pat_name,
        "TABLEAU_PAT_SECRET": secret,
        "TABLEAU_PAT_VALUE": secret,
        "TABLEAU_REST_API_VERSION": site.rest_version,
    }


_LUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def looks_like_luid(value: str) -> bool:
    """Whether a string is shaped like a Tableau LUID (used by tests asserting identity-by-luid)."""
    return bool(_LUID_RE.match(value or ""))
