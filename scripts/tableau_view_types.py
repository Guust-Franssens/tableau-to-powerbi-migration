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
from typing import Any

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
    if payload.get("errors"):
        # ⚠️ A GraphQL 200 can carry `errors` BESIDE usable `data` -- notably FieldUndefined when
        # `luid` is absent from this server's schema. Partial data must not be treated as an answer:
        # it would type some views and leave others silently unknown, which reads as a complete run.
        first = (payload["errors"] or [{}])[0]
        return {}, f"metadata api error: {str(first.get('message') or 'unspecified')[:120]}"
    mapping: dict[str, str] = {}
    for workbook in (payload.get("data") or {}).get("workbooks") or []:
        if not isinstance(workbook, dict):
            continue
        for key, kind in (("dashboards", DASHBOARD), ("sheets", WORKSHEET)):
            for node in workbook.get(key) or []:
                luid = node.get("luid") if isinstance(node, dict) else None
                if isinstance(luid, str) and luid.strip():
                    mapping[luid.strip().lower()] = kind
    if not mapping:
        return {}, "metadata api returned no dashboards or sheets carrying a luid"
    return mapping, None


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
