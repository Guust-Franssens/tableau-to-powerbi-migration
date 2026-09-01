"""
purpose: turn a list of captured view records into the oracle MANIFEST and the process exit code
usage:   imported by scripts/capture_tableau_oracle.py; no CLI of its own

Split out of ``capture_tableau_oracle`` for the same reason ``tableau_view_types`` and
``tableau_payload_facts`` were: that module was already at its 1200-line ceiling, and this is a
genuinely different layer. Everything here answers **"what does this evidence MEAN"** -- which views
are complete, which are blocked, which render legs are missing and why, and which exit code the
operator's shell should see. Nothing here talks to Tableau: the only thing it wants from a session is
two counters and a redactor, so it takes the session **duck-typed** (``reauth_count``,
``retry_count``, ``redact_text``) and the pair stays acyclic -- the same arrangement
``tableau_render_capability`` uses.

⚠️ This module is covered by ``tests/test_diagnostic_redaction.py``: it is where the manifest is
serialised, so it holds THE SINK (``scrub_tree`` immediately before ``json.dumps``) and every console
line that quotes a response-derived view name. Its entry points are declared in that gate's
``TAINT_SEEDS`` because taint propagation cannot cross a module boundary.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tableau_view_types
from tableau_env import redacted_note, scrub_tree

LOG = logging.getLogger("tableau-oracle")

# SVG export is gated by REST version. Below 3.29 the server refuses with this phrase and a 400 -- it
# does NOT silently fall back to PNG (measured on 3.21 / 3.24 / 3.28), so the sniff is safe. Both
# constants live HERE rather than beside the transport because both exist to CLASSIFY a failure and
# to name the knob that fixes it: `_capture_render` relabels the failure `unsupported_api_version`,
# and `_log_blocked_and_stale` prints the remedy. That is verdict vocabulary, not transport.
SVG_MIN_API_VERSION = "3.29"
SVG_VERSION_MARKER = "SVG export requires API version"


def log_progress(index: int, total: int, record: dict[str, Any], redactor=None) -> None:
    """One line per view: proof of rows captured, or a loud, classified failure.

    ⚠️ The console is the THIRD artifact, after the manifest and the files. A view NAME is response
    data -- a reflected token can arrive as one -- and this line used to slice it to 34 characters
    before anything scrubbed it, which is the round-4 defect at a boundary round 4 never looked at.
    CI keeps its logs, so "only the terminal" is not a mitigation.
    """
    data = record.get("data", {})
    name = redacted_note(record.get("view_name"), redactor, limit=34)
    status = data.get("status")
    if status == "ok":
        marks = []
        if data.get("reauths"):
            marks.append(f"re-auth x{data['reauths']}")
        if data.get("retries"):
            marks.append(f"retry x{data['retries']}")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        LOG.info(
            "  %2d/%d  %-34s %5d rows  %6.1fs%s", index, total, name, data["row_count"], data["elapsed_sec"], suffix
        )
    elif status == "source_credential":
        LOG.warning("  %2d/%d  %-34s NEEDS CREDENTIAL: %s", index, total, name, data.get("detail"))
    else:
        LOG.warning("  %2d/%d  %-34s FAILED (%s): %s", index, total, name, status, data.get("detail"))


def _render_statuses(record: dict[str, Any], requested: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """Status of every RENDER leg for this view, judged against what was actually ASKED FOR.

    An absent key normally means the leg was not requested, which must read as ``ok`` -- otherwise a
    plain data-only capture (no ``--images``, ``--svg`` or ``--pdf``) would count itself as failed.
    But when a leg WAS requested and is nevertheless absent, "absent" is a real failure: without
    ``requested`` the capture silently degrades to data-only and still reports success, which is
    exactly the exit-0-with-no-reference hole. Returning a tuple keeps the aggregate sets below
    reading the same legs, so adding a fourth output format cannot be counted by one and missed by
    the others.

    ⚠️ A render leg is absent for TWO different reasons and they must not collapse into one.
    ``capture_view`` returns before attempting any render once the **data** leg has failed -- all four
    routes come from the same VizQL render, so being refused three more times costs metered calls to
    learn nothing. Those renders are absent *because of their prerequisite*, and inventing an
    independent ``not_captured`` failure for each put a purely credential-blocked view into
    ``blocked`` **and** ``failed`` at once, where ``failed`` wins and the run exits 3 instead of the
    human-actionable 2. The prerequisite's own status is propagated instead, so one root cause is
    counted once -- and a genuinely broken data leg still yields failing renders.
    """
    data_status = (record.get("data") or {}).get("status")
    absent = "not_captured" if data_status in (None, "ok") else data_status
    statuses = []
    for kind, leg in (("png", "image"), ("svg", "svg"), ("pdf", "pdf")):
        if leg in record:
            statuses.append(record[leg].get("status"))
        elif kind in requested:
            statuses.append(absent)
        else:
            statuses.append("ok")
    return tuple(statuses)


def _partition(
    records: list[dict[str, Any]], requested: frozenset[str] = frozenset()
) -> dict[str, list[dict[str, Any]]]:
    """Split records into the four sets the manifest and the exit code both read.

    One function so the sets cannot drift apart: they must all consult the same render legs, and the
    bug this replaces was three list comprehensions where only two had been taught about a new leg.
    """
    ok = [r for r in records if r.get("data", {}).get("status") == "ok"]
    return {
        "ok": ok,
        "empty": [r for r in ok if r["data"]["row_count"] == 0],
        "complete": [
            r
            for r in records
            if r.get("data", {}).get("status") == "ok" and all(s == "ok" for s in _render_statuses(r, requested))
        ],
        "blocked": [
            r
            for r in records
            if "source_credential" in {r.get("data", {}).get("status"), *_render_statuses(r, requested)}
        ],
        "failed": [
            r
            for r in records
            if any(
                status not in {"ok", "source_credential"}
                for status in (r.get("data", {}).get("status"), *_render_statuses(r, requested))
            )
        ],
    }


@dataclass(frozen=True)
class CaptureRun:
    """Where and when one capture happened -- the provenance half of the manifest.

    Bundled because ``write_manifest`` needs all four together and nothing else needs any of them
    individually; passing them as loose positional parameters is what pushed the signature past the
    readable limit as soon as capability reporting was added.

    ``requested_renders`` is what the caller ASKED for, which is not the same as what came back --
    that gap is the point. ``reference_required`` records that ``--reference-best`` was used, so a run
    whose capability probe returned UNDETERMINED (and therefore requested nothing) is still judged
    against the operator's intent rather than against its own empty plan.

    ⚠️ ``session`` is DUCK-TYPED, deliberately: this module reads only ``reauth_count``,
    ``retry_count`` and ``redact_text`` off it. Annotating it as ``TableauSession`` would import the
    transport module that imports this one, and the cycle buys nothing -- three attributes are the
    whole contract, and ``tableau_render_capability`` takes its session the same way.
    """

    session: Any
    env: dict[str, str]
    out_dir: Path
    started: float
    requested_renders: frozenset[str] = frozenset()
    reference_required: bool = False


def write_manifest(
    records: list[dict[str, Any]],
    run: CaptureRun,
    capability_report: dict[str, Any] | None = None,
) -> int:
    """Write the manifest and return the process exit code.

    Codes: 0 all selected views captured, 1 partial non-credential failure, 2 credential-blocked,
    3 total non-credential failure, 4 no views selected, **5 a reference render was required but none
    was obtained**.

    Code 5 exists because the alternative is silence. With ``--reference-best`` and an UNDETERMINED
    probe, no render kind is requested at all, every view's data still succeeds, and the run would
    otherwise exit **0 having captured zero reference images** -- a caller gating on the exit code
    would read that as a complete capture.

    ⚠️ Code 5 must NOT swallow code 2. When every selected view is credential-blocked no render could
    have been produced by anything we control, and the one actionable instruction is "a human must
    reauthorize the source in Tableau" -- code 2. Code 5 there points the operator at our capability
    probe instead: the same debug-the-wrong-system cost that made 3 wrong for the same input. A
    *partial* block still yields 5, because the absence is then not explained by the credential.
    """
    sets = _partition(records, run.requested_renders)
    blocked, failed, complete = sets["blocked"], sets["failed"], sets["complete"]
    rendered = sum(1 for r in records if any(r.get(leg, {}).get("status") == "ok" for leg in ("image", "svg", "pdf")))
    # "Nothing rendered, and the credential explains ALL of it" -- the one case where an absent
    # reference is code 2's problem rather than code 5's.
    credential_only = rendered == 0 and bool(blocked) and len(blocked) == len(records)
    reference_missing = run.reference_required and rendered == 0 and not credential_only
    manifest = {
        "schema": "tableau-oracle/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": run.env["TABLEAU_SERVER_URL"],
        "site": run.env["TABLEAU_SITE"],
        "rest_api_version": run.env.get("TABLEAU_REST_API_VERSION"),
        "view_count": len(records),
        # #402: a per-run census of what the capture could DISCRIMINATE, so a consumer reads it once
        # instead of tallying `view_type` itself and guessing what a zero means. `unknown` is the
        # honest state when the Metadata API cannot be reached or does not expose `luid`; it is not
        # a synonym for worksheet.
        "view_types": tableau_view_types.census(records),
        "captured_complete": len(complete),
        "data_ok": len(sets["ok"]),
        "data_empty": len(sets["empty"]),
        "image_ok": sum(1 for r in records if r.get("image", {}).get("status") == "ok"),
        "svg_ok": sum(1 for r in records if r.get("svg", {}).get("status") == "ok"),
        "pdf_ok": sum(1 for r in records if r.get("pdf", {}).get("status") == "ok"),
        "requested_renders": sorted(run.requested_renders),
        "reference_required": run.reference_required,
        "reference_missing": reference_missing,
        # #403's surviving half: the manifest must STATE the grade of evidence it holds, so a
        # downstream validator reads it instead of inferring it from the fact that a file exists.
        "render_capability": capability_report,
        "credential_blocked": len(blocked),
        "failed": len(failed),
        "total_reauths": run.session.reauth_count,
        "total_retries": run.session.retry_count,
        "elapsed_sec": round(time.perf_counter() - run.started, 1),
        "views": records,
    }
    manifest_path = run.out_dir / "oracle-manifest.json"
    # THE SINK. Everything above this line is a source, and five review rounds went one source at a
    # time: `raw_get`, the 200-mismatch diagnostic, a case-folded Content-Type, a truncated body quote,
    # a `<detail>` capture group -- and then a field that was never a diagnostic at all, a successful
    # CSV's own header row copied into `data.columns`. Guarding sources one at a time cannot terminate,
    # because the next leak is by definition the one nobody enumerated. So the manifest is scrubbed as
    # a WHOLE, immediately before it is serialised, and every string in it is covered regardless of
    # how it got there.
    manifest, sink_hits = scrub_tree(manifest, run.session.redact_text)
    # Firing is itself a defect report: it means a source let something reach the sink. Recorded IN
    # the artifact, and named, so the finding survives the terminal scrollback.
    manifest["credential_scrubbed_at_sink"] = sink_hits
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if sink_hits:
        LOG.error(
            "\nThe manifest sink had to redact %d field(s) -- %s. A credential reached the manifest "
            "through a path that should have scrubbed it upstream; the file is safe, the code is not. "
            "Do NOT assume which credential: a reflected SESSION TOKEN can arrive as a view name from "
            "an authenticated metadata call, which the export seam never sees. Find the source before "
            "deciding this is the cosmetic PAT-name case.",
            len(sink_hits),
            ", ".join(sink_hits[:8]),
        )

    LOG.info(
        "\n%d/%d captured (%d empty), %d credential-blocked, %d failed, %d re-auth(s), %d retr(ies), %.0fs -> %s",
        len(complete),
        len(records),
        len(sets["empty"]),
        len(blocked),
        len(failed),
        run.session.reauth_count,
        run.session.retry_count,
        manifest["elapsed_sec"],
        manifest_path,
    )
    _log_blocked_and_stale(records, blocked, capability_report, run.session.redact_text)
    if reference_missing:
        LOG.error(
            "\nA reference render was REQUIRED (--reference-best) but NONE was captured across %d "
            "view(s). The capability probe did not settle on a tier, so nothing was requested. This "
            "run has data only and must not be treated as a complete capture; re-run once the probe "
            "can reach a renderable view, or name a tier explicitly with --images/--svg/--pdf.",
            len(records),
        )
    elif run.reference_required and credential_only:
        # Deliberately NOT code 5: nothing rendered, but the cause is entirely upstream of us and the
        # blocked list above already names the whole fix.
        LOG.error(
            "\nA reference render was REQUIRED (--reference-best) and none was captured, because ALL "
            "%d selected view(s) are credential-blocked on the Tableau side. That is exit code 2, not "
            "5: no render route could have succeeded, and re-probing our capability ladder cannot "
            "help. Reauthorize the source(s) named above in Tableau and re-run.",
            len(records),
        )
    if not records:
        return 4
    if reference_missing:
        return 5
    if failed:
        return 1 if complete else 3
    return 2 if blocked else 0


def _log_blocked_and_stale(
    records: list[dict[str, Any]], blocked: list[dict[str, Any]], capability_report: dict[str, Any] | None, redactor
) -> None:
    """The two loud, differently-actionable warning classes, plus the probe's own warnings.

    Every response-derived name goes through the chokepoint: these lines run BEFORE `scrub_tree` has
    been applied to anything (it returns a scrubbed copy, it does not mutate `records`), so the
    console would otherwise print the one thing the manifest was careful not to.
    """
    if blocked:
        LOG.warning(
            "\n%d view(s) need a credential ON THE TABLEAU SIDE - no retry can fix this, a human must "
            "reauthorize the source in Tableau:",
            len(blocked),
        )
        for record in blocked:
            detail = next(
                (
                    record.get(leg, {}).get("detail")
                    for leg in ("data", "image", "svg", "pdf")
                    if record.get(leg, {}).get("detail")
                ),
                None,
            )
            LOG.warning(
                "  - %s (%s): %s",
                redacted_note(record.get("view_name"), redactor, limit=60),
                redacted_note(record.get("workbook_name"), redactor, limit=60),
                redacted_note(detail, redactor, limit=200),
            )
    stale_api = [r for r in records if r.get("svg", {}).get("status") == "unsupported_api_version"]
    if stale_api:
        # Loud and separate from `blocked`: this one is fixed by an .env line, not by a human
        # reauthorizing a data source in Tableau, and conflating the two sends the reader hunting
        # in the wrong system.
        LOG.warning(
            "\n%d view(s) could not produce SVG: this site's REST API version is below %s. "
            "Set TABLEAU_REST_API_VERSION=%s in .env and re-run; the PNG and PDF captures are "
            "unaffected (they reach back to API 2.5 and 2.8 respectively).",
            len(stale_api),
            SVG_MIN_API_VERSION,
            SVG_MIN_API_VERSION,
        )
    for warning in (capability_report or {}).get("warnings", []):
        LOG.warning("! %s", warning)
