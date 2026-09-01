"""
purpose: tell a Tableau DASHBOARD from a WORKSHEET, by LUID, via the Metadata API (issue #402)
usage:   import tableau_view_types; mapping, why = tableau_view_types.view_types(session)

Tableau REST returns dashboards and worksheets BOTH under ``/views``, with no field distinguishing
them. So a captured render could be a whole dashboard composite or one chart, and nothing downstream
could say which. That matters because a Power BI page is rebuilt from a Tableau **dashboard**, and a
dashboard routinely shares its name with its principal worksheet -- so matching on NAME silently
accepts a single visual as evidence for a whole page. The false match is the normal case for an
ordinary workbook, not an edge case.

Two sibling components in this repo already discriminate correctly, which is why this is a gap in
the capture path specifically rather than a hard problem:

* ``assess_estate.py`` queries the Metadata API for ``sheets { name }`` and ``dashboards { name }``
  -- separate GraphQL types.
* the deterministic engine's ``twb_to_pbir.py`` keeps a ``placed`` set, so a worksheet already laid
  onto a dashboard never becomes its own page; every other worksheet gets ``page-ws-<name>``.

⚠️ **This module fails closed and never guesses.** Every failure path -- Metadata API disabled, an
older schema with no ``luid``, a transport error, a GraphQL ``errors`` block -- returns an EMPTY
mapping plus a stated reason, so every view records ``unknown``. There is deliberately **no
name-based fallback**: matching on the view name is the exact join this replaces. An absent type is
recoverable because a consumer can see it is absent; a wrong type would be believed.

It is split out of ``capture_tableau_oracle`` rather than added to it because that module was already
at its line ceiling, and because this is a self-contained question with its own failure modes -- the
same reason ``harvest_gap_shapes``/``harvest_gap_trees`` were split from ``harvest_engine_gaps``.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The same closed allowlist `capture_tableau_oracle.artifact_stem` uses for filenames, for the same
# reason: a value that has been PROVED to be a UUID cannot carry a credential, so it needs no
# redaction downstream. Validating here means the mapping's keys are shape-verified rather than
# merely trusted, and a Metadata API that returned something else is refused rather than indexed by.
_LUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# `luid` is not queried anywhere else in this repo, so an older server that does not expose it is a
# capability question rather than a bug. The GraphQL error it raises is reported verbatim.
VIEW_TYPE_QUERY = """
{ workbooks { dashboards { luid } sheets { luid } } }
"""

DASHBOARD = "dashboard"
WORKSHEET = "worksheet"
UNKNOWN = "unknown"

#: The key this module writes onto each view dict, read later when the record is built. Double
#: underscore so it cannot collide with a field Tableau's REST response actually carries.
VIEW_TYPE_KEY = "__view_type"


def view_types(session: Any) -> tuple[dict[str, str], str | None]:
    """Map view LUID -> ``dashboard``/``worksheet``. Returns ``(mapping, unavailable_reason)``.

    The request goes through the session's own ``_request`` with ``api="metadata"``, which composes
    the unversioned ``/api/metadata/graphql`` endpoint and travels the ONE hardened HTTP round trip
    (``tableau_http``). It deliberately does not open a client of its own: three hand-rolled HTTP
    paths in this codebase each leaked a reflected credential in a different review round, and the
    fix was to stop having more than one.

    ⚠️ **The reason string never carries server-controlled text.** The GraphQL ``errors[].message``
    is attacker-influenceable -- measured: a one-request server that reflects the inbound
    ``X-Tableau-Auth`` header into ``errors[0].message`` put a live session token into this
    function's warning. Only the *shape* of the failure is reported. That is the same deletion, for
    the same reason, as the HTTP reason phrase in ``tableau_render_capability``: detecting a
    credential FRAGMENT is not solvable, so the defence is to emit fewer server-controlled strings.

    ⚠️ **A partial answer is refused whole.** An earlier version skipped malformed nodes and trusted
    their valid siblings, which produced a mapping that typed some views and silently left others
    ``unknown`` -- indistinguishable, downstream, from a run where those views genuinely had no type.
    """
    try:
        status, body, _ = session._request(  # pylint: disable=protected-access
            "POST", "/graphql", body={"query": VIEW_TYPE_QUERY}, api="metadata"
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {}, f"metadata api call failed: {type(exc).__name__}"
    if status != 200:
        return {}, f"metadata api returned HTTP {status}"
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, f"metadata api response was not JSON: {type(exc).__name__}"
    if not isinstance(payload, dict):
        return {}, f"metadata api response was {type(payload).__name__}, not an object"
    if payload.get("errors"):
        # A GraphQL 200 can carry `errors` BESIDE usable `data` -- notably FieldUndefined when `luid`
        # is absent from this server's schema. The count is ours; the message is the server's and is
        # deliberately not reported (see the docstring).
        count = len(payload["errors"]) if isinstance(payload["errors"], list) else 1
        return {}, f"metadata api returned {count} graphql error(s); response refused"
    return _mapping_from(payload)


def _mapping_from(payload: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """Build the LUID -> kind mapping, refusing the WHOLE answer on any malformed part.

    Every ``return {}, reason`` here is a refusal of the entire response, never of one node. Skipping
    a bad node and keeping its siblings is the failure this function is shaped to prevent: it yields
    a confident mapping built from an answer we already know is not intact.

    ⚠️ **The refused return is a fresh ``{}``, never ``mapping``.** By the time a workbook part is
    found malformed, earlier workbooks have already folded real entries in -- and returning those
    would be precisely the partial answer this whole function exists to refuse, just arrived at from
    the other direction. Pinned by the fixtures that poison one node BESIDE a valid sibling.

    Split from :func:`_fold_workbook` on a real seam -- the response ENVELOPE here, ONE workbook
    there -- rather than to satisfy a linter. `R0911` fired at nine returns, and the honest reading is
    that one function was carrying two levels of validation; suppressing it, or hiding the same nine
    exits behind an internal exception, would have left that unchanged.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}, f"metadata api `data` was {type(data).__name__}, not an object"
    workbooks = data.get("workbooks")
    if not isinstance(workbooks, list):
        return {}, f"metadata api `workbooks` was {type(workbooks).__name__}, not a list"
    mapping: dict[str, str] = {}
    for workbook in workbooks:
        refused = _fold_workbook(workbook, mapping)
        if refused:
            return {}, refused
    if not mapping:
        return {}, "metadata api returned no dashboards or sheets carrying a luid"
    return mapping, None


def _fold_workbook(workbook: Any, mapping: dict[str, str]) -> str | None:
    """Fold ONE workbook's dashboards and sheets into ``mapping``. Returns a refusal reason, or None.

    ⚠️ It accumulates IN PLACE, and that is load-bearing rather than convenient: the contradiction
    check needs the mapping built so far across ALL workbooks, because a LUID can be reported as a
    dashboard in one workbook and a sheet in another. Returning a per-workbook dict and merging would
    push that check to the merge site, where the two kinds have already been separated.

    A partially-filled ``mapping`` on refusal is harmless because the caller discards it -- see
    :func:`_mapping_from`, which returns a fresh ``{}``.
    """
    if not isinstance(workbook, dict):
        return f"a workbook node was {type(workbook).__name__}, not an object; response refused"
    for key, kind in (("dashboards", DASHBOARD), ("sheets", WORKSHEET)):
        nodes = workbook.get(key)
        if nodes is None:
            continue
        if not isinstance(nodes, list):
            return f"`{key}` was {type(nodes).__name__}, not a list; response refused"
        for node in nodes:
            if not isinstance(node, dict):
                return f"a `{key}` node was {type(node).__name__}, not an object; response refused"
            luid = node.get("luid")
            if not isinstance(luid, str) or not _LUID_RE.match(luid.strip()):
                # Shape-verified, not merely non-empty: a proved UUID cannot carry a credential,
                # which is what lets the mapping's keys travel without redaction.
                return f"a `{key}` node carried a {type(luid).__name__} that is not a luid; response refused"
            key_luid = luid.strip().lower()
            # ⚠️ A LUID naming BOTH a dashboard and a sheet is contradictory, not a last-wins
            # tiebreak. Overwriting would have silently picked whichever the server listed second.
            if mapping.get(key_luid, kind) != kind:
                return "the same luid was reported as both a dashboard and a worksheet; response refused"
            mapping[key_luid] = kind
    return None


def stamp(views: list[dict[str, Any]], mapping: dict[str, str]) -> None:
    """Write the resolved type onto each view dict, in place.

    Enriching the view at the source keeps ``capture_view``'s signature unchanged: the type is a
    property of the view, not another run-level argument every call site has to remember to pass.
    """
    for view in views:
        luid = str(view.get("id") or "").strip().lower()
        view[VIEW_TYPE_KEY] = mapping.get(luid, UNKNOWN)


def resolve_and_stamp(session: Any, views: list[dict[str, Any]], log: Any) -> str | None:
    """Resolve types once for the run, stamp them, and warn if the run cannot discriminate.

    One call so the caller keeps no intermediate state: the failure reason is *reported here* rather
    than returned for the caller to remember to check, which is how a "cannot establish" quietly
    becomes an unexamined variable. Returns the reason anyway, for a caller that wants to record it.
    """
    mapping, unavailable = view_types(session)
    if unavailable:
        log.warning(
            "view type is UNKNOWN for every view in this run (%s). A consumer cannot tell a dashboard "
            "composite from a single worksheet; treat page-level visual evidence as unestablished.",
            unavailable,
        )
    stamp(views, mapping)
    return unavailable


def census(records: list[dict[str, Any]]) -> dict[str, int]:
    """Per-run tally, so a consumer reads one number instead of re-deriving it.

    A zero must be legible as "none of that kind", which is why all three keys are always present --
    a missing ``dashboard`` key would be indistinguishable from a run that could not tell.
    """
    return {kind: sum(1 for r in records if r.get("view_type") == kind) for kind in (DASHBOARD, WORKSHEET, UNKNOWN)}
