"""
purpose: census a Tableau site's Metadata API for BLANK view LUIDs, so the assumption that
         `tableau_view_types` rests on stays a MEASUREMENT rather than a documentation claim (#402).
usage:   python scripts/tableau_luid_census.py [--env <path>] [--json <path>]

Why this exists
---------------
`tableau_view_types` refuses a malformed Metadata API answer whole, and that is right -- but a
blank ``luid`` is not malformed. Tableau documents ``Sheet.luid: String!`` as *"Blank if worksheet is
hidden in Workbook"*, and REST ``/views`` omits hidden sheets entirely, so a blank luid names no
capturable view. Treating it as garbage refuses the whole response, and because this query scans
EVERY workbook on the site, one hidden sheet in one unrelated workbook turns dashboard/worksheet
typing off for **every captured view on the site**.

That was not a hypothetical. Measured against our Tableau Cloud trial on 2026-09-01 (REST 3.29
requested): 48 workbooks, 60 dashboards, 416 sheets -- and **116 blank sheet LUIDs across 5
workbooks**, 27.9% of all sheets. The pre-fix rule typed **0** views on that exact response; the
shipped parser types **360**.

Documentation said it; nothing had measured it. This script is how it stays measured -- run it
against any site and the census answers, in counts, whether that site would exercise the case.

Safety
------
⚠️ **Read-only, and one query.** Sign-in, then exactly one GraphQL request through the session's own
``_request`` -- the ONE hardened ``tableau_http`` round trip -- with no loop and no concurrency. The
response body is never written to disk and never printed.

⚠️ **Counts and flags only, enforced rather than promised.** :func:`_emit` refuses to print anything
that is not an ``int``, ``bool`` or ``None``, so a careless edit fails loudly instead of leaking a
workbook name, a sheet name or a LUID. The census is built from SHAPES -- type, emptiness, regex
match -- and never from an identity.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_env  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_view_types  # noqa: E402  # pylint: disable=wrong-import-position

#: Everything this module is willing to put on stdout. A str is deliberately NOT here.
PRINTABLE = (int, bool, type(None))

#: Per-node-list shape buckets. Order is the reporting order.
BUCKETS = ("total", "missing_key", "non_string", "blank", "uuid", "non_uuid_non_blank")

EXIT_OK = 0
EXIT_CANNOT_TELL = 2


def _emit(label: str, value: object) -> None:
    """Print one measurement. ⚠️ Refuses anything that could carry an identifier."""
    if not isinstance(value, PRINTABLE):
        raise SystemExit(f"REFUSED to print {label!r}: {type(value).__name__} is not a count or a flag")
    print(f"  {label:44s} {value}")


def classify(nodes: list) -> dict[str, int]:
    """Shape census for one node list. Reads TYPES and EMPTINESS only, never a value."""
    out = dict.fromkeys(BUCKETS, 0)
    for node in nodes:
        out["total"] += 1
        if not isinstance(node, dict) or "luid" not in node:
            out["missing_key" if isinstance(node, dict) else "non_string"] += 1
            continue
        luid = node["luid"]
        if not isinstance(luid, str):
            out["non_string"] += 1
        elif not luid.strip():
            out["blank"] += 1
        elif tableau_view_types.is_luid(luid):
            out["uuid"] += 1
        else:
            out["non_uuid_non_blank"] += 1
    return out


def census(payload: dict) -> dict[str, int]:
    """Whole-site census. Returns counts only -- it is what gets printed and optionally serialised."""
    workbooks = payload.get("data", {}).get("workbooks", [])
    totals = {f"dashboards_{k}": 0 for k in BUCKETS} | {f"sheets_{k}": 0 for k in BUCKETS}
    totals["workbooks"] = len(workbooks) if isinstance(workbooks, list) else 0
    totals["workbooks_missing_a_collection"] = 0
    totals["workbooks_with_a_blank_luid"] = 0
    for workbook in workbooks if isinstance(workbooks, list) else []:
        if not isinstance(workbook, dict) or "dashboards" not in workbook or "sheets" not in workbook:
            totals["workbooks_missing_a_collection"] += 1
            continue
        one = {"dashboards": classify(workbook["dashboards"] or []), "sheets": classify(workbook["sheets"] or [])}
        for kind, counts in one.items():
            for bucket, value in counts.items():
                totals[f"{kind}_{bucket}"] += value
        if one["dashboards"]["blank"] or one["sheets"]["blank"]:
            totals["workbooks_with_a_blank_luid"] += 1
    totals["blank_luids"] = totals["dashboards_blank"] + totals["sheets_blank"]
    totals["nodes"] = totals["dashboards_total"] + totals["sheets_total"]
    return totals


def verdict(totals: dict[str, int]) -> str:
    """CONFIRMED / NOT-PRESENT / CANNOT-TELL. ⚠️ All three are useful; none is the "right" answer."""
    if totals["blank_luids"]:
        return "CONFIRMED"
    if not totals["nodes"]:
        return "CANNOT-TELL"
    return "NOT-PRESENT"


def _session(env_path: Path) -> oracle.TableauSession:
    """Build the session from `.env`, with a ONE-attempt retry policy.

    ⚠️ `max_attempts=1` is deliberate. This script exists to answer one read-only question, and a
    retry loop against a credentialed site-wide query is exactly the shape the repository's
    "never block silently on an external system" rule exists to prevent. One try, then a verdict.
    """
    env = tableau_env.resolve_env(env_path)
    tableau_env.require(env, "TABLEAU_SERVER_URL", "TABLEAU_SITE", "TABLEAU_PAT_NAME")
    return oracle.TableauSession(
        oracle.SiteCredentials(
            base=tableau_env.server_url(env),
            site=env["TABLEAU_SITE"],
            pat_name=env["TABLEAU_PAT_NAME"],
            pat_secret=tableau_env.pat_secret(env),
            version=env.get("TABLEAU_REST_API_VERSION", "3.29"),
        ),
        oracle.RetryPolicy(max_attempts=1, budget_sec=60),
    )


def main(argv: list[str] | None = None) -> int:
    """Sign in, ask ONCE, report counts, and name a verdict.

    Exit 0 means a census was produced -- including the NOT-PRESENT verdict, which is a real answer
    about the site. Exit 2 means CANNOT-TELL: authentication failed, the API did not answer 200, the
    body was unreadable, or it carried GraphQL errors. ⚠️ The two are kept apart on purpose. "This
    site has no blank luids" and "we could not find out" are different claims, and collapsing them is
    how a vacuous run gets quoted later as a clean result.
    """
    parser = argparse.ArgumentParser(description="Census a Tableau site's Metadata API for blank view LUIDs.")
    parser.add_argument("--env", default=".env", help="path to the .env holding the Tableau credentials")
    parser.add_argument("--json", dest="json_out", help="write the COUNTS (no identifiers) to this path")
    args = parser.parse_args(argv)

    session = _session(Path(args.env))
    print("=== blank-luid census (read-only, one query) ===")
    try:
        session.sign_in()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"  sign-in failed: {type(exc).__name__}")
        print("\nVERDICT: CANNOT-TELL (could not authenticate)")
        return EXIT_CANNOT_TELL

    status, body, _ = session._request(  # pylint: disable=protected-access
        "POST", "/graphql", body={"query": tableau_view_types.VIEW_TYPE_QUERY}, api="metadata"
    )
    _emit("http status", status)
    if status != 200:
        print("\nVERDICT: CANNOT-TELL (metadata api did not answer 200)")
        return EXIT_CANNOT_TELL

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"  response was not usable JSON: {type(exc).__name__}")
        print("\nVERDICT: CANNOT-TELL (unreadable response)")
        return EXIT_CANNOT_TELL
    del body  # ⚠️ never persisted, never printed

    # ⚠️ The SHARED seam, not a re-implementation: `parse_payload` applies the exact protocol and
    # mapping rules `view_types` would, to a payload we already hold, at no extra request. A second
    # implementation of "what does this response mean" is how the two would drift apart.
    mapping, unavailable = tableau_view_types.parse_payload(payload)
    totals = census(payload)
    for key in sorted(totals):
        _emit(key, totals[key])

    # A future regression in blank handling shows up here as `refused=True` with zero typed views on
    # a site whose census above says it is healthy.
    _emit("views typed by the shipped parser", len(mapping))
    _emit("shipped parser refused the response", bool(unavailable))
    if unavailable:
        print(f"  refusal reason: {unavailable}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(totals, indent=2, sort_keys=True), encoding="utf-8")

    answer = verdict(totals)
    print(f"\nVERDICT: {answer}")
    if answer == "CONFIRMED":
        print("Blank luids exist here, so this site DOES exercise the hidden-sheet case.")
    elif answer == "NOT-PRESENT":
        print("No blank luids today. The handling is still correct and documented, but this site")
        print("would not have exercised it -- do not cite this run as evidence that it cannot occur.")
    else:
        print("No sheet or dashboard nodes came back, so nothing here exercises the case either way.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
