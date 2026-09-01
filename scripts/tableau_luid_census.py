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
⚠️ **Read-only, and one query.** Sign-in, then exactly one GraphQL request -- made by
``tableau_view_types.fetch_payload``, the SAME transport hop the shipped parser uses, over the ONE
hardened ``tableau_http`` round trip -- with no loop and no concurrency. The response body is never
written to disk and never printed.

⚠️ **It shares the shipped parser's seams rather than re-implementing them, and that is a correctness
property, not tidiness.** This script's entire claim is "here is what the shipped parser sees on this
site". While it did its own request and decode it skipped the byte ceiling, the status check and the
request-exception handling -- so on a valid 33 MB body it reported ``NOT-PRESENT``, exit 0,
``assessable: 1`` and "the shipped parser did not refuse", about a response ``view_types`` refuses
outright. A census that contradicts the parser it reports on is worse than no census.

Sharing a function is a fact about today's code; **parity is the property**, and
``test_the_census_and_the_shipped_parser_agree_on_the_same_bytes`` is what stops the two drifting
apart again.

⚠️ **Nothing this script PRINTS may contain non-ASCII**, and the marker is spelled ``[WARN]``
rather than a glyph for that reason. A default Windows console is CP1252: the unassessable path
reached the right verdict and then died delivering it -- ``UnicodeEncodeError``, **exit 1**, after
having already printed ``VERDICT: CANNOT-TELL``. So a caller trying to tell "unassessable" from
"clean" got neither; it got a crash, and the exit-2 guarantee this script exists to provide was
destroyed at the last line. ``test_no_runtime_string_can_break_a_cp1252_console`` is the gate; the
docstrings and comments here are free to use glyphs because nothing writes them to a stream.

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

#: Site-level tallies, and the assessability flag that travels with them.
SITE_BUCKETS = (
    "workbooks",
    "envelope_readable",
    "workbooks_with_an_unusable_collection",
    "workbooks_with_a_blank_luid",
    "blank_luids",
    "nodes",
    "assessable",
)

#: The handful of labels that are not census keys.
# ⚠️ `http status` is deliberately NOT here any more. The status is the transport hop's, and the
# census no longer performs its own request -- the refusal reason names it ("metadata api returned
# HTTP 403"), so nothing is lost and one duplicated code path is gone.
FIXED_LABELS = (
    "views typed by the shipped parser",
    "shipped parser refused the response",
)

#: ⚠️ THE CLOSED SET OF LABELS. `_emit` guarded only its `value`, so an arbitrary string reached
#: stdout through `label` -- measured, and worse, a RESPONSE-DERIVED label produced
#: `uncertified_sinks == []` because the taint gate had a blanket certification for the parameter.
#: A safety argument that covers one parameter of two is not a safety argument. Derived from this
#: module's own literals, so it cannot be widened by accident.
LABELS = frozenset(
    [f"{kind}_{bucket}" for kind in ("dashboards", "sheets") for bucket in BUCKETS]
    + list(SITE_BUCKETS)
    + list(FIXED_LABELS)
)

EXIT_OK = 0
EXIT_CANNOT_TELL = 2


def _emit(label: str, value: object) -> None:
    """Print one measurement. ⚠️ Refuses BOTH parameters, not just the value.

    ⚠️ The label check comes first, and its refusal deliberately does NOT echo the rejected label.
    Quoting it back would reintroduce the exact leak being refused, on the error path -- the same
    shape as the reflected-credential findings that produced `tableau_http`. Once a label has passed
    the allowlist it is one of this module's own literals, so the second refusal may name it.
    """
    if label not in LABELS:
        raise SystemExit("REFUSED to print a label this module did not author (see LABELS)")
    if not isinstance(value, PRINTABLE):
        raise SystemExit(f"REFUSED to print {label!r}: value is a {type(value).__name__}, not a count or a flag")
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


def _readable(workbook: dict) -> bool:
    """Both declared collections present AND actually lists."""
    return all(isinstance(workbook.get(key), list) for key in ("dashboards", "sheets"))


def census(payload: object) -> dict[str, int]:
    """Whole-site census over ARBITRARY decoded JSON. Returns counts only.

    ⚠️ Total by construction. It used to assume a dict-shaped envelope, so a top-level ``null``,
    list or string -- or ``"data": null`` -- raised before a single count existed. Every level is now
    read defensively and reported: an unreadable envelope yields all-zero counts WITH
    ``envelope_readable = 0``, because a zero that means "we could not look" must never be
    indistinguishable from a zero that means "we looked and found none".
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    workbooks = data.get("workbooks") if isinstance(data, dict) else None
    totals = {f"dashboards_{k}": 0 for k in BUCKETS} | {f"sheets_{k}": 0 for k in BUCKETS}
    totals["workbooks"] = len(workbooks) if isinstance(workbooks, list) else 0
    totals["envelope_readable"] = int(isinstance(workbooks, list))
    totals["workbooks_with_an_unusable_collection"] = 0
    totals["workbooks_with_a_blank_luid"] = 0
    for workbook in workbooks if isinstance(workbooks, list) else []:
        # ⚠️ Absent, null and "not a list" are one bucket because the operator's action is the same:
        # those workbooks were NOT counted, so any zero below is a partial answer. `dashboards: 7`
        # used to reach `for node in 7` and abort the run with an uncaught TypeError.
        if not isinstance(workbook, dict) or not _readable(workbook):
            totals["workbooks_with_an_unusable_collection"] += 1
            continue
        one = {"dashboards": classify(workbook["dashboards"]), "sheets": classify(workbook["sheets"])}
        for kind, counts in one.items():
            for bucket, value in counts.items():
                totals[f"{kind}_{bucket}"] += value
        if one["dashboards"]["blank"] or one["sheets"]["blank"]:
            totals["workbooks_with_a_blank_luid"] += 1
    totals["blank_luids"] = totals["dashboards_blank"] + totals["sheets_blank"]
    totals["nodes"] = totals["dashboards_total"] + totals["sheets_total"]
    return totals


def assessable(totals: dict[str, int], refused: bool) -> bool:
    """Whether the counts describe THE SITE, rather than the part of it we managed to read.

    ⚠️ THREE independent ways to be unassessable, OR-ed rather than assumed equivalent: the shared
    parser refused the response outright, the census could not read the envelope at all, or it found
    a workbook whose collections it could not read. The parser refuses on the FIRST problem, so today
    each of the last two implies the first -- they are computed from different data, and if they ever
    disagree the safe answer is "we did not assess this".

    ⚠️ Because they are implied, neither of the last two clauses can be killed by a mutation driven
    through `main()`; a clause no test can fail is worse than no clause, so both are pinned directly
    against this function with a `totals` a loader would not produce today
    (`test_each_unassessable_route_is_pinned_independently`), and the implication itself is asserted
    rather than assumed (`test_an_unreadable_envelope_always_makes_the_parser_refuse`). That is the
    same treatment an arithmetically-implied clause got in the #384 campaign.
    """
    return (
        not refused and totals.get("envelope_readable", 1) == 1 and totals["workbooks_with_an_unusable_collection"] == 0
    )


def verdict(totals: dict[str, int], refused: bool) -> str:
    """CONFIRMED / NOT-PRESENT / CANNOT-TELL. ⚠️ All three are useful; none is the "right" answer.

    ⚠️ `refused` is a REQUIRED argument, not a keyword with a safe-looking default. It was not a
    parameter at all, and the caller ignored the parser's refusal: a response carrying GraphQL
    `errors` beside one valid dashboard reported **NOT-PRESENT, exit 0** -- a permanent measurement
    artifact stating a site is clean when it was never assessed, and the omitted workbooks are
    exactly the ones that might have carried the blank luids being looked for. Had that run during
    the live verification, "no blank LUIDs" would have been recorded for a site that has 116.

    That is the most repeated defect class in this repository -- unassessable input collapsing into
    the clean bucket -- so the fix is to make it impossible to reach a clean verdict without having
    answered the question.
    """
    if not assessable(totals, refused):
        return "CANNOT-TELL"
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
    # ⚠️ ONE path for every outcome. Sign-in, the transport hop, the protocol check and the mapping
    # all land on the same `(payload, unavailable)`, so there is no early return that can skip the
    # assessability flag, the JSON, or the exit code. Three such early returns used to exist, and
    # each produced a DIFFERENT output shape from the one the guarantees were written for.
    try:
        session.sign_in()
        payload, refusal = tableau_view_types.fetch_payload(session)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        payload, refusal = None, f"sign-in failed: {type(exc).__name__}"

    # ⚠️ The SHARED seams, not re-implementations. `fetch_payload` is the transport hop `view_types`
    # itself uses -- byte ceiling, status check, strict decode, request-exception handling -- and
    # `parse_payload` applies the exact protocol and mapping rules, to a payload we already hold, at
    # no extra request. A second implementation of either is how the two entry points would drift,
    # and drifting is precisely what made the census contradict the parser it reports on.
    if refusal:
        mapping, unavailable = {}, refusal
    else:
        mapping, unavailable = tableau_view_types.parse_payload(payload)
    totals = census(payload)
    # ⚠️ Rides WITH the counts, into stdout and into --json, so a consumer cannot read `blank_luids:
    # 0` without also seeing whether that zero is a measurement of the site or of our own blindness.
    totals["assessable"] = int(assessable(totals, bool(unavailable)))
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

    answer = verdict(totals, bool(unavailable))
    print(f"\nVERDICT: {answer}")
    if answer == "CONFIRMED":
        print("Blank luids exist here, so this site DOES exercise the hidden-sheet case.")
    elif answer == "NOT-PRESENT":
        print("No blank luids today. The handling is still correct and documented, but this site")
        print("would not have exercised it -- do not cite this run as evidence that it cannot occur.")
    elif not totals["assessable"]:
        print("The response was refused, unreadable, or only partly readable, so these counts")
        print("describe what we could read, NOT the site. [WARN] Do not record this as evidence.")
    else:
        print("No sheet or dashboard nodes came back, so nothing here exercises the case either way.")
    # ⚠️ The exit code FOLLOWS the verdict. It did not: a run that printed CANNOT-TELL still exited
    # 0, so an automated caller checking only the status read it as a clean measurement.
    return EXIT_OK if answer != "CANNOT-TELL" else EXIT_CANNOT_TELL


if __name__ == "__main__":
    raise SystemExit(main())
