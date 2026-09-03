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
from tableau_payload_facts import (
    CSV_CERTIFIED,
    CSV_CONTENT_TYPE_ABSENT,
    CSV_CONTENT_TYPE_UNSPECIFIC,
    CSV_REFUSALS,
    CSV_UNCERTIFIED,
    CSV_UNCERTIFIED_DETAIL,
)

LOG = logging.getLogger("tableau-oracle")

# SVG export is gated by REST version. Below 3.29 the server refuses with this phrase and a 400 -- it
# does NOT silently fall back to PNG (measured on 3.21 / 3.24 / 3.28), so the sniff is safe. Both
# constants live HERE rather than beside the transport because both exist to CLASSIFY a failure and
# to name the knob that fixes it: `_capture_render` relabels the failure `unsupported_api_version`,
# and `_log_blocked_and_stale` prints the remedy. That is verdict vocabulary, not transport.
SVG_MIN_API_VERSION = "3.29"
SVG_VERSION_MARKER = "SVG export requires API version"

# What a per-tier status reads as when a leg carries no record at all. A module constant rather than
# an inline literal, and that is not style: `tests/test_diagnostic_redaction.py` keys its
# certifications on `ast.unparse` output, and unparse renders an f-string containing a NESTED string
# literal differently across Python versions -- `f"{k}={s or 'absent'}"` on 3.13 versus
# `f'{k}={s or 'absent'}'` on the CI interpreter (PEP 701 quote reuse). The certification matched
# locally and was simultaneously stale AND missing on CI. Interpolating only NAMES keeps the
# unparsed form identical everywhere.
ABSENT_LEG = "absent"

# A render leg that was REQUESTED and deliberately not asked for, because a sibling leg drawn from the
# same VizQL render had just failed. Distinct from every failure status: nothing was learned about
# this tier, so it must not read as "this tier is broken" -- and distinct from an ABSENT key, which
# now means one thing only, "not requested".
NOT_ATTEMPTED = "not_attempted"

# Tier -> the record key it is written under. `png` is spelled `image` for historical reasons: it was
# the only render there was, and renaming the key now would orphan every manifest already captured.
_LEG_KEY = {"png": "image", "svg": "svg", "pdf": "pdf"}

# A view whose `/data` export SUCCEEDED and carried no data rows (#471). A per-view flag and NOT a
# status: the HTTP call genuinely succeeded, and `status` drives the exit code plus the
# `blocked`/`failed` partitions, so overloading it would turn a legible run into a failed one. The
# fact is a DIAGNOSTIC -- an otherwise-clean run still exits 0 -- exactly as `render_unestablished`
# is a diagnostic rather than a failure.
FLAG_DATA_EMPTY = "data_empty"

# ...and WHY it is empty, as far as the capture can honestly tell. Two values, because there are
# three real causes and the payload can only separate one of them:
#
#   * a real query that returned nothing -- the field defect (#471): a relative-date filter whose
#     window has no data yet, or a required filter defaulting to None;
#   * a sheet with no underlying query at all -- a glossary/reference sheet, where an empty `/data`
#     is CORRECT behaviour and counting it as a defect overstates the finding (14 -> 12 on the
#     reporting site) and trains the reader to discount the whole list.
#
# ⚠️ **The tempting discriminators do not work, and this was MEASURED rather than reasoned.** Over
# `summarise_csv`: a 0-byte body (glossary) and a 2-byte CRLF body (blank data) BOTH land on
# `row_count=0, columns=[]`, differing only in `bytes`. "Glossary means 0 bytes" is one site's
# observation at n=14, not a documented Tableau contract, so the byte count is deliberately NOT
# consulted. What a payload CAN establish is that a header came back at all: a CSV naming its
# columns proves a query ran and returned a shape, which no fieldless sheet can produce. The
# converse does not hold -- a real query can also return nothing at all -- so an absent header is
# reported as UNCLASSIFIABLE rather than assigned to either cause. Resolving it needs field-level
# metadata (`Sheet.sheetFieldInstances`, or the workbook XML `parse_tableau.py` reads) that this
# capture does not hold; see the module note in :func:`empty_classification`.
EMPTY_QUERY_NO_ROWS = "empty_query_no_rows"
EMPTY_CANNOT_CLASSIFY = "empty_cannot_classify"

# A view whose `/data` export succeeded and whose evidence CANNOT BE ASSESSED AT ALL -- the third
# state, and the one this layer used to be missing. Before it, `empty_classification` returned `None`
# for a record with no `row_count`, and every consumer read that `None` as "not empty": the view was
# reported successful, evidence-complete, unflagged and unnamed, indistinguishable from one that
# returned 900,000 rows. That is the #471 defect one level up -- "a zero-row capture reads as ok"
# fixed, "an UNASSESSABLE capture reads as ok" introduced -- and it is strictly worse than the
# `KeyError` it replaced, because a crash is fail-closed and a clean bucket is not.
#
# ⚠️ It is a FLAG and not a status, for the same reason `data_empty` is: the transport genuinely
# succeeded. Collapsing it into `status` would destroy a real distinction (the HTTP call worked) and
# would move the exit code for a run whose only fault is that we cannot vouch for what we hold.
FLAG_DATA_UNASSESSABLE = "data_unassessable"

# The default reason: a record whose data leg recorded no row count at all. An OLDER manifest, or a
# `certification` this module does not recognise -- either way nothing measured the rows, so nothing
# may be claimed about them.
UNASSESSABLE_NO_ROW_COUNT = "row_count_unrecorded"

# Reasons a CURRENT capture can supply, from `certify_csv`'s closed vocabulary. Only the UNCERTIFIED
# verdicts (`content_type_absent`, `content_type_unspecific`) can reach a `status: ok` record --
# every refusal is recorded `format_mismatch` at capture time and never claims to be a successful
# data leg -- but the whole set is accepted here so a record written by a newer capture is named
# honestly rather than flattened.
UNASSESSABLE_REASONS = frozenset({UNASSESSABLE_NO_ROW_COUNT}) | CSV_UNCERTIFIED | CSV_REFUSALS

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# WHERE a data leg's bytes are named, which is the difference between evidence and a retained blob.
#
# ⚠️ **This is a STRUCTURAL rule, not a flag, and that is the whole point of it (#480 round 2).**
# Round 1 recorded an uncertified capture honestly -- `certification`, `flags`, no `row_count`, its
# own counted-and-named list -- and then left the bytes at `data/<luid>.csv` under the same `path`
# key a certified capture uses. Every consumer that wanted a number therefore kept reading it: the
# review found `build_reconcile_items.build()` emitting `tableau_value: 10.0` from a record whose own
# flags said `data_unassessable`, and `check_unit` counting the same record as numeric evidence.
# Patching each consumer to check the flag cannot terminate -- the next consumer is by definition the
# one nobody enumerated -- so the invalid state is made UNREPRESENTABLE instead: an uncertified body
# is never written under `data/`, never named `.csv`, and never named by `path`.
#
# The consequence is that a consumer needs no new knowledge to be safe. `build_reconcile_items`
# requires `status == "ok" and data["path"]`, `check_unit` requires the same, `package_unit._copy_leg`
# and `group_oracle_by_workbook.copy_view_files` both key on `path` -- and all four skip an
# uncertified capture without a line of change, because there is nothing there to read.
#
#: The key a CERTIFIED data leg names its file with. Unchanged, and named here so the pair reads as
#: one decision.
EVIDENCE_PATH_KEY = "path"
#: The key UNCERTIFIED retained bytes are named with instead. A different key on purpose: a consumer
#: that reads `path` gets nothing, and a consumer that wants the bytes for forensics has to ask for
#: them by a name that says what they are.
RETAINED_PATH_KEY = "retained_path"
#: Beside it, an authored sentence saying why the bytes are not evidence -- so a manifest answers the
#: question without the reader having to know this module's vocabulary.
EVIDENCE_WITHHELD_KEY = "evidence_withheld"
#: The subdirectory uncertified bytes are retained in, and the suffix they carry. NOT `data/` and NOT
#: `.csv`: a consumer that lists or globs the capture folder must not find them either, which is the
#: same fail-open one level down from the manifest.
RETAINED_DIR = "unassessable"
RETAINED_SUFFIX = ".bin"
#: The sentence used when a record gives no verdict of its own -- a manifest written before
#: `certification` existed, whose rows nothing measured.
RETAINED_DETAIL_DEFAULT = (
    "this data leg records no row count and no CSV certification, so nothing established its bytes "
    "as data. They are retained for inspection and are NOT placed where a numeric-oracle consumer "
    "reads evidence; re-capture to obtain assessable numbers."
)


def data_leg_fields(out_dir: Path, stem: str, certification: str) -> tuple[Path, dict[str, str]]:
    """``(file to write, the manifest fields that NAME it)`` -- the ONE place that pair is decided.

    Both halves move together or the rule is only half applied: bytes under ``data/*.csv`` named by
    ``retained_path`` are still discoverable by anything globbing the capture folder, and bytes in
    ``unassessable/`` named by ``path`` are still read by every consumer that asks for a path. The
    withheld-evidence sentence ships in the same breath, so a record can never say "not evidence" in
    one field and offer a readable ``path`` in another. Kept here, not at the call site, so a second
    writer cannot invent its own convention.

    The returned paths are relative POSIX strings built from module literals plus ``stem``, which the
    caller derives from a validated LUID -- nothing a server sent reaches them.
    """
    if certification == CSV_CERTIFIED:
        path = out_dir / "data" / f"{stem}.csv"
        return path, {EVIDENCE_PATH_KEY: f"data/{path.name}"}
    path = out_dir / RETAINED_DIR / f"{stem}{RETAINED_SUFFIX}"
    return path, {
        RETAINED_PATH_KEY: f"{RETAINED_DIR}/{path.name}",
        EVIDENCE_WITHHELD_KEY: CSV_UNCERTIFIED_DETAIL[certification],
    }


def unassessable_reason(record: dict[str, Any]) -> str | None:
    """Why a SUCCESSFUL data leg cannot be assessed for emptiness at all, or ``None``.

    The third state, beside "rows present" and "zero rows". A capture is unassessable when its data
    leg reports ``status: ok`` and yet carries no measured ``row_count`` -- because the capture
    predates the field, or because the payload could not be certified as CSV and no shape was
    therefore taken from it (:data:`tableau_payload_facts.CSV_CONTENT_TYPE_ABSENT`).

    ⚠️ **Absence is not a zero, and it is not a non-zero either.** :func:`empty_classification`
    already refuses to call this empty, which is right; what was missing is that its ``None`` then
    read as *"not empty"* everywhere downstream. A view nothing measured must say so, in its own
    flag and its own named list, and must not be counted evidence-complete.

    ``bool`` is excluded explicitly: ``isinstance(True, int)`` is true in Python, and a ``row_count``
    of ``True`` is a corrupt record, not a measurement of one row.
    """
    data = record.get("data") or {}
    if data.get("status") != "ok":
        return None
    row_count = data.get("row_count")
    if isinstance(row_count, int) and not isinstance(row_count, bool):
        return None
    certification = data.get("certification")
    return certification if certification in UNASSESSABLE_REASONS else UNASSESSABLE_NO_ROW_COUNT


def withhold_uncertified_evidence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records in which no UNASSESSABLE data leg names a file under the evidence key.

    The enforcement point for the structural rule above, applied both when this repo WRITES a
    manifest (so a current capture cannot produce the invalid state) and when it READS one (so a
    manifest written before the rule -- the review's own second reproduction, ``flags=[
    data_unassessable, row_count_unrecorded]`` with ``data/view.csv`` still under ``path`` -- cannot
    be consumed as evidence either). A file already on disk cannot be rewritten retroactively, so the
    boundary that loads it is where the invariant is restored; :func:`read_manifest` is that
    boundary.

    An already-compliant record is returned untouched, so this is idempotent and cheap to apply more
    than once along a pipeline.

    ⚠️ The bytes are NOT deleted and ``status`` is NOT changed. The transport genuinely succeeded and
    the body may well be a perfect export -- what is denied is that anything ESTABLISHED it as one.
    Copies rather than mutates, for the reason :func:`flag_empty` does.
    """
    out = []
    for record in records:
        data = record.get("data") if isinstance(record, dict) else None
        if not isinstance(data, dict) or EVIDENCE_PATH_KEY not in data or not unassessable_reason(record):
            out.append(record)
            continue
        demoted = {k: v for k, v in data.items() if k != EVIDENCE_PATH_KEY}
        demoted[RETAINED_PATH_KEY] = data[EVIDENCE_PATH_KEY]
        demoted[EVIDENCE_WITHHELD_KEY] = CSV_UNCERTIFIED_DETAIL.get(data.get("certification"), RETAINED_DETAIL_DEFAULT)
        out.append({**record, "data": demoted})
    return out


def read_manifest(path: Path) -> Any:
    """Load an ``oracle-manifest.json`` with the evidence-path rule restored over its views.

    ⚠️ **Read a capture manifest through this, never ``json.loads`` directly.** A manifest written
    before #480 names uncertified bytes under ``path``, and a consumer reading that file raw reads
    the exact fail-open shape this change removes. For a manifest THIS repo wrote the guarantee is
    stronger and needs no cooperation at all, because :func:`write_manifest` already applied the same
    rule to the bytes on disk; this is what covers the ones it did not write.

    Raises exactly what ``read_text``/``json.loads`` raise -- a caller that wants to tolerate an
    absent or corrupt manifest must still say so, as it did before.
    """
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("views"), list):
        return manifest
    return {**manifest, "views": withhold_uncertified_evidence(manifest["views"])}


def empty_classification(record: dict[str, Any]) -> str | None:
    """Why this view's data leg is empty, or ``None`` when it is not empty at all.

    The ONE predicate for "this capture carries no rows", shared by the per-view console line, the
    per-view flag, the manifest's count and the manifest's named list -- for the same reason
    :func:`_partition` is one function: three copies of a rule is three chances for a count and a
    list to disagree about the same views.

    ``None`` is returned for anything that is not an ESTABLISHED zero-row capture, which includes a
    record whose data leg failed (its emptiness is explained by the failure, not by the view) and an
    older record that carries no ``row_count`` at all (absence is not a zero -- claiming one would
    invent a diagnostic about a manifest that never measured it).

    ⚠️ **That ``None`` is NOT "this capture is fine", and reading it as one is a fail-open defect in
    its own right.** It answers a single question -- "is this an established zero-row capture" -- and
    a record with no ``row_count`` answers it "no" for the opposite reason a 900,000-row capture
    does. :func:`unassessable_reason` is the other half, and every consumer that partitions, counts
    or flags must consult both; the count is not evidence-complete without it.

    ⚠️ **The glossary case is left OPEN on purpose.** ``EMPTY_CANNOT_CLASSIFY`` means the export
    returned no header, which a fieldless sheet and a genuinely empty query produce identically; the
    only thing that separates them is field-level metadata, and the capture holds none. The Metadata
    API call this run already makes asks for ``luid`` and nothing else, and widening it is not free:
    ``tableau_view_types`` refuses a partial answer WHOLE, so a field this server spells differently
    would turn view typing off for every view on the site, and per-field nodes would push a large
    estate at its 32 MiB body bound. Guessing from the byte count would be cheap and wrong. So the
    honest states are two, and the third is named as missing rather than invented.
    """
    data = record.get("data") or {}
    if data.get("status") != "ok" or data.get("row_count") != 0:
        return None
    return EMPTY_QUERY_NO_ROWS if data.get("columns") else EMPTY_CANNOT_CLASSIFY


def flag_empty(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records with the per-view ``flags`` key added to every capture whose numbers are not clean.

    Two flags, because there are three states and only one of them is silent. An established
    zero-row capture gets :data:`FLAG_DATA_EMPTY` plus its classification; a capture nothing could
    measure gets :data:`FLAG_DATA_UNASSESSABLE` plus its reason. A capture that returned rows gets
    no flag at all, which is what keeps the flag worth reading.

    ⚠️ The unassessable half is the fail-open fix: without it a record with no ``row_count`` came
    through here untouched, and "no flags" is exactly what a clean capture looks like. Every
    consumer -- the packaged unit, the per-workbook subset, a human reading one view -- then read
    silence as evidence.

    Copies rather than mutates: the caller's records are its own, and a function that silently
    rewrites the list it was handed is the shape that makes a second call to it wrong.

    The flag is what a CONSUMER reads -- ``package_unit.py`` ships it per view, and a per-workbook
    subset carries it along -- so the fact survives every slice of the capture, not just the
    capture-wide manifest.
    """
    out = []
    for record in records:
        classification = empty_classification(record)
        unassessable = unassessable_reason(record)
        if classification:
            added = [FLAG_DATA_EMPTY, classification]
        elif unassessable:
            added = [FLAG_DATA_UNASSESSABLE, unassessable]
        else:
            out.append(record)
            continue
        flags = list(dict.fromkeys([*record.get("flags", []), *added]))
        out.append({**record, "flags": flags})
    return out


def _named_views(records: list[dict[str, Any]], reason_of, key: str) -> list[dict[str, Any]]:
    """The shared projection behind every "counted AND named" list in this module.

    One function so a count and its list cannot describe different views, and so a new diagnostic
    ships the same identity fields as the ones before it. ``reason_of`` is the predicate --
    :func:`empty_classification` or :func:`unassessable_reason` -- and ``key`` names what the reason
    is called in the entry, since "why is this empty" and "why can this not be assessed" are
    different questions and must not share a field name.
    """
    out = []
    for record in records:
        reason = reason_of(record)
        if not reason:
            continue
        out.append(
            {
                "view_luid": record.get("view_luid"),
                "view_name": record.get("view_name"),
                "workbook_name": record.get("workbook_name"),
                "view_type": record.get("view_type"),
                key: reason,
            }
        )
    return out


def data_unassessable_views(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Views whose data leg SUCCEEDED and whose evidence could not be assessed. Counted AND named.

    The same shape as :func:`data_empty_views` and for the same reason, applied to the state that
    had no reporting at all. A reviewer needs "on WHICH views is a numeric-fidelity finding
    impossible" answered identically whether the cause is a measured zero or an unmeasurable body --
    and the second used to answer "none", silently, which is worse than the first because it looks
    like good news.

    Each entry carries its REASON, not a verdict: ``row_count_unrecorded`` is a manifest written
    before the count existed (re-capture and it resolves), while ``content_type_absent`` is a live
    server or proxy that did not declare what it sent (the payload may be perfect data -- nothing
    here establishes that it is, which is exactly the point).
    """
    return _named_views(records, unassessable_reason, "reason")


def data_empty_views(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Views whose data leg SUCCEEDED and returned zero rows. Counted AND named (#471).

    The same argument :func:`render_unestablished` already makes, applied to the numeric half: the
    count answers "how much of my oracle is evidentially empty" and the list answers "for which
    views is a NUMERIC-fidelity finding currently impossible to make". Before this the count was the
    only thing that escaped -- ``empty`` was computed as a list and immediately reduced to its
    ``len()`` -- so a reviewer holding a 94-view capture with 12 blank ones had to open every PNG by
    hand to find them (SES, 2026-09-03).

    Each entry carries its CLASSIFICATION rather than a verdict, for the reason
    :func:`render_unestablished` carries per-tier statuses rather than one: the causes need opposite
    remedies. A relative-date filter whose window has not landed yet is re-runnable; a required
    filter defaulting to None never resolves from a default-state capture and needs ``?vf_`` state
    pinning (#194). This capture cannot tell those two apart either -- both are
    ``EMPTY_QUERY_NO_ROWS`` -- because nothing on hand describes a view's filters, and inventing the
    distinction from a rendered image would be a guess.
    """
    return _named_views(records, empty_classification, "classification")


def log_progress(index: int, total: int, record: dict[str, Any], redactor=None) -> None:
    """One line per view: proof of rows captured, a loud zero, or a loud, classified failure.

    ⚠️ The console is the THIRD artifact, after the manifest and the files. A view NAME is response
    data -- a reflected token can arrive as one -- and this line used to slice it to 34 characters
    before anything scrubbed it, which is the round-4 defect at a boundary round 4 never looked at.
    CI keeps its logs, so "only the terminal" is not a mitigation.

    ⚠️ A zero-row capture is a WARNING, not an INFO carrying a zero (#471). ``0 rows`` inside the
    ordinary progress line is technically visible and practically invisible: the reporting site had
    12 of them among 91 INFO lines and found them by opening PNGs by hand. The line keeps its
    columns so the run still reads as a table, and gains the classification and a marker.

    ⚠️ An UNASSESSABLE capture is a WARNING for the same reason and a separate branch for a stronger
    one: the ordinary line interpolates ``data["row_count"]``, which a record without one does not
    have, so printing it here raised ``KeyError`` and took the whole run down at the console. It
    reports no row count, because there is none to report -- printing ``0`` would be the invented
    zero this module refuses everywhere else.
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
        empty = empty_classification(record)
        unassessable = unassessable_reason(record)
        if unassessable:
            LOG.warning(
                "  %2d/%d  %-34s     ? rows  %6.1fs%s  <- UNASSESSABLE (%s)",
                index,
                total,
                name,
                data.get("elapsed_sec", 0.0),
                suffix,
                unassessable,
            )
        elif empty:
            LOG.warning(
                "  %2d/%d  %-34s %5d rows  %6.1fs%s  <- NO DATA (%s)",
                index,
                total,
                name,
                data["row_count"],
                data["elapsed_sec"],
                suffix,
                empty,
            )
        else:
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
    Since #423 a NEW capture never leaves a requested leg absent: ``_capture_renders`` writes a record
    for every requested tier whichever branch it takes, so absent means "not requested" and nothing
    else. The fallback below is what reads an OLDER manifest correctly, where ``capture_view`` did
    return before attempting any render once the **data** leg had failed. Those renders are absent
    *because of their prerequisite*, and inventing an independent ``not_captured`` failure for each
    put a purely credential-blocked view into ``blocked`` **and** ``failed`` at once, where ``failed``
    wins and the run exits 3 instead of the human-actionable 2. The prerequisite's own status is
    propagated instead, so one root cause is counted once -- and a genuinely broken data leg still
    yields failing renders. New captures reach the same verdicts through the recorded statuses,
    because a credential-blocked leg is stamped ``source_credential`` at capture time for exactly
    this reason.
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
        # ONE predicate, shared with the named list and the per-view flag (#471). It used to be an
        # inline `r["data"]["row_count"] == 0` here and a second copy in `group_oracle_by_workbook`,
        # which is how a count and a list come to disagree about the same views -- and it raised
        # KeyError on an older record whose data leg recorded no row count at all.
        "empty": [r for r in ok if empty_classification(r)],
        # The third state. Separate from `empty` because they need opposite readings: an empty
        # capture MEASURED nothing there, an unassessable one measured NOTHING AT ALL.
        "unassessable": [r for r in ok if unassessable_reason(r)],
        # ⚠️ `not unassessable_reason(r)` is the fail-open fix. `complete` is what the run reports as
        # "captured", and a record whose rows were never measured used to satisfy every clause here:
        # data status ok, renders ok, nothing to say otherwise. Evidence-complete has to mean the
        # evidence was established, not that nothing objected.
        "complete": [
            r
            for r in records
            if r.get("data", {}).get("status") == "ok"
            and not unassessable_reason(r)
            and all(s == "ok" for s in _render_statuses(r, requested))
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


def render_unestablished(records: list[dict[str, Any]], requested: frozenset[str]) -> list[dict[str, Any]]:
    """Views for which a render WAS requested and not one requested tier came back ``ok``.

    ⚠️ The defect class this exists for is a collapse, not a miscount: an absent ``image`` key used to
    read exactly like "no image was asked for", so a view whose render could never be established
    landed in the clean bucket and stayed there. Field evidence (#423): "Availability Summary by
    Tail" failed its data leg three times across two days with ``HTTP 0 / TimeoutError: read
    operation timed out``, and because the render legs were skipped it has no ``image`` key in ANY
    record. Nothing downstream could tell that from a data-only capture, so an equivalent
    visual-fidelity defect on that page was not merely unverified but **unfalsifiable**.

    UNESTABLISHED is deliberately NOT the same claim as FAILED. A credential-blocked tier, a
    version-gated tier and a never-attempted tier are three different remedies; what they share is
    that no reference image exists for this view, which is the one fact a fidelity review needs
    before it can start. Each entry therefore carries the per-tier statuses, not a verdict.
    """
    if not requested:
        return []
    out = []
    for record in records:
        legs = {kind: (record.get(_LEG_KEY[kind]) or {}).get("status") for kind in sorted(requested)}
        if any(status == "ok" for status in legs.values()):
            continue
        out.append({"view_luid": record.get("view_luid"), "view_name": record.get("view_name"), "renders": legs})
    return out


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

    ⚠️ **A zero-row capture is NOT one of these codes** (#471). It is recorded, flagged, named and
    warned about, and an otherwise-clean run still exits 0: the export succeeded, so calling it a
    failure would make an operator debug the transport for a view whose filter is simply pointed at
    a day with no data. Legibility and exit status are different questions, and conflating them is
    what "do not overload ``status``" means in practice.
    """
    # Before anything partitions or counts them: the per-view fact rides ON the record, so every
    # downstream slice of this capture -- the manifest, a per-workbook subset, a packaged unit --
    # carries it without re-deriving the rule.
    records = flag_empty(records)
    # ...and the STRUCTURAL half beside it (#480 round 2). `_capture_data` already writes an
    # uncertified body outside `data/`, so this normally changes nothing; it is here because
    # `write_manifest` is the one place every record must pass through, and a record assembled by
    # anything other than `_capture_data` must not be able to reach the manifest with uncertified
    # bytes named as evidence.
    records = withhold_uncertified_evidence(records)
    sets = _partition(records, run.requested_renders)
    blocked, failed, complete = sets["blocked"], sets["failed"], sets["complete"]
    rendered = sum(1 for r in records if any(r.get(leg, {}).get("status") == "ok" for leg in ("image", "svg", "pdf")))
    # "Nothing rendered, and the credential explains ALL of it" -- the one case where an absent
    # reference is code 2's problem rather than code 5's.
    credential_only = rendered == 0 and bool(blocked) and len(blocked) == len(records)
    reference_missing = run.reference_required and rendered == 0 and not credential_only
    unestablished = render_unestablished(records, run.requested_renders)
    empty_views = data_empty_views(records)
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
        # #471: the same counted-AND-named shape as `render_unestablished_views`, and for the same
        # reason: the count answers "how much of this oracle is evidentially empty", the list answers
        # "on WHICH views can a numeric-fidelity finding not be made". The count already existed and
        # the list did not -- `empty` was built and immediately reduced to its `len()` -- so 12 blank
        # views among 94 were countable and unfindable, indistinguishable per view from a capture
        # that returned 900,000 rows.
        "data_empty_views": empty_views,
        # The third state, counted AND named beside the other two. A capture whose rows were never
        # measured is not a clean capture and not an empty one; before this it was reported as
        # neither, which meant it was reported as fine. `data_ok` deliberately still counts it --
        # the HTTP call DID succeed, and collapsing that into the numeric verdict would destroy a
        # real distinction -- so this pair is what stops `data_ok` being read as evidence.
        "data_unassessable": len(sets["unassessable"]),
        "data_unassessable_views": data_unassessable_views(records),
        "image_ok": sum(1 for r in records if r.get("image", {}).get("status") == "ok"),
        "svg_ok": sum(1 for r in records if r.get("svg", {}).get("status") == "ok"),
        "pdf_ok": sum(1 for r in records if r.get("pdf", {}).get("status") == "ok"),
        "requested_renders": sorted(run.requested_renders),
        "reference_required": run.reference_required,
        "reference_missing": reference_missing,
        # #423: views for which a render was REQUESTED and none was obtained. Counted AND named,
        # because the count answers "is my reference set complete" and the list answers "for which
        # pages is a visual-fidelity finding currently impossible to make". An absent `image` key can
        # no longer mean this -- every requested leg is now recorded -- but the manifest must still
        # SAY it, or a consumer has to re-derive it from three per-tier statuses and will not.
        "render_unestablished": len(unestablished),
        "render_unestablished_views": unestablished,
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
    # The manifest's own directory, ensured HERE rather than inherited as a side effect. It used to
    # exist only because `_capture_data` created `<out>/data/` before every export -- including the
    # ones it then refused -- and since #480 an uncertified or refused body creates nothing there. A
    # writer that depends on another function's incidental `mkdir` is one refactor from losing the
    # whole manifest of a run whose every leg failed, which is the run most worth reading.
    run.out_dir.mkdir(parents=True, exist_ok=True)
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
        "\n%d/%d captured (%d empty, %d unassessable), %d credential-blocked, %d failed, %d re-auth(s), "
        "%d retr(ies), %.0fs -> %s",
        len(complete),
        len(records),
        len(sets["empty"]),
        len(sets["unassessable"]),
        len(blocked),
        len(failed),
        run.session.reauth_count,
        run.session.retry_count,
        manifest["elapsed_sec"],
        manifest_path,
    )
    _log_blocked_and_stale(records, blocked, capability_report, run.session.redact_text)
    _log_unestablished(unestablished, run.session.redact_text)
    _log_empty(empty_views, run.session.redact_text)
    _log_unassessable(data_unassessable_views(records), run.session.redact_text)
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


def _log_empty(empty: list[dict[str, Any]], redactor) -> None:
    """Name every view whose capture carries no data rows, and say what that costs.

    Separate from ``_log_unestablished`` for the same reason that one is separate from
    ``_log_blocked_and_stale``: the remedies differ, and they differ WITHIN this class too. A
    relative-date filter whose window has no data yet is re-runnable tomorrow; a required filter
    defaulting to None never resolves from a default-state capture and needs ``?vf_`` state pinning
    (#194). So the actionable statement is about the CONSEQUENCE, which is what a fidelity reviewer
    would otherwise discover by opening 94 PNGs by hand.
    """
    if not empty:
        return
    LOG.warning(
        "\n%d view(s) captured ZERO DATA ROWS. The export SUCCEEDED, so these are recorded "
        "status 'ok' and this run's exit code is unaffected -- but a NUMERIC-fidelity finding "
        "cannot be made or refuted from an empty capture, exactly as a missing render makes a "
        "visual one impossible. Two field causes need OPPOSITE remedies: a relative-date filter "
        "whose window has not landed yet is re-runnable, while a required filter defaulting to "
        "None can never resolve from a default-state capture (it needs ?vf_ pinning, #194):",
        len(empty),
    )
    for entry in empty:
        LOG.warning(
            "  - %s (%s): %s",
            redacted_note(entry.get("view_name"), redactor, limit=60),
            redacted_note(entry.get("workbook_name"), redactor, limit=60),
            entry.get("classification"),
        )
    if any(entry.get("classification") == EMPTY_CANNOT_CLASSIFY for entry in empty):
        LOG.warning(
            "  %s means the export returned NO HEADER at all. A sheet with no underlying query "
            "(a glossary or reference sheet, where empty is CORRECT) and a real query that "
            "returned nothing are indistinguishable from that payload, and this capture holds no "
            "field-level metadata to separate them -- so it is reported unclassified rather than "
            "guessed from the byte count. Check those views by hand before counting them as "
            "defects; %s is the class that is certainly a real query with a real header.",
            EMPTY_CANNOT_CLASSIFY,
            EMPTY_QUERY_NO_ROWS,
        )


def _log_unassessable(unassessable: list[dict[str, Any]], redactor) -> None:
    """Name every view whose data leg succeeded and whose evidence could not be assessed at all.

    A separate block from :func:`_log_empty`, and the separation is the whole point: an empty
    capture is a MEASUREMENT (zero rows came back, which a numeric review can act on), while this
    one is the absence of a measurement. Folding them together would let a reader take "N empty"
    as the total cost, when the unassessable views are the ones nothing at all is known about.

    Both reasons are actionable and they are actionable DIFFERENTLY: ``row_count_unrecorded`` is an
    older manifest and re-capturing resolves it, while ``content_type_absent`` and
    ``content_type_unspecific`` are a live server or proxy that did not declare what it sent, or
    declared only ``text/plain`` -- re-capturing reproduces both, and the fix is upstream.
    """
    if not unassessable:
        return
    LOG.warning(
        "\n%d view(s) captured data that could NOT BE ASSESSED. The export succeeded, so these are "
        "recorded status 'ok' and this run's exit code is unaffected -- but no row count was ever "
        "established for them, so they are NOT counted as captured-complete and a numeric-fidelity "
        "finding cannot be made or refuted from them. This is not the same as an empty capture: an "
        "empty one measured zero rows, these measured nothing. Their retained bytes are kept under "
        "'%s/' and named '%s', never as data, so nothing downstream can read them as numbers. '%s' "
        "means an older manifest that predates the count (re-capture resolves it); '%s' means the "
        "server or a proxy returned the body with no Content-Type, and '%s' that it declared only "
        "text/plain, which an error banner is too. A CSV carries no signature, so neither "
        "establishes those bytes as data -- the fix is upstream of this capture:",
        len(unassessable),
        RETAINED_DIR,
        RETAINED_PATH_KEY,
        UNASSESSABLE_NO_ROW_COUNT,
        CSV_CONTENT_TYPE_ABSENT,
        CSV_CONTENT_TYPE_UNSPECIFIC,
    )
    for entry in unassessable:
        LOG.warning(
            "  - %s (%s): %s",
            redacted_note(entry.get("view_name"), redactor, limit=60),
            redacted_note(entry.get("workbook_name"), redactor, limit=60),
            entry.get("reason"),
        )


def _log_unestablished(unestablished: list[dict[str, Any]], redactor) -> None:
    """Name every view whose reference render could not be established, and say what that costs.

    Not folded into ``_log_blocked_and_stale``: those two classes each have ONE remedy (reauthorize a
    source; raise the API version). This class does not -- its members got there by different routes
    and some of them are simply "the view is too slow to export" -- so the actionable statement is
    about the CONSEQUENCE, which an operator will otherwise not connect to a line of per-tier statuses.
    """
    if not unestablished:
        return
    LOG.warning(
        "\n%d view(s) have NO reference render, so a visual-fidelity finding on those pages cannot "
        "currently be made or refuted. This is not the same as 'no render was requested' -- one was. "
        "Re-run just these views (a later batch often succeeds where an earlier one timed out), and "
        "raise --rest-timeout if the failures read as a read timeout:",
        len(unestablished),
    )
    for entry in unestablished:
        legs = entry.get("renders") or {}
        detail = ", ".join(f"{kind}={status or ABSENT_LEG}" for kind, status in legs.items())
        LOG.warning("  - %s: %s", redacted_note(entry.get("view_name"), redactor, limit=60), detail)


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
