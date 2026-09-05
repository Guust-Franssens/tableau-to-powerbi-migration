"""
purpose: discover the Tableau dependency graph BEFORE migrating anything, so a Tableau estate can be
         migrated MODEL-FIRST instead of workbook-by-workbook.

         A Tableau published data source is typically consumed by several workbooks. Migrating
         workbook-by-workbook rebuilds a near-identical semantic model every time, and those copies
         then drift. The correct Power BI shape mirrors Tableau's own: migrate each published data
         source ONCE into a shared semantic model, then bind every downstream report to it.

         This script asks Tableau itself who depends on what:
           * Metadata API (GraphQL) -> publishedDatasources { downstreamWorkbooks } lineage
           * REST API               -> download each .tdsx so the model layer can actually be parsed
           * estate_survey.json     -> the REST-derived dependency graph, supplied via --survey
         and emits a migration PLAN ordered by leverage (most-consumed data source first).

         WHY --survey EXISTS. The Metadata API is BLIND to 'sqlproxy' connections (a workbook that
         embeds a published data source), so it reports such a data source as having no downstream
         workbooks at all. Without a survey this script therefore under-reports the graph, and used
         to call those data sources "possibly abandoned" - the exact opposite of the truth, about
         data sources every consumer hard-depends on. `estate_survey.py --json` reads each
         workbook's real connections over REST and does see them. So:

           PRECEDENCE: where the survey and the Metadata API disagree, the SURVEY WINS. Metadata-API
           silence is not evidence of absence. Edges only the Metadata API saw are still KEPT (never
           dropped - losing a real dependency is the failure this whole script guards against) and
           labelled 'metadata-api', so an operator can see which source produced which claim.

           COMPLETENESS: a survey also reports on ITSELF - `degraded`, `listing_errors`,
           per-workbook `dependencies_unknown`. Every one of those means it did not see the whole
           estate, so its silence about a data source is not evidence either. Such a survey still
           CONTRIBUTES every edge it did see (that only ever adds dependencies, which is the safe
           direction); what it cannot do is license the word "abandoned".

         The dedup key it prints is the SAME key `scripts/parse_tableau.py` stamps on a parsed
         workbook (`data_sources[].published_datasource.key`), so server-side lineage and locally
         parsed workbooks line up.

usage:   # credentials come from a git-ignored .env (see .env.example) or the environment, never
         # argv (which leaks to the process list):
         #   TABLEAU_SERVER_URL=https://10ax.online.tableau.com
         #   TABLEAU_SITE=mysitecontenturl        (empty string for Tableau Server's Default site)
         #   TABLEAU_PAT_NAME=<personal access token name>
         #   TABLEAU_PAT_SECRET=<personal access token secret>
         python scripts/tableau_lineage.py --plan --survey _assessment/estate_survey.json
         python scripts/tableau_lineage.py --plan --env .env
         python scripts/tableau_lineage.py --plan --download migrations/datasources/_downloads

         # offline: re-plan from a previously saved API response, no server needed
         python scripts/tableau_lineage.py --plan --from-json lineage.json --survey estate_survey.json

Docs: Metadata API endpoint POST <server>/api/metadata/graphql (help.tableau.com/current/api/
metadata_api/en-us/docs/meta_api_start.html); datasource download GET
/api/<ver>/sites/<site-id>/datasources/<datasource-id>/content.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tableau_env import redact, redacted_note, require, resolve_env, scrub_tree  # noqa: E402  # pylint: disable=wrong-import-position

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("tableau_lineage")

DEFAULT_API_VERSION = "3.19"
_LUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.IGNORECASE)
_TEXTUAL_ARCHIVE_MEMBERS = (".twb", ".tds", ".xml", ".txt", ".json", ".csv", ".ini", ".log", ".yaml", ".yml")

# How an edge (data source -> workbook) was learned. Printed next to every edge so a plan is
# self-describing: an operator never has to guess which system made which claim.
FROM_SURVEY = "survey"
FROM_METADATA = "metadata-api"
FROM_BOTH = "both"

PRECEDENCE_NOTE = (
    "PRECEDENCE: where the survey and the Metadata API disagree, the SURVEY WINS - it reads each\n"
    "            workbook's real connections over REST, while the Metadata API is blind to\n"
    "            'sqlproxy' (published data source) connections, so its silence is NOT evidence of\n"
    "            absence. Edges only the Metadata API saw are kept, never dropped, and marked\n"
    "            [metadata-api]."
)

NO_SURVEY_WARNING = (
    "NO --survey WAS SUPPLIED, so this plan is the Metadata API's view ALONE, and that view is\n"
    "known-incomplete: it does not see 'sqlproxy' (published data source) connections. Read every\n"
    "'no downstream workbooks' line below as UNKNOWN, never as unused. Re-run with:\n"
    "    python scripts/tableau_lineage.py --plan --survey _assessment/estate_survey.json"
)

# Tableau content lineage (workbook -> published datasource) is available WITHOUT the Data Management
# license; only *external* assets (databases/tables upstream of Tableau) require it. This query stays
# on the free side of that line on purpose.
LINEAGE_QUERY = """
query migrationLineage {
  publishedDatasources {
    id
    luid
    name
    projectName
    hasExtracts
    downstreamWorkbooks {
      luid
      name
      projectName
    }
  }
}
"""


def dedup_key(site: str, name: str) -> str:
    """Build the SAME stable key parse_tableau.py stamps on a workbook's published_datasource.

    Keep this in lockstep with `_parse_published_datasource`: '<site>/<name>' lowercased, with the
    site omitted when there isn't one (Tableau Server's Default site publishes no `site=` attribute).
    """
    return "/".join(p for p in (site, name) if p).lower()


def _norm(name: str | None) -> str:
    """Case/whitespace-insensitive matching key for a Tableau content name."""
    return (name or "").strip().lower()


@dataclass
class SurveyDatasource:
    """One published data source as the survey saw it, with every workbook that binds to it."""

    name: str
    luid: str | None = None
    project: str | None = None
    consumers: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    luids: set[str] = field(default_factory=set)

    def observe(self, luid: str | None, project: str | None) -> None:
        """Record a RESOLVED sighting: this is the identity the download step will use."""
        if luid:
            self.luids.add(luid)
            self.luid = self.luid or luid
        if project:
            self.projects.add(project)
            self.project = self.project or project

    def note_candidate(self, luid: str | None, project: str | None) -> None:
        """Record one candidate the survey could NOT choose between - evidence, never identity.

        `resolve_dependency` returns `status='ambiguous', luid='', project=''` plus a `candidates`
        list naming every data source that shares the name, and it "NEVER picks one" on purpose.
        Neither does this: candidates feed the ambiguity evidence only, so `luid`/`project` stay
        empty and the download step skips a data source nobody can identify - instead of quietly
        fetching whichever candidate happened to be listed first.
        """
        if luid:
            self.luids.add(luid)
        if project:
            self.projects.add(project)

    @property
    def ambiguous(self) -> bool:
        """True when this ONE key covers several data sources that merely share a name."""
        return len(self.projects) > 1 or len(self.luids) > 1


@dataclass(frozen=True)
class Survey:
    """The REST-derived dependency graph from `estate_survey.py --json`.

    This is GROUND TRUTH for whether a dependency exists: it was read from each workbook's own
    connections. `gaps` records the ways this particular survey is nevertheless incomplete (it
    reports itself DEGRADED, a site listing failed so workbooks are missing from it entirely, a
    workbook's connections could not be read, a dependency resolved to no data source). An
    incomplete survey may still ADD edges, but it must not be used to claim a data source is
    unused, because the edge proving otherwise may be exactly the one it failed to read.
    """

    path: Path
    datasources: dict[str, SurveyDatasource] = field(default_factory=dict)
    workbook_names: dict[str, str] = field(default_factory=dict)
    by_luid: dict[str, str] = field(default_factory=dict)
    workbooks_total: int = 0
    gaps: tuple[str, ...] = ()
    scoped: bool = False

    @property
    def complete(self) -> bool:
        """True when nothing stopped this survey from seeing the whole estate."""
        return not self.gaps

    def match(self, luid: str | None, name: str | None) -> tuple[str | None, str | None]:
        """Resolve a Metadata-API data source to this survey's key -> (key, how it was matched).

        LUID first because it is an identity; NAME only as a fallback, because it is not. The
        fallback is load-bearing rather than decorative: `estate_survey.py::resolve_dependency`
        emits `luid: ""` for any dependency it could not resolve to exactly one data source, so on a
        real site the name path is what carries those edges. HOW the match was made is returned with
        it so the caller can flag a name match that landed on more than one data source.
        """
        if luid and luid in self.by_luid:
            return self.by_luid[luid], "luid"
        key = _norm(name)
        return (key, "name") if key in self.datasources else (None, None)

    def consumers(self, key: str) -> list[str]:
        """Every workbook the survey saw binding to this data source, by display name."""
        entry = self.datasources.get(key)
        if not entry:
            return []
        return sorted((self.workbook_names.get(w, w) for w in entry.consumers), key=str.lower)

    def includes_metadata_source(
        self, datasource_luid: str | None, survey_key: str | None, downstream_workbooks: Sequence[str]
    ) -> bool:
        """Whether a Metadata API row belongs to a complete, filtered survey.

        A survey dependency with a stable LUID is matched by identity. Name-only dependencies are
        intentionally accepted only when the survey could not resolve any LUID; an ambiguous name
        must not select an arbitrary Metadata API row. A Metadata edge to a selected workbook is
        retained even when the datasource itself is outside the selected project.
        """
        selected_workbooks = {_norm(workbook) for workbook in self.workbook_names}
        if any(_norm(workbook) in selected_workbooks for workbook in downstream_workbooks):
            return True
        if survey_key is None:
            return False
        entry = self.datasources.get(survey_key)
        if not entry:
            return False
        if datasource_luid and entry.luid:
            return datasource_luid == entry.luid
        if datasource_luid and entry.luids:
            return False
        return not entry.luids


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    """The survey's own summary block, or an empty one."""
    summary = data.get("summary")
    return summary if isinstance(summary, dict) else {}


def _count(values: Sequence[Any]) -> int:
    """The largest of several counts of the SAME failure, ignoring anything that is not a count.

    `estate_survey.py` records one failure in more than one place (`connection_read_errors`, the
    per-workbook `dependencies_unknown` flag, `summary.dependencies_unknown`). Taking the max
    reports it ONCE, while still catching a survey that carries only one of the three - which is
    exactly what `build_survey()` produces when it is called directly, without `survey_site()`'s
    error bookkeeping.
    """
    counts = [len(value) if isinstance(value, list) else value for value in values]
    return max((c for c in counts if isinstance(c, int) and not isinstance(c, bool)), default=0)


def _flag_gaps(data: dict[str, Any], other_gaps: Sequence[str] = ()) -> list[str]:
    """Read the survey's OWN verdict on itself: `degraded`.

    `survey_site()` sets `degraded = bool(errors or listing_errors)` and documents it as "this
    survey did NOT see the whole estate, so its 'no dependency' answers are not evidence of
    independence". That is the single flag every consumer is told to trust, so it is read first and
    honoured on its own - never re-derived from the error lists, which is how a NEW failure class
    added upstream would silently stop counting.

    Its ABSENCE is a gap too. A survey with no flag cannot show it saw the whole estate, and the
    only claim gated on completeness here is the strongest one this tool makes ("may be abandoned").

    That gate stays strict even for the common benign cause - an OLD survey. `degraded` and
    `listing_errors` arrived in engine 2.117.0 (upstream commit 72f983a8, 2026-08-10), so every
    survey taken before that date lacks them; a pre-2.117.0 survey that silently lost a listing call
    is genuinely indistinguishable from a healthy one, which is why the flag was added. What this
    DOES do is name that likely cause when nothing else in the survey looks wrong, so an operator
    five days from a workshop is not sent hunting for a failure that never happened - or, worse,
    told only to "re-run estate_survey.py", which needs live Tableau credentials.
    """
    declared = data.get("degraded", _summary(data).get("degraded"))
    if declared is None:
        cause = (
            " - no other error is reported, so this survey most likely predates engine 2.117.0 "
            "(2026-08-10), which added the flag"
            if not other_gaps
            else ""
        )
        return [
            "the survey carries no 'degraded' flag, so it cannot show it saw the whole estate "
            f"(re-run estate_survey.py to produce one){cause}"
        ]
    return ["the survey reports itself DEGRADED (estate_survey.py's own flag)"] if declared else []


def _visibility_gaps(data: dict[str, Any]) -> list[str]:
    """Every way this survey failed to SEE part of the estate."""
    gaps: list[str] = []
    summary = _summary(data)
    workbooks = data.get("workbooks") or []

    listing = _count((data.get("listing_errors"), summary.get("listing_errors")))
    if listing:
        gaps.append(
            f"{listing} site listing(s) failed - workbooks or data sources are MISSING from this survey entirely"
        )
    unread = _count(
        (
            data.get("connection_read_errors"),
            summary.get("connection_read_errors"),
            summary.get("dependencies_unknown"),
            sum(1 for wb in workbooks if isinstance(wb, dict) and wb.get("dependencies_unknown")),
        )
    )
    if unread:
        gaps.append(f"{unread} workbook connection(s) could not be read")
    if not workbooks:
        gaps.append("the survey lists no workbooks at all, so it observed no consumer edge")
    total = summary.get("workbooks_total")
    if isinstance(total, int) and total > len(workbooks):
        gaps.append(f"the survey lists {len(workbooks)} of the {total} workbook(s) it counted")
    return gaps


def _resolution_gaps(data: dict[str, Any], unresolved_deps: int) -> list[str]:
    """Every dependency the survey saw but could not tie to a published data source.

    The survey declares this count in `unresolved_dependencies` (and in its summary) AND it is
    visible per-row in the parsed edges; `_count` reports the ONE failure once, so a listing failure
    does not print the same 38 dependencies under two different sentences.
    """
    unresolved = _count(
        (
            data.get("unresolved_dependencies"),
            _summary(data).get("unresolved_dependencies"),
            unresolved_deps,
        )
    )
    if not unresolved:
        return []
    return [f"{unresolved} dependency(ies) did not resolve to a published data source"]


def _survey_gaps(data: dict[str, Any], unresolved_deps: int) -> tuple[str, ...]:
    """Describe every reason this survey's view of the estate is incomplete.

    Read EVERY signal `estate_survey.py` publishes about its own completeness, not the subset that
    happens to be convenient. Reading only `connection_read_errors` + `unresolved_dependencies` let
    a survey with `degraded: true` and a failed site listing - i.e. one with whole workbooks missing
    - report `complete`, which re-armed issue #126's wrong claim with MORE authority than the
    original ("Both the Metadata API and the survey found no consumer"). The missing workbook is
    precisely the consumer that would have disproved it.
    """
    observed = _visibility_gaps(data) + _resolution_gaps(data, unresolved_deps)
    return tuple(_flag_gaps(data, observed) + observed)


def _survey_scope(data: dict[str, Any]) -> bool:
    """Return whether the engine declared this survey narrower than the whole site."""
    scope = data.get("scope")
    if isinstance(scope, str):
        return _norm(scope) not in {"", "site", "site-wide", "sitewide", "all"}
    if not isinstance(scope, dict):
        return False
    if scope.get("site_wide") is True or _norm(scope.get("type")) in {"site", "site-wide", "sitewide", "all"}:
        return False
    return bool(
        _norm(scope.get("type")) or any(scope.get(key) for key in ("projects", "project", "workbooks", "workbook"))
    )


def _read_survey_edges(
    workbooks: list[dict[str, Any]],
    datasources: dict[str, SurveyDatasource],
    workbook_names: dict[str, str],
) -> int:
    """Index every workbook -> published data source edge the survey recorded.

    Returns the number of dependencies that did NOT resolve to a published data source.
    """
    unresolved = 0
    for workbook in workbooks:
        wb_display = workbook.get("name") or "?"
        workbook_names[_norm(wb_display)] = wb_display
        for dep in workbook.get("published_dependencies") or []:
            if not isinstance(dep, dict):
                continue
            # `datasource_name` is estate_survey.py's field, read explicitly rather than guessed at
            # (see assess_estate.py::_parse_dependencies, which learned the same lesson): a guess
            # that yields nothing is indistinguishable from an estate with no dependencies.
            name = dep.get("datasource_name")
            if not name:
                continue
            entry = datasources.setdefault(_norm(name), SurveyDatasource(name=name))
            entry.consumers.add(_norm(wb_display))
            entry.observe(dep.get("luid"), dep.get("project"))
            # An AMBIGUOUS dependency carries `luid: ""`, `project: ""` and a `candidates` list -
            # `resolve_dependency` "NEVER picks one" when a name matches several data sources. Read
            # that list: it is the only place the collision is visible when the Metadata API lists
            # at most one row for the name, and without it the merge this tool performs on purpose
            # (over-migrate, never orphan) would happen SILENTLY, which is the one thing it must
            # not do.
            for candidate in dep.get("candidates") or []:
                if isinstance(candidate, dict):
                    entry.note_candidate(candidate.get("luid"), candidate.get("project"))
            if dep.get("status") and dep.get("status") != "resolved":
                unresolved += 1
    return unresolved


def load_survey(path: Path) -> Survey:
    """Read `estate_survey.py --json` output into a matchable dependency graph.

    Raises rather than under-reporting. A survey whose schema has moved would otherwise parse to
    zero edges, and "no edges" is indistinguishable from "no dependencies" - which sequences the
    migration wrong and re-creates the very defect --survey exists to fix.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "workbooks" not in data:
        raise RuntimeError(
            f"{path} has no 'workbooks' key - this does not look like estate_survey.py --json output. "
            "Refusing to plan from it: an unreadable survey must not be mistaken for an estate with "
            "no dependencies."
        )

    workbooks = data.get("workbooks") or []
    datasources: dict[str, SurveyDatasource] = {}
    workbook_names: dict[str, str] = {}
    unresolved = _read_survey_edges(workbooks, datasources, workbook_names)

    # `required_datasources` names the same data sources again, with the LUID/project the download
    # step needs. It also keeps a required data source in the plan when its consumers were listed
    # under a name variant, so nothing that must be fetched first can fall out of the sequence.
    for required in data.get("required_datasources") or []:
        if not isinstance(required, dict):
            continue
        name = required.get("datasource_name")
        if not name:
            continue
        entry = datasources.setdefault(_norm(name), SurveyDatasource(name=name))
        entry.observe(required.get("luid"), required.get("project"))

    declared = sum(len(wb.get("published_dependencies") or []) for wb in workbooks)
    if declared and not any(ds.consumers for ds in datasources.values()):
        raise RuntimeError(
            f"{path} declares {declared} dependency entries but none parsed - its schema has changed. "
            "Refusing to report 'no dependencies', which would sequence the migration wrong."
        )

    return Survey(
        path=path,
        datasources=datasources,
        workbook_names=workbook_names,
        by_luid={ds.luid: key for key, ds in datasources.items() if ds.luid},
        workbooks_total=len(workbooks),
        gaps=_survey_gaps(data, unresolved),
        scoped=_survey_scope(data),
    )


class TableauSession(NamedTuple):
    """An authenticated Tableau connection: everything the REST + Metadata calls need to be made."""

    server: str
    token: str
    site_id: str
    api_version: str = DEFAULT_API_VERSION
    pat_name: str = ""
    pat_secret: str = ""

    @property
    def base(self) -> str:
        """Server root with any trailing slash removed."""
        return self.server.rstrip("/")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """POST JSON and return the decoded JSON response."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in {"Content-Type": "application/json", "Accept": "application/json", **headers}.items():
        req.add_header(key, value)
    result = json.loads(_response_text(req))
    return result


def _response_text(req: urllib.request.Request) -> str:
    """Read a credentialed Tableau response without persisting it."""
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - URL comes from env config
        return resp.read().decode("utf-8")


def sign_in(server: str, site: str, pat_name: str, pat_secret: str, api_version: str) -> TableauSession:
    """Authenticate with a Personal Access Token; return an authenticated session.

    The Metadata API shares this token -- there is no separate GraphQL login.
    """
    url = f"{server.rstrip('/')}/api/{api_version}/auth/signin"
    payload = {
        "credentials": {
            "personalAccessTokenName": pat_name,
            "personalAccessTokenSecret": pat_secret,
            "site": {"contentUrl": site},
        }
    }
    creds = _post_json(url, payload, headers={}).get("credentials", {})
    token = creds.get("token")
    site_id = (creds.get("site") or {}).get("id")
    if not token or not site_id:
        raise RuntimeError("sign-in succeeded but returned no token/site id")
    return TableauSession(
        server=server,
        token=token,
        site_id=site_id,
        api_version=api_version,
        pat_name=pat_name,
        pat_secret=pat_secret,
    )


def fetch_lineage(session: TableauSession) -> list[dict[str, Any]]:
    """Run the Metadata API lineage query; return the publishedDatasources list."""
    url = f"{session.base}/api/metadata/graphql"
    result = _post_json(
        url,
        {"query": LINEAGE_QUERY},
        headers={"X-Tableau-Auth": session.token},
    )
    if result.get("errors"):
        raise RuntimeError(
            "Metadata API returned errors: "
            + redacted_note(
                json.dumps(result["errors"]),
                lambda text: redact(text, session.pat_name, session.pat_secret, session.token),
                limit=400,
            )
        )
    return result.get("data", {}).get("publishedDatasources", [])


def download_datasource(session: TableauSession, luid: str, dest: Path) -> Path:
    """Download one published data source's content (.tdsx) so its model layer can be parsed."""
    luid = _download_stem(luid, session)
    if luid is None:
        raise RuntimeError("refusing to download a datasource without a valid LUID")
    url = f"{session.base}/api/{session.api_version}/sites/{session.site_id}/datasources/{luid}/content"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Tableau-Auth", session.token)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 - URL comes from env config
        payload = resp.read()
    if _contains_credential(payload, session):
        raise RuntimeError("refusing to persist a datasource response that reflects a credential")
    dest.write_bytes(payload)
    return dest


def _contains_credential(payload: bytes, session: TableauSession) -> bool:
    """Inspect raw XML and persisted .tdsx metadata before persistence."""
    secrets = (session.pat_name, session.pat_secret, session.token)

    def reflected(value: bytes) -> bool:
        text = value.decode("utf-8", "replace")
        return redact(text, *secrets) != text

    if reflected(payload):
        return True
    if not payload.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if reflected(archive.comment):
                return True
            for member in archive.infolist():
                if (
                    redact(member.filename, *secrets) != member.filename
                    or reflected(member.comment)
                    or reflected(member.extra)
                ):
                    return True
            members = [
                member for member in archive.infolist() if member.filename.lower().endswith(_TEXTUAL_ARCHIVE_MEMBERS)
            ]
            if not members:
                raise RuntimeError("refusing to persist an archive with no assessable Tableau XML member")
            return any(reflected(archive.read(member)) for member in members)
    except zipfile.BadZipFile as exc:
        raise RuntimeError("refusing to persist an unreadable datasource archive") from exc


def _download_stem(luid: object, session: TableauSession) -> str | None:
    """Return the only response-derived value allowed to name a download."""
    if (
        not isinstance(luid, str)
        or not _LUID.fullmatch(luid)
        or luid in {session.pat_name, session.pat_secret, session.token}
    ):
        return None
    return luid


def _origin(key: str, metadata_keys: set[str], survey_keys: set[str]) -> str:
    """Label one edge with the source(s) that saw it."""
    if key in metadata_keys and key in survey_keys:
        return FROM_BOTH
    return FROM_SURVEY if key in survey_keys else FROM_METADATA


def _entry(
    site: str,
    source: dict[str, Any],
    metadata_workbooks: list[str],
    survey_workbooks: list[str] | None,
    matched_via: str | None = None,
) -> dict[str, Any]:
    """Build one plan row, merging both sources' edges and recording where each edge came from.

    Merging (rather than replacing) is deliberate. The precedence rule settles the CLAIM - whether a
    data source has consumers, and therefore where it lands in the order - and the survey always
    wins that, because it can see edges the Metadata API structurally cannot. It does not license
    DELETING an edge the Metadata API reported: that would be the same class of error in the other
    direction, dropping a real dependency and rebuilding its consumer first.
    """
    seen: dict[str, str] = {_norm(w): w for w in metadata_workbooks}
    # The survey's spelling wins for display too, in step with the precedence rule.
    seen.update({_norm(w): w for w in survey_workbooks or []})

    metadata_keys = {_norm(w) for w in metadata_workbooks}
    survey_keys = {_norm(w) for w in survey_workbooks or []}
    downstream = sorted(seen.values(), key=str.lower)
    if survey_keys:
        evidence = FROM_BOTH if metadata_keys else FROM_SURVEY
    else:
        evidence = FROM_METADATA if metadata_keys else "none"
    name = source.get("name") or ""
    return {
        "key": dedup_key(site, name),
        "name": name,
        "luid": source.get("luid"),
        "project": source.get("project"),
        "has_extracts": source.get("has_extracts"),
        "downstream_count": len(downstream),
        "downstream_workbooks": downstream,
        "edge_origin": {seen[key]: _origin(key, metadata_keys, survey_keys) for key in seen},
        "metadata_count": len(metadata_keys),
        "survey_count": len(survey_keys),
        "survey_only": sorted((seen[k] for k in survey_keys - metadata_keys), key=str.lower),
        "metadata_only": sorted((seen[k] for k in metadata_keys - survey_keys), key=str.lower),
        "evidence": evidence,
        "known_to_survey": survey_workbooks is not None,
        "matched_via": matched_via,
        "name_collision": [],
    }


def _flag_name_collisions(by_key: dict[str, list[dict[str, Any]]], survey: Survey) -> None:
    """Mark every plan row whose survey edges were attached by NAME to more than one data source.

    A name is not an identity - two data sources called 'Sales' in different projects normalise to
    one survey key, so the survey's consumers get attributed to BOTH. That merge is kept rather than
    refused, and the direction is the reason: attaching a consumer to one data source too many
    over-migrates (recoverable, and the order still holds because both are sequenced ahead of it),
    while refusing the match would orphan a real consumer and rebuild it as an empty report - the
    failure this whole script exists to prevent. `estate_survey.py::resolve_dependency` refuses the
    same ambiguity and is right to: it is deciding an IDENTITY. This is only deciding an ORDER.

    What is not acceptable is doing it silently, so every affected row is flagged and printed. The
    edges are usable; the per-project attribution is not.
    """
    for key, rows in by_key.items():
        entry = survey.datasources.get(key)
        survey_projects = sorted(entry.projects) if entry and entry.ambiguous else []
        if len(rows) < 2 and not (entry and entry.ambiguous):
            continue
        projects = sorted({str(row["project"]) for row in rows if row["project"]} | set(survey_projects))
        for row in rows:
            row["name_collision"] = projects or ["?"]


def _survey_only_rows(site: str, survey: Survey, by_key: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Plan rows for data sources the Metadata API never listed at all.

    They are registered in `by_key` as well, not merely appended to the plan: a collision the
    Metadata API cannot see (it lists at most one row for the name - and for a `sqlproxy` source,
    often none) would otherwise reach the operator as a single quiet row, and the name merge this
    script performs on purpose would happen with no warning printed anywhere.
    """
    rows: list[dict[str, Any]] = []
    for key, seen in survey.datasources.items():
        if key in by_key:
            continue
        ambiguous = survey.scoped and survey.complete and len(seen.luids) > 1
        source = {
            "name": seen.name,
            "luid": None if ambiguous else seen.luid,
            "project": None if ambiguous else seen.project,
            "has_extracts": None,
        }
        row = _entry(site, source, [], survey.consumers(key), "survey-only-ambiguous" if ambiguous else "survey-only")
        rows.append(row)
        by_key.setdefault(key, []).append(row)
    return rows


def build_plan(datasources: list[dict[str, Any]], site: str, survey: Survey | None = None) -> list[dict[str, Any]]:
    """Turn raw lineage (plus an optional survey) into a plan ordered by LEVERAGE.

    Highest fan-out first is deliberate: migrating the data source that 12 workbooks depend on saves
    11 duplicate semantic models, so it is the highest-value unit of work in the estate.

    The survey does not merely annotate rows. A data source the Metadata API reports as having NO
    downstream workbooks is promoted into phase 1 as soon as the survey names one consumer, and a
    data source the Metadata API never listed at all is added from the survey - because a hard
    dependency missing from the plan is exactly how a report gets rebuilt before the model it binds
    to, which is the empty-report failure this script exists to prevent.
    """
    plan: list[dict[str, Any]] = []
    by_key: dict[str, list[dict[str, Any]]] = {}
    apply_scope = bool(survey and survey.scoped and survey.complete)
    for datasource in datasources:
        name = datasource.get("name") or ""
        downstream = [w.get("name") or "?" for w in datasource.get("downstreamWorkbooks") or []]
        survey_key, matched_via = survey.match(datasource.get("luid"), name) if survey else (None, None)
        if apply_scope and not survey.includes_metadata_source(datasource.get("luid"), survey_key, downstream):
            continue
        source = {
            "name": name,
            "luid": datasource.get("luid"),
            "project": datasource.get("projectName"),
            "has_extracts": datasource.get("hasExtracts"),
        }
        survey_workbooks = survey.consumers(survey_key) if survey and survey_key is not None else None
        entry = _entry(site, source, downstream, survey_workbooks, matched_via)
        plan.append(entry)
        if survey_key:
            by_key.setdefault(survey_key, []).append(entry)

    if survey:
        plan.extend(_survey_only_rows(site, survey, by_key))
        _flag_name_collisions(by_key, survey)

    return sorted(plan, key=lambda p: (-p["downstream_count"], (p["name"] or "").lower()))


def build_order(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the plan into ONE migration sequence: every data source before any consumer of it.

    Ordering is the whole point of this script, so it is emitted as data (and asserted in tests)
    rather than left implicit in two printed phase headings.
    """
    order: list[dict[str, Any]] = []
    rank: dict[str, int] = {}
    for entry in plan:
        if entry["downstream_count"] == 0:
            continue
        rank[entry["name"]] = len(order)
        order.append({"kind": "datasource", "name": entry["name"], "luid": entry["luid"], "requires": []})

    requires: dict[str, list[str]] = {}
    for entry in plan:
        for workbook in entry["downstream_workbooks"]:
            requires.setdefault(workbook, []).append(entry["name"])
    for workbook in sorted(requires, key=lambda w: (min(rank.get(d, 0) for d in requires[w]), w.lower())):
        # De-duplicated because two data sources that merely SHARE a name are indistinguishable in a
        # list of names: printing "after: Sales, Sales" states nothing the reader can act on. Both
        # rows still appear in the data source half of the order, so the guarantee is unaffected;
        # the collision itself is reported separately rather than smuggled in as a repeat.
        order.append({"kind": "workbook", "name": workbook, "luid": None, "requires": sorted(set(requires[workbook]))})
    return order


def _print_sources(survey: Survey | None) -> None:
    """Print which systems this plan was built from, and how their disagreements are settled."""
    if survey:
        log.info(
            "SOURCES: Metadata API (GraphQL) + survey %s (REST, %d workbook(s))",
            survey.path,
            survey.workbooks_total,
        )
        for line in PRECEDENCE_NOTE.splitlines():
            log.info("%s", line)
        if survey.gaps:
            log.info("SURVEY IS INCOMPLETE: %s.", "; ".join(survey.gaps))
            if survey.scoped:
                log.info(
                    "SCOPED SURVEY FILTER DISABLED: incomplete scope falls back to the full Metadata API "
                    "plan; narrowing it would silently hide dependencies."
                )
    else:
        log.info("SOURCES: Metadata API (GraphQL) only")
        for line in NO_SURVEY_WARNING.splitlines():
            log.info("%s", line)


def _print_header(plan: list[dict[str, Any]], survey: Survey | None) -> None:
    """Print the sources, the precedence rule, and the headline counts."""
    shared = [p for p in plan if p["downstream_count"] > 1]
    workbooks = {_norm(w) for p in plan for w in p["downstream_workbooks"]}
    consumed = [p for p in plan if p["downstream_count"] > 0]

    log.info("=" * 78)
    log.info("MIGRATION PLAN - model layer first")
    log.info("=" * 78)
    _print_sources(survey)
    log.info("")
    log.info(
        "%d published data source(s) feed %d workbook(s). %d are SHARED by more than one workbook.",
        len(consumed),
        len(workbooks),
        len(shared),
    )
    if survey:
        metadata_sources = [p for p in plan if p["metadata_count"] > 0]
        metadata_workbooks = {_norm(w) for p in plan for w in p["downstream_workbooks"] if p["metadata_count"]}
        log.info(
            "         (the Metadata API alone saw %d data source(s) feeding %d workbook(s); "
            "the survey raised that to %d and %d.)",
            len(metadata_sources),
            len(metadata_workbooks),
            len(consumed),
            len(workbooks),
        )


def _print_phase1(plan: list[dict[str, Any]]) -> None:
    """Print the data sources to migrate first, highest leverage first, edge by edge."""
    log.info("\nPHASE 1 - migrate these data sources to semantic models (highest leverage first):\n")
    for i, entry in enumerate(plan, 1):
        if entry["downstream_count"] == 0:
            continue
        saved = max(0, entry["downstream_count"] - 1)
        log.info(
            "  %2d. %-38s  %2d workbook(s)   key=%s",
            i,
            (entry["name"] or "?")[:38],
            entry["downstream_count"],
            entry["key"],
        )
        log.info(
            "      project=%-24s extracts=%-5s saves %d duplicate model(s)   evidence=%s",
            entry["project"],
            entry["has_extracts"],
            saved,
            entry["evidence"],
        )
        for workbook in entry["downstream_workbooks"]:
            log.info("        -> %-44s [%s]", workbook, entry["edge_origin"][workbook])


def _print_disagreements(plan: list[dict[str, Any]], survey: Survey | None) -> None:
    """Name every data source the two systems describe differently, and how it was resolved."""
    if not survey:
        return
    conflicted = [p for p in plan if p["survey_only"] or p["metadata_only"]]
    if not conflicted:
        log.info("\nThe survey and the Metadata API agree on every data source.")
        return
    log.info("\nDISAGREEMENTS - resolved by the precedence rule above (the survey wins):")
    for entry in conflicted:
        if entry["survey_only"]:
            log.info(
                "  - %s: Metadata API saw %d consumer(s), survey saw %d -> SURVEY WINS, %d edge(s) added: %s",
                entry["name"],
                entry["metadata_count"],
                entry["survey_count"],
                len(entry["survey_only"]),
                ", ".join(entry["survey_only"]),
            )
        if entry["metadata_only"]:
            log.info(
                "  - %s: the survey did not see %d Metadata-API edge(s) - KEPT (dropping a real "
                "dependency is the risk this guards against), marked [metadata-api]: %s",
                entry["name"],
                len(entry["metadata_only"]),
                ", ".join(entry["metadata_only"]),
            )


def _print_name_collisions(plan: list[dict[str, Any]]) -> None:
    """Name every data source whose survey edges were attached on a shared NAME, not an identity.

    Silence here would be the same mistake as the "abandoned" claim in a different place: a merge
    the operator cannot see is a merge they cannot check. The edges are kept (see
    `_flag_name_collisions`); what is withheld is the confident per-project attribution.
    """
    collided = [entry for entry in plan if entry["name_collision"]]
    if not collided:
        return
    log.info("\nNAME COLLISION - a name is not an identity, so this attribution is UNCONFIRMED:")
    for entry in collided:
        log.info(
            "  - %r exists in %d project(s) (%s); the survey's %d consumer edge(s) are attributed to "
            "EVERY one of them.",
            entry["name"],
            len(entry["name_collision"]),
            ", ".join(entry["name_collision"]),
            entry["survey_count"],
        )
    log.info("      Over-migrating a shared name is recoverable and keeps the order valid; refusing the")
    log.info("      match would orphan a real consumer, which is not. Disambiguate by LUID (re-run the")
    log.info("      survey so its dependencies resolve) before quoting per-project usage.")


def _print_phase2(order: list[dict[str, Any]]) -> None:
    """Print the workbook half of the sequence, each with the data sources it must follow."""
    log.info("\nPHASE 2 - migrate each workbook to a REPORT bound to the model built in phase 1.")
    log.info("          Do NOT rebuild the model per workbook; check first with:")
    log.info("          python scripts/published_datasource_registry.py --spec <spec.json>")
    if not any(step["kind"] == "workbook" for step in order):
        return
    log.info("\n          migration ORDER (every data source precedes every workbook that binds to it):")
    for i, step in enumerate(order, 1):
        if step["kind"] == "datasource":
            log.info("          %2d. [datasource] %s", i, step["name"])
        else:
            log.info("          %2d. [workbook]   %-38s after: %s", i, step["name"], ", ".join(step["requires"]))


def _orphan_heading(orphans: list[dict[str, Any]], survey: Survey | None) -> None:
    """Introduce the no-consumer list with only the claim the available evidence supports."""
    if survey and survey.complete:
        log.info("\nNOTE: %d published data source(s) have NO downstream workbooks in EITHER source:", len(orphans))
    elif survey:
        log.info(
            "\nNOTE: %d published data source(s) have no downstream workbooks in either source, but "
            "the survey is incomplete (%s):",
            len(orphans),
            "; ".join(survey.gaps),
        )
    else:
        log.info(
            "\nNOTE: %d published data source(s) have no downstream usage VISIBLE TO THE METADATA API:",
            len(orphans),
        )


def _print_orphans(plan: list[dict[str, Any]], survey: Survey | None) -> None:
    """Report data sources with no consumers - with only the claim the evidence actually supports.

    Without a survey the honest statement is "no downstream usage VISIBLE TO THE METADATA API",
    which is materially weaker than "abandoned" and is the one this tool can support: the Metadata
    API cannot see sqlproxy connections at all, so its silence says nothing about usage. The
    stronger claim requires a COMPLETE survey that also found no consumer, so the word itself must
    not appear anywhere on the no-survey path - `tests/test_tableau_lineage.py` greps for it.
    """
    orphans = [p for p in plan if p["downstream_count"] == 0]
    if not orphans:
        return
    _orphan_heading(orphans, survey)
    for entry in orphans:
        log.info("        - %s (%s)", entry["name"], entry["project"])
    if survey and survey.complete:
        log.info("      Both the Metadata API and the survey found no consumer - these may be abandoned.")
        log.info("      Confirm with the customer before migrating.")
    elif survey:
        log.info("      UNCONFIRMED: an incomplete survey cannot show a data source is unused - the workbook")
        log.info("      it failed to read may be the consumer. Close the survey's gaps before deciding.")
    else:
        log.info("      This is NOT evidence they are unused: the Metadata API does not see 'sqlproxy'")
        log.info("      (published data source) connections at all, so it cannot observe a consumer even")
        log.info("      when one exists. Re-run with --survey _assessment/estate_survey.json before")
        log.info("      drawing any conclusion about usage.")


def print_plan(plan: list[dict[str, Any]], survey: Survey | None = None) -> None:
    """Print the model-first migration plan, attributing every claim to the system that made it."""
    if not plan:
        log.info("No published data sources found on this site.")
        if survey:
            log.info("The survey found no published-datasource dependency either -> migrate workbook-by-workbook.")
        else:
            log.info("Every workbook embeds its own data source -> migrate workbook-by-workbook as usual.")
            log.info("Confirm with --survey: the Metadata API cannot see 'sqlproxy' connections.")
        return

    _print_header(plan, survey)
    _print_phase1(plan)
    _print_disagreements(plan, survey)
    _print_name_collisions(plan)
    _print_phase2(build_order(plan))
    _print_orphans(plan, survey)


def _env_config(env_path: Path | None = None) -> tuple[str, str, str, str]:
    """Read server/site/PAT from a `.env` file layered over the environment.

    Previously read ``os.environ`` directly under the name ``TABLEAU_SERVER``, so a `.env` written
    from our own ``.env.example`` (which documents ``TABLEAU_SERVER_URL``) failed here while working
    everywhere else -- on step 2 of the documented site path.
    """
    env = resolve_env(env_path)
    require(env)
    return (
        env["TABLEAU_SERVER_URL"].rstrip("/"),
        env.get("TABLEAU_SITE", ""),
        env["TABLEAU_PAT_NAME"],
        env["TABLEAU_PAT_SECRET"],
    )


def _build_parser() -> argparse.ArgumentParser:
    """Define the CLI."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", action="store_true", help="Print the model-first migration plan")
    parser.add_argument("--download", type=Path, help="Download every published data source (.tdsx) to this folder")
    parser.add_argument("--from-json", type=Path, help="Re-plan offline from a saved lineage response")
    parser.add_argument(
        "--survey",
        type=Path,
        help=(
            "estate_survey.py --json output. Its dependency edges OVERRIDE the Metadata API's, which "
            "is blind to 'sqlproxy' connections. Without this the plan is known-incomplete."
        ),
    )
    parser.add_argument(
        "--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials (default .env)"
    )
    parser.add_argument("--save-json", type=Path, help="Save the raw lineage response for offline re-planning")
    parser.add_argument(
        "--api-version", default=DEFAULT_API_VERSION, help=f"REST API version (default {DEFAULT_API_VERSION})"
    )
    return parser


def _resolve_survey(path: Path | None) -> tuple[Survey | None, bool]:
    """Load --survey if given. Returns (survey, ok); a survey that fails to load is FATAL.

    Continuing without it would silently produce the known-incomplete plan the operator explicitly
    asked not to have, under a heading that no longer warns about it.
    """
    if not path:
        log.warning("no --survey: the Metadata API cannot see 'sqlproxy' connections, so this plan may be incomplete")
        return None, True
    try:
        return load_survey(path), True
    except (OSError, ValueError, RuntimeError) as exc:
        log.error("--survey could not be read: %s", exc)
        return None, False


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-locals
    """CLI entry point."""
    args = _build_parser().parse_args(argv)

    survey, ok = _resolve_survey(args.survey)
    if not ok:
        return 1

    if args.from_json:
        payload = json.loads(args.from_json.read_text(encoding="utf-8"))
        print_plan(build_plan(payload.get("datasources", []), payload.get("site", ""), survey), survey)
        return 0

    server, site, pat_name, pat_secret = _env_config(args.env)
    try:
        session = sign_in(server, site, pat_name, pat_secret, args.api_version)
        log.info("signed in to %s (site '%s')", server, site or "<default>")
        datasources = fetch_lineage(session)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
        log.error(
            "Tableau API call failed: %s",
            redacted_note(str(exc), lambda text: redact(text, pat_secret), limit=400),
        )
        return 1

    log.info("found %d published data source(s)", len(datasources))
    if args.save_json:
        args.save_json.write_text(
            json.dumps(
                scrub_tree(
                    {"site": site, "datasources": datasources},
                    lambda text: redact(text, pat_name, pat_secret, session.token),
                )[0],
                indent=2,
            ),
            encoding="utf-8",
        )
        log.info("raw lineage saved to %s", args.save_json)

    plan = build_plan(datasources, site, survey)
    display_plan, _paths = scrub_tree(plan, lambda text: redact(text, pat_name, pat_secret, session.token))
    if args.plan or not args.download:
        print_plan(display_plan, survey)

    if args.download:
        log.info("\nDownloading %d data source(s) to %s ...", len(plan), args.download)
        for entry in plan:
            luid = _download_stem(entry["luid"], session)
            if luid is None:
                log.warning(
                    "  !!  %s has no valid LUID; refusing download",
                    redacted_note(
                        str(entry["name"]),
                        lambda text: redact(text, pat_name, pat_secret, session.token),
                        limit=400,
                    ),
                )
                continue
            dest = args.download / f"{luid}.tdsx"
            try:
                download_datasource(session, luid, dest)
                log.info("  OK  %s", dest)
            except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
                log.warning(
                    "  !!  %s failed: %s",
                    redacted_note(
                        str(entry["name"]),
                        lambda text: redact(text, pat_name, pat_secret, session.token),
                        limit=400,
                    ),
                    redacted_note(str(exc), lambda text: redact(text, pat_name, pat_secret, session.token), limit=400),
                )
        log.info("\nParse each with: python scripts/parse_tableau.py <file>.tdsx -o <spec>.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
