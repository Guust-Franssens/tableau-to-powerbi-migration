"""
purpose: the ONE allowlist mechanism every shipped handover manifest is projected through, plus the
         value-shaped host-path redactor for the artifacts that must ship whole.
usage:   library only - imported by scripts/package_unit.py; there is no CLI.

Extracted from `package_unit.py` so that "one mechanism" is structurally true rather than merely
asserted, and so the declarative surface of a handover package - what a customer deliverable may
contain, at every level - can be read in one place.

Why the surface is TINY, and why that is the fix
------------------------------------------------
Three review rounds each closed one level of an allowlist and were followed by a deeper one:

| round | where the allowlist stopped |
|---|---|
| R2 | at the collection boundary - `workbooks[0].future_nested` survived |
| R3 | at container-valued fields - `workbooks[0].model_facts.future_install_root` survived |
| R3 | at whole artifacts that never entered `project()` - provenance, handover |

`docs/review-throughput-postmortem.md` measured that shape directly: **66% of round-2+ findings share
a defect class with round N-1**, and the stop rule is *simplify, delete, split or descope - not
another local guard.* So this round DELETES the surface instead of enumerating it. What the two gates
actually read was measured, not argued:

| shipped file | consumed by the gates | measured at |
|---|---|---|
| `report.json` | `workbooks` must be a **list**; `workbooks[].name`; `datasources[].name` |
  `check_reference_readiness._engine_report` :461, `._unit_names` :478-483;
  `check_unit._is_engine_report` :379 |
| `source-provenance.json` | `inputs[].input.sha256`, `inputs[].origin.match`,
  `inputs[].origin.workbook_luid` | `check_reference_readiness._provenance_luid` |
| `engine-output-receipt.json` | `engine.version` | `check_engine_receipts` :33-35 |

Everything else was engine metadata no gate consumes, so it is no longer shipped. That deletes every
container-valued field from `report.json` and the receipt outright - there is nothing left for a
fourth round to find a level below, because there is no level below.

Two mechanisms, deliberately different in kind
----------------------------------------------
1. **`project()` - name-shaped.** For documents whose schema we own the meaning of. `KEEP` is now
   **scalar-only**: a container reaching a `KEEP` leaf raises `UnscopedStructure` at packaging time
   rather than shipping. An unenumerated structure is a loud failure, not a silent pass-through.
2. **`redact_host_paths()` - value-shaped.** For the handover slice, which CANNOT be allowlisted: its
   schema is engine-owned, deeply nested and volatile, and it is the agent's actual deliverable
   (`read_handover.py` is documented against it, and `handover.md` is derived from it). Enumerating
   it would be the fourth patch. Its residual risk is a *value* - an absolute host path in a field
   nobody predicted - so it is closed by value shape, which no new field name can evade.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_paths import discloses_host_location, discloses_host_path  # noqa: E402  # pylint: disable=wrong-import-position


class UnscopedStructure(TypeError):
    """A dict or list reached a scalar-only `KEEP` leaf, so nothing describes what may ship from it.

    Raised rather than dropped, and rather than carried. Carrying it is the round-2/round-3 defect;
    dropping it silently would hide a real engine schema change behind a package that quietly lost
    content. Failing names the exact JSON path, which is what makes it actionable.
    """


class Rows:  # pylint: disable=too-few-public-methods
    """Spec marker: a list whose object entries are each projected onto ``spec``."""

    __slots__ = ("spec",)

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec


class Sanitized:  # pylint: disable=too-few-public-methods
    """Spec marker: a value normalised by a TYPED rule rather than by field names alone.

    ``project()`` is name-shaped, and a name-shaped rule cannot decide whether a *string* is safe to
    ship. One field needs both: ``view_type_resolution.unavailable_reason`` is free-form text by
    type, and the allowlist would carry whatever it happened to say. The sanitiser answers the value
    question; the allowlist inside it still answers the name question, so unknown nested keys are
    dropped exactly as they are everywhere else.

    The callable returns ``(shippable value, dropped/refused JSON paths)`` -- the same pair
    ``project()`` returns -- so the two compose without a special case at the call site.
    """

    __slots__ = ("sanitize",)

    def __init__(self, sanitize: Any) -> None:
        self.sanitize = sanitize


#: Spec leaf: carry this value verbatim - and it MUST be a scalar (str/int/float/bool/None).
#: A container here raises, because "verbatim" over a container is exactly the hole rounds 2 and 3
#: found: `_fields()` mapped every retained name to `KEEP`, and any of those that happened to be a
#: dict shipped its unknown grandchildren.
KEEP = "keep-scalar"
#: Spec leaf: a list whose entries must all be scalars (e.g. `requested_renders: ["png", "svg"]`).
SCALAR_LIST = "keep-scalar-list"

_SCALARS = (str, int, float, bool, type(None))


def _fields(*names: str) -> dict[str, Any]:
    """An allowlist level carrying the named SCALAR fields and dropping everything else."""
    return {name: KEEP for name in names}


def project(payload: Any, spec: Any, *, prefix: str = "") -> tuple[Any, list[str]]:  # pylint: disable=too-many-return-statements,too-many-branches
    """`(projected value, dropped paths)` - carry ONLY what ``spec`` names, at EVERY level.

    The recursion is the point, and so is the scalar restriction. `{"workbooks": Rows({"name": KEEP})}`
    drops an unenumerated `workbooks[].future_nested`; and a `model_facts` dict arriving at a `KEEP`
    leaf now RAISES instead of shipping its grandchildren.

    Dropped paths are de-duplicated and use `[]` for a row position, so 48 rows carrying the same
    unknown field report one `workbooks[].future_nested` rather than 48 indexed near-duplicates.

    A spec/value type mismatch on a container spec drops the value rather than guessing - a `Rows`
    spec meeting a dict means the manifest is not the shape we enumerated, and carrying it anyway is
    how the estate got out the first time.
    """
    if spec is KEEP:
        if not isinstance(payload, _SCALARS):
            raise UnscopedStructure(
                f"{prefix or '.'} is a {type(payload).__name__}, but the allowlist marks it as a scalar. "
                "Nothing describes what may ship from inside it - give it an explicit spec "
                "(a mapping, Rows(...) or SCALAR_LIST), or stop shipping the field."
            )
        return payload, []
    if spec is SCALAR_LIST:
        if not isinstance(payload, list):
            return [], [prefix or "."]
        for index, entry in enumerate(payload):
            if not isinstance(entry, _SCALARS):
                raise UnscopedStructure(
                    f"{prefix}[{index}] is a {type(entry).__name__} inside a scalar-list field; "
                    "give it Rows(...) or stop shipping the field."
                )
        return list(payload), []
    if isinstance(spec, Rows):
        if not isinstance(payload, list):
            return [], [prefix or "."]
        kept_rows: list[Any] = []
        dropped: list[str] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                dropped.append(f"{prefix}[{index}]")
                continue
            value, lost = project(entry, spec.spec, prefix=f"{prefix}[]")
            kept_rows.append(value)
            dropped.extend(lost)
        return kept_rows, sorted(set(dropped))
    if isinstance(spec, Sanitized):
        return spec.sanitize(payload, prefix=prefix or ".")
    if isinstance(spec, dict):
        if not isinstance(payload, dict):
            return {}, [prefix or "."]
        kept: dict[str, Any] = {}
        dropped = []
        for key, value in payload.items():
            path = f"{prefix}.{_safe_path_segment(key)}" if prefix else _safe_path_segment(key)
            if key not in spec:
                dropped.append(path)
                continue
            projected, lost = project(value, spec[key], prefix=path)
            kept[key] = projected
            dropped.extend(lost)
        return kept, sorted(set(dropped))
    raise TypeError(f"unusable allowlist spec at {prefix or '.'}: {spec!r}")


def _safe_path_segment(key: Any) -> str:
    """One dropped-path segment, with a host-path-disclosing KEY redacted before it is built into it.

    ⚠️ **#480 round-7 finding B2.** `project()`'s whole job is to refuse an unenumerated field, and it
    then named the refusal using the field's own key - so an *untrusted* key was re-emitted verbatim
    in `scope.dropped_fields`, which ships. Measured, one injected view field::

        "<drive>:\\Users\\<account>\\private\\secret": "safe scalar"
          -> "dropped_fields": ["views[].<drive>:\\Users\\<account>\\private\\secret"]

    The rule applied here is the one `tableau_env.scrub_tree` (:461-478) already states for the
    credential sink: **keys are scrubbed, and a diagnostic path is built from the SCRUBBED key.** The
    guard must not become the disclosure channel. The `views[]` prefix survives, so the operator still
    learns at which LEVEL an unnameable field was dropped - redaction here costs the key, not the
    location.

    ⚠️ **This one stays on the NARROW, profile-only predicate, and that is deliberate** (round 9).
    Everything that SHIPS is judged by :func:`host_paths.discloses_host_location`; this is not a
    shipping decision but a pre-scrub applied while a diagnostic is being BUILT, and the mitigation
    is the packager's final sweep over the stamped document, which is wide. Widening this one too
    would MASK that sweep - `test_NOTHING_is_appended_to_the_oracle_manifest_after_its_last_containment_pass`
    discriminates the two guards precisely by driving a key this one is silent on, and with both wide
    the ordering claim becomes unobservable. That is the same trap round 9 documents for the parse
    anchor in `package_unit._declares_non_relative`: a wider layer above an existing one retires the
    proof that the lower one still runs. Nothing leaks by keeping it narrow - the sweep refuses the
    raw key either way.
    """
    text = str(key)
    return REDACTED if discloses_host_path(text) else text


# --------------------------------------------------------------------------------------------
# the value-shaped half: absolute host locations, wherever they appear
# --------------------------------------------------------------------------------------------

REDACTED = "<redacted-absolute-path>"


def _redacted_key(key: Any, taken: dict[str, Any]) -> tuple[Any, bool]:
    """`(key safe to ship, was it redacted)` - one dictionary key, collision-disambiguated.

    ⚠️ **#480 round 9, leak 2.** :func:`redact_host_paths` cleaned dictionary VALUES and preserved
    raw KEYS, so the handover slice - the one artifact that ships WHOLE - carried whatever a key
    spelled. Round 8 declined this citing *"engine-authored keys, no reproduction"*; a reproduction
    now exists, built with the same hypothetical-future-field method the slice's own value-redaction
    test already uses::

        {"workbook": {"<a profile path>": "safe scalar"}}
          -> handover/Book.json ships the key, and the account name, verbatim

    "Engine-authored" describes a key's ORIGIN; it does not enforce the shipping invariant, and the
    packager's own manifest walk (`package_unit._contain_unsafe_key`) had already concluded the same
    thing one artifact over. The two properties below are copied from there, and from
    `tableau_env._scrub_key` before it:

    * **a collision is disambiguated, never silently dropped** - two distinct unsafe keys would
      otherwise both redact to one sentinel and `dict` would keep the last, turning redaction into
      data loss in the agent's actual work queue;
    * an **already-redacted** key reports redacted, so the walk stays idempotent and a second pass
      cannot read the sentinel as a fresh, safe key.
    """
    if not isinstance(key, str):
        return key, False
    if key == REDACTED or key.startswith(f"{REDACTED}#"):
        return key, True
    if not discloses_host_location(key):
        return key, False
    unique, suffix = REDACTED, 2
    while unique in taken:
        unique, suffix = f"{REDACTED}#{suffix}", suffix + 1
    return unique, True


def redact_host_paths(payload: Any, *, prefix: str = "") -> tuple[Any, list[str]]:
    """`(payload with absolute host locations replaced, JSON paths redacted)`.

    The complement to `project()`, for documents that must ship whole. It closes by VALUE SHAPE, so
    a field name nobody predicted cannot evade it - which is precisely the property the three
    name-shaped rounds lacked. It redacts rather than drops: the handover slice is the agent's work
    queue, and deleting a key it reads would trade a leak for a broken deliverable.

    ⚠️ **ONE question, asked once** (#480 round 9). Round 7 unioned an anchored `HOST_PATH_RE.match`
    ("does this value START as a location") with a profile-only containment test, because neither
    alone covered the other. Both were spelling tests, and round 9 measured the gap between them:
    `HTTP 503: ` + a non-profile absolute passed BOTH. :func:`host_paths.discloses_host_location`
    normalises the spelling away and then asks a single question, and it is a strict superset of the
    anchored predicate it replaced - so that predicate is deleted rather than kept beside it. Two
    overlapping definitions of one question is what six rounds of this PR have been.

    ⚠️ **KEYS are redacted too** (round 9, leak 2), by :func:`_redacted_key`, and the reported path
    is built from the REDACTED key so the record of the catch cannot re-emit what was caught.
    """
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        hit: list[str] = []
        for key, value in payload.items():
            safe_key, key_redacted = _redacted_key(key, out)
            here = f"{prefix}.{safe_key}"
            if key_redacted:
                hit.append(f"{here} (key)")
            cleaned, found = redact_host_paths(value, prefix=here)
            out[safe_key] = cleaned
            hit.extend(found)
        return out, hit
    if isinstance(payload, list):
        rows = []
        hit = []
        for index, value in enumerate(payload):
            cleaned, found = redact_host_paths(value, prefix=f"{prefix}[{index}]")
            rows.append(cleaned)
            hit.extend(found)
        return rows, hit
    if isinstance(payload, str) and discloses_host_location(payload):
        return REDACTED, [prefix or "."]
    return payload, []


# --------------------------------------------------------------------------------------------
# the surfaces, each descoped to what the gates measurably read
# --------------------------------------------------------------------------------------------

#: `report.json`. The gate surface is a NAME, so a name is all that ships. The engine's 29-field
#: workbook row and 29-field datasource row are no longer carried at all: nothing in either gate
#: reads them, and the same account of this unit's residual work is in the handover slice, which is
#: the artifact `read_handover.py` and `handover.md` are built on. Deleting them removes ~90 KB per
#: package AND every container-valued field, so there is no deeper level for a fourth round to find.
REPORT_ROW = _fields("name")
REPORT_ALLOW: dict[str, Any] = {
    "tool": KEEP,
    "generated_at": KEEP,
    "workbooks": Rows(REPORT_ROW),
    "datasources": Rows(REPORT_ROW),
}
#: The two collections filtered to this unit before projection. Always emitted, always lists.
REPORT_UNIT_LISTS = ("workbooks", "datasources")

#: `engine-output-receipt.json`. `engine.version` is the only field any consumer reads
#: (`check_engine_receipts.py:33-35`). `artifacts[]` - 3,138 entries on the reference bundle - is
#: read by nobody in a package: `credential_gate._receipt_artifacts` is only reached through
#: `_receipt_matches_bundle`, which raises OSError on the package's absent `input_manifest.json`
#: first. `engine.root`/`plugin_root` were absolute installation paths; provenance is a VERSION, not
#: a location on the machine that happened to build it.
RECEIPT_ALLOW: dict[str, Any] = {
    "version": KEEP,
    "created_at": KEEP,
    "engine": _fields("version", "source", "canonical"),
}

#: Top-level keys of a handover slice that any consumer reads. Measured two ways: every one of the
#: 46 real slices in `_runs/407-dryrun-gates/bundle/handover` carries exactly `workbook` and
#: `estate`, and the only readers are `read_handover._workbooks_from_payload` (:383-390),
#: `check_unit` (:1817) and `_is_handover_slice` (:392) - all of which take `workbook`/`workbooks`.
#: `estate` is estate-wide by content and read by nobody, so it is not shipped.
HANDOVER_CONSUMED_KEYS = ("workbook", "workbooks")

#: `source-provenance.json`. Exactly the three fields `check_reference_readiness._provenance_luid`
#: reads, and nothing else - not `workbook_name`, not `project`, both of which are foreign-identity
#: channels when an entry belongs to another workbook.
PROVENANCE_ALLOW: dict[str, Any] = {
    "inputs": Rows(
        {
            "input": _fields("file", "sha256"),
            "origin": _fields("workbook_luid", "match"),
        }
    )
}

#: One render/data leg inside an oracle view. Explicit rather than `KEEP`, because these are the
#: containers round 3 found shipping unknown grandchildren. Key sets measured on
#: `_runs/407-dryrun-gates/oracle` (image 11, svg 16, data 11); the union is specified once, since a
#: leg only ever carries its own keys and an unknown one must now raise rather than ship.
ORACLE_LEG_ALLOW = _fields(
    "status",
    "path",
    "format",
    "sha256",
    "bytes",
    "elapsed_sec",
    "retries",
    "reauths",
    "vector",
    "width_px",
    "height_px",
    "text_elements",
    "path_elements",
    "image_elements",
    "external_refs",
    "row_count",
    # #480. The data leg's CSV certification verdict -- one of `tableau_payload_facts.CSV_VERDICTS`,
    # this repo's own closed vocabulary. It ships for the same reason `flags` does: without it a
    # packaged unit carries `status: ok` with no `row_count` and nothing anywhere saying the body
    # was never established as CSV, which is the fail-open this field exists to close.
    "certification",
    # #480 round 2. WHERE uncertified bytes were retained, and the authored sentence saying why they
    # are not evidence. They ship together and they ship instead of `path`: a data leg that names a
    # file here is one no consumer may read as numbers, which is the whole structural point.
    "retained_path",
    "evidence_withheld",
    "packaged_from",
    "packaging_reason",
)
ORACLE_LEG_LIST_FIELDS = {"retry_reasons": SCALAR_LIST, "dimensions_px": SCALAR_LIST, "columns": SCALAR_LIST}
ORACLE_LEG_SPEC: dict[str, Any] = {**ORACLE_LEG_ALLOW, **ORACLE_LEG_LIST_FIELDS, "format_hints": Rows(_fields())}

#: One view inside `oracle-manifest.json` (N=13, plus the stem this packager adds).
ORACLE_VIEW_ALLOW: dict[str, Any] = {
    **_fields(
        "view_luid",
        "view_name",
        "view_url_name",
        "view_type",
        "content_url",
        "project",
        "workbook_luid",
        "workbook_name",
        "captured_at",
        "updated_at",
        "packaged_object_stem",
    ),
    # #471. A PER-VIEW fact, which is why it ships where the estate-wide `data_empty` count is
    # dropped: this packager cannot honestly recompute "how much of the capture was empty" for one
    # unit, but "this view returned no rows" is true of the view regardless of which unit ships it.
    # The values are `tableau_oracle_manifest`'s own literals -- no foreign identity, no free text --
    # and without this line the diagnostic would be silently dropped at the package boundary, which
    # is moving the failure rather than fixing it.
    "flags": SCALAR_LIST,
    **{leg: ORACLE_LEG_SPEC for leg in ("image", "svg", "pdf", "data")},
}

# --------------------------------------------------------------------------------------------
# view_type_resolution: the ONE record whose value, not merely whose field name, decides shipping
# --------------------------------------------------------------------------------------------

#: The record `tableau_view_types.resolve_and_stamp` writes and `capture_tableau_oracle` stamps into
#: `oracle-manifest.json` as `view_type_resolution` -- `{"reauths": int, "unavailable_reason": str |
#: None}` (#560). It says whether the ONE site-wide Metadata call that types every view lost its
#: session and recovered, and whether typing stayed unavailable. Both grouping
#: (`group_oracle_by_workbook.subset_manifest`) and packaging (`package_unit._scope_oracle_manifest`)
#: used to drop it, so a per-workbook or packaged manifest showed a `view_types` census with nothing
#: saying it had been arrived at through a 401 -> reauth -> 200, or not arrived at at all.
VIEW_TYPE_RESOLUTION_FIELDS = ("reauths", "unavailable_reason")

#: Emitted INSTEAD of a record we cannot read. Deliberately not an omission and deliberately not a
#: zero: "no recovery happened" and "we cannot tell what happened" are different answers, and a
#: malformed record silently becoming `{"reauths": 0}` is the fail-open this whole field exists to
#: close. It echoes nothing of the offending value -- the report of a refusal must not re-emit what
#: was refused (`tableau_env.scrub_tree`'s rule, applied here).
RESOLUTION_REFUSED = "the capture's view_type_resolution was not a readable record; resolution evidence refused"
#: Emitted instead of a reason string this repository did not author -- see :func:`ships_reason`.
REASON_REFUSED = "the capture's view_type_resolution carried a reason this repository did not author; text refused"

_TYPE = r"[A-Za-z_][A-Za-z0-9_]*"  # a Python type name: `__name__`, never server-controlled text
_NODE = r"(dashboards|sheets)"  # the two GraphQL fields `tableau_view_types` names, and no others

#: **A closed vocabulary, checked by VALUE, because a name-shaped allowlist cannot make this call.**
#:
#: `unavailable_reason` is free-form by type, and this layer holds no credential, so it cannot redact
#: one out of a sentence the way the capture's own sink (`tableau_env.scrub_tree`) can. What it CAN
#: do is refuse anything outside the vocabulary its producer guarantees:
#: `tableau_view_types` authors every one of these strings and interpolates only Python type names,
#: HTTP statuses, integer counts and its own two literal field names -- never server-controlled text
#: (`tests/test_diagnostic_redaction.py` certifies that claim; this ENFORCES it one layer down).
#:
#: Consequence, and it is the intended one: a reflected credential arriving in that field never
#: reaches a grouped or packaged manifest -- it is replaced by :data:`REASON_REFUSED`, so the FACT
#: that typing was unavailable survives while the text does not. Drift is fail-closed and loud:
#: `tests/test_view_type_resolution_scope.py` drives the producer's own failure branches through
#: :func:`ships_reason`, so a nineteenth reason fails a test rather than shipping unchecked.
_AUTHORED_REASONS: tuple[str, ...] = (
    rf"metadata api response was {_TYPE}, not an object",
    r"metadata api returned HTTP \d+",
    rf"metadata api returned HTTP \d+; re-authentication failed: {_TYPE}",
    rf"metadata api call failed: {_TYPE}",
    r"metadata api response exceeded the \d+ byte ceiling; response refused",
    rf"metadata api response was not usable JSON: {_TYPE}",
    r"metadata api returned \d+ graphql error\(s\); response refused",
    rf"metadata api `errors` was {_TYPE}, not a list; response refused",
    rf"metadata api `data` was {_TYPE}, not an object",
    rf"metadata api `workbooks` was {_TYPE}, not a list",
    r"metadata api returned no dashboards or sheets carrying a luid",
    rf"a workbook node was {_TYPE}, not an object; response refused",
    rf"a workbook had no `{_NODE}` field, which the schema declares non-null; response refused",
    rf"`{_NODE}` was {_TYPE}, not a list; response refused",
    rf"a `{_NODE}` node was {_TYPE}, not an object; response refused",
    rf"a `{_NODE}` node carried a {_TYPE} where the schema declares String!; response refused",
    rf"a `{_NODE}` node carried a non-empty value that is not a luid; response refused",
    r"the same luid was reported as both a dashboard and a worksheet; response refused",
    # This module's own aggregate over merged batches, and its two refusals. They are in the
    # vocabulary so the rule is IDEMPOTENT: a grouped manifest re-read by the packager must survive
    # its own sanitiser unchanged, or evidence would decay one hop at a time.
    (
        r"across \d+ capture batches: \d+ could not establish view types, "
        r"\d+ carried no readable resolution record; see view_type_resolution_by_batch"
    ),
    re.escape(RESOLUTION_REFUSED),
    re.escape(REASON_REFUSED),
)
_AUTHORED_REASON_RE = re.compile(r"(?:%s)\Z" % "|".join(_AUTHORED_REASONS))  # pylint: disable=consider-using-f-string

#: What one merged batch contributed. `record` is a closed vocabulary, so "this batch recovered",
#: "this batch never resolved typing" and "this batch's record was unreadable" stay three answers.
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_ABSENT = "absent"
RESOLUTION_UNREADABLE = "unreadable"
#: The four fields one per-batch row may carry. Anything else is dropped and named, like every other
#: level here -- a row is a diagnostic, not a place to smuggle a field past the allowlist.
RESOLUTION_ROW_FIELDS = ("batch", "record", *VIEW_TYPE_RESOLUTION_FIELDS)


def ships_reason(text: Any) -> bool:
    """True when ``text`` is a reason this repository authored, and may therefore ship verbatim."""
    return isinstance(text, str) and bool(_AUTHORED_REASON_RE.match(text))


def _shippable_reauths(value: Any) -> tuple[int | None, bool]:
    """`(count, was it refused)`. A bool is NOT a count, and a negative one is not either.

    ``None`` is returned for anything unreadable, never ``0``: the caller ships "not established",
    which no consumer can mistake for "no re-authentication happened".
    """
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, True
    return value, False


def scope_view_type_resolution(value: Any, *, prefix: str = "view_type_resolution") -> tuple[Any, list[str]]:
    """One `view_type_resolution` record, made shippable. `(record or None, refused paths)`.

    ``None`` in and ``None`` out means exactly what the capture means by it: view typing was not
    resolved on this run at all. Everything else is normalised onto the two typed fields, with any
    unknown key dropped (and named) like every other allowlist level in this module.
    """
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return {"reauths": None, "unavailable_reason": RESOLUTION_REFUSED}, [prefix]
    refused: list[str] = [
        f"{prefix}.{_safe_path_segment(key)}" for key in value if key not in VIEW_TYPE_RESOLUTION_FIELDS
    ]
    reauths, bad_count = _shippable_reauths(value.get("reauths"))
    if bad_count:
        refused.append(f"{prefix}.reauths")
    reason = value.get("unavailable_reason")
    if reason is not None and not ships_reason(reason):
        reason = REASON_REFUSED
        refused.append(f"{prefix}.unavailable_reason")
    return {"reauths": reauths, "unavailable_reason": reason}, sorted(set(refused))


def merged_view_type_resolution(
    contributions: list[tuple[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Fold several batches' records into `(a conservative aggregate, one row per batch)`.

    ⚠️ **Last-wins is the defect this exists to prevent.** ``merge_batches`` takes its non-view fields
    from the NEWEST batch, so a clean re-run silently overwrote an earlier batch's recovered 401 --
    the grouped manifest then said `reauths: 0` about a merge that contained a recovery. The
    aggregate is therefore built so that it **cannot erase either**:

    * ``reauths`` is the SUM of the batches whose counts are readable, so any recovery anywhere keeps
      the aggregate non-zero;
    * ``unavailable_reason`` is non-null whenever ANY batch could not establish typing or carried an
      unreadable record, and it names how many of each -- the per-batch detail is in the rows, which
      the caller writes beside it as ``view_type_resolution_by_batch``.

    A single contribution is returned VERBATIM (normalised, never summarised), so the ordinary
    one-capture case survives grouping exactly as the capture wrote it.

    Returns ``(None, rows)`` only when no batch carried a record at all -- an aggregate is not
    invented for a merge that has nothing to say.
    """
    rows: list[dict[str, Any]] = []
    for label, value in contributions:
        record, _refused = scope_view_type_resolution(value)
        if record is None:
            state = RESOLUTION_ABSENT
        elif record["unavailable_reason"] == RESOLUTION_REFUSED:
            state = RESOLUTION_UNREADABLE
        else:
            state = RESOLUTION_RESOLVED
        rows.append(
            {
                "batch": REDACTED if discloses_host_location(str(label)) else str(label),
                "record": state,
                "reauths": None if record is None else record["reauths"],
                "unavailable_reason": None if record is None else record["unavailable_reason"],
            }
        )
    present = [row for row in rows if row["record"] != RESOLUTION_ABSENT]
    if not present:
        return None, rows
    if len(contributions) == 1:
        single, _refused = scope_view_type_resolution(contributions[0][1])
        return single, rows
    unavailable = [row for row in present if row["record"] == RESOLUTION_RESOLVED and row["unavailable_reason"]]
    unreadable = [row for row in rows if row["record"] == RESOLUTION_UNREADABLE]
    reason = None
    if unavailable or unreadable:
        reason = (
            f"across {len(rows)} capture batches: {len(unavailable)} could not establish view types, "
            f"{len(unreadable)} carried no readable resolution record; see view_type_resolution_by_batch"
        )
    return {
        "reauths": sum(row["reauths"] for row in present if isinstance(row["reauths"], int)),
        "unavailable_reason": reason,
    }, rows


def scope_view_type_resolution_batches(
    value: Any, *, prefix: str = "view_type_resolution_by_batch"
) -> tuple[Any, list[str]]:
    """The per-batch rows, made shippable. Same rules as one record, plus the batch LABEL.

    A label is a directory name an operator chose, so it is swept for an absolute host location by
    the same predicate everything else in this module is judged by; the row is kept either way,
    because which batch is which is the whole point of the list.
    """
    if not isinstance(value, list):
        return [], [prefix]
    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            refused.append(f"{prefix}[{index}]")
            continue
        refused.extend(f"{prefix}[].{_safe_path_segment(key)}" for key in entry if key not in RESOLUTION_ROW_FIELDS)
        record, lost = scope_view_type_resolution(
            {key: entry[key] for key in VIEW_TYPE_RESOLUTION_FIELDS if key in entry}, prefix=f"{prefix}[]"
        )
        refused.extend(lost)
        label = str(entry.get("batch", ""))
        state = entry.get("record")
        if state not in (RESOLUTION_RESOLVED, RESOLUTION_ABSENT, RESOLUTION_UNREADABLE):
            if state is not None:
                refused.append(f"{prefix}[].record")
            state = None
        rows.append(
            {
                "batch": REDACTED if discloses_host_location(label) else label,
                "record": state,
                **(record or {"reauths": None, "unavailable_reason": None}),
            }
        )
    return rows, sorted(set(refused))


#: `oracle-manifest.json`. Everything counting the ESTATE RUN is dropped and RECOMPUTED from the
#: packaged views (see `_scope_oracle_manifest`); everything identifying another unit is dropped
#: outright. `render_capability.probe_view_luid`/`probe_view_name`/`probe_view_luids` name the view
#: the capability ladder was probed against, which on the reference estate is `'Overview'` in the
#: FOREIGN `Superstore` workbook; `warnings` is free text that can quote that same view, so it goes
#: too. What survives is what tells a consumer which GRADE of evidence it got.
ORACLE_MANIFEST_ALLOW: dict[str, Any] = {
    **_fields("schema", "captured_at", "server", "site", "rest_api_version"),
    "requested_renders": SCALAR_LIST,
    # #560 review round 1. The GRADE of the `view_types` census travels WITH it, or a packaged
    # manifest asserts a census arrived at through a session loss as though nothing had happened --
    # and a persistently unavailable typing as though it had been established. `Sanitized`, not
    # `_fields(...)`: `unavailable_reason` is a string, so a name-shaped allowlist would carry
    # whatever it said, and this layer holds no credential to redact one out of it.
    "view_type_resolution": Sanitized(scope_view_type_resolution),
    # Present only on a manifest that MERGED several capture batches (`group_oracle_by_workbook`).
    # It ships for the reason the aggregate is conservative: two batches with different recoveries
    # must not be readable as one, and the counts in the aggregate are only actionable beside the
    # rows they were counted from.
    "view_type_resolution_by_batch": Sanitized(scope_view_type_resolution_batches),
    "render_capability": {
        **_fields(
            "configured_api_version",
            "advertised_api_version",
            "selected_api_version",
            "selected_tier",
            "provisional",
            "capability_complete",
        ),
        "server": _fields("status", "product_version", "build", "rest_api_version"),
        "tiers": Rows(_fields("tier", "verdict", "detail", "min_api", "min_release", "answered_api")),
    },
    "views": Rows(ORACLE_VIEW_ALLOW),
}


# --------------------------------------------------------------------------------------------
# the per-document scoping functions
#
# These live beside the specs, not in `package_unit.py`, so that "what a package may contain" is
# decided in exactly one module. Round 3 found two shipped artifacts - provenance and the handover
# slice - that never entered this layer at all, which is easy to do when the layer is scattered.
# --------------------------------------------------------------------------------------------


def shippable_provenance(entries: list[dict[str, Any]], identity: dict[str, Any], unit: str) -> dict[str, Any]:
    """The provenance actually written into the package: projected, and SUPPRESSED when refused.

    ⚠️ **Round-3 finding: a refusal that does not suppress the artifact is not a refusal.** Two
    entries sharing an asset sha made `workbook_identity` correctly refuse to attribute anything -
    and the package shipped BOTH entries anyway, including a foreign `workbook_name` and `project`.
    `_provenance_luid` returns on the FIRST sha match, so which one a consumer would have believed
    is list-order chance.

    When identity was refused there is, by definition, no entry this unit is entitled to, so none is
    written; the reason travels in `scope.suppressed_reason` and in `handover.md`'s
    `ORACLE_ATTRIBUTION` line, so the refusal stays visible rather than silent.
    """
    refused = not identity.get("luid")
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    if not refused:
        kept, dropped = project(entries, Rows(PROVENANCE_ALLOW["inputs"].spec), prefix="inputs")
    scoped: dict[str, Any] = {"inputs": kept, "input_count": len(kept)}
    scoped["scoped_by"] = (
        "package_unit.py: source-provenance.json rebuilt for this unit from an allowlist, at every level"
    )
    scoped["scope"] = {
        "unit": unit,
        "kept_fields": sorted(scoped),
        "dropped_fields": dropped,
        "suppressed_reason": identity.get("reason") if refused else None,
        "reason": (
            "estate-wide, or not this unit: a one-unit handover package must not carry another "
            "unit's names, paths, status or counts. Field PATHS are listed; values are not."
        ),
    }
    return scoped


def scope_handover(payload: Any, unit: str) -> tuple[dict[str, Any], list[str]]:
    """`(shippable handover slice, redacted JSON paths)` - top-level allowlist, then value redaction.

    Two mechanisms composed, because the slice has two different risks and one treatment cannot
    close both:

    * **Top level is allowlisted**, and measured: every one of the 46 real slices in
      `_runs/407-dryrun-gates/bundle/handover` has exactly two keys, `workbook` and `estate`. No
      consumer reads `estate` - `read_handover._workbooks_from_payload` takes `workbook`/`workbooks`
      (:383-390), `check_unit` reads `payload.get("workbook")` (:1817), and `_is_handover_slice`
      (:392) ORs three markers of which `workbook` is always present. And `estate` is genuinely
      estate-wide: `definition_of_done_status` for the whole run, `pending_gates` counting 220
      stubbed calcs and 396 warned visuals across ALL 48 workbooks, and `source.root`. It is the same
      class as `report.json`'s `summary`/`pending_gates`, shipping in a second place. So it is
      dropped - 2,058 bytes of another unit's business per package.
    * **The interior is redacted by VALUE, not enumerated.** `workbook` is this unit's own work queue,
      deeply nested, engine-owned and volatile; `read_handover.py` is documented against it and
      `handover.md` is derived from it, so it must ship whole. Enumerating it would be the fourth
      allowlist in four rounds. Its residual risk is an absolute host path in a field nobody
      predicted, which is a value shape - and a value-shaped guard cannot be evaded by a new name.
    """
    if not isinstance(payload, dict):
        return {}, []
    kept = {key: value for key, value in payload.items() if key in HANDOVER_CONSUMED_KEYS}
    return redact_host_paths(kept, prefix=f"handover/{unit}.json")


def scope_report(engine_report: Any, unit: str) -> dict[str, Any]:
    """A `report.json` BUILT for this unit - and DESCOPED to what the gates measurably read.

    ⚠️ **Round 3 deleted the surface instead of enumerating it again.** Rounds 1 and 2 each closed
    one level of an allowlist and were followed by a deeper one; round 3 found
    `workbooks[0].model_facts.future_install_root` surviving inside a RETAINED container. The stop
    rule in `docs/review-throughput-postmortem.md` is *simplify, delete, split or descope - not
    another local guard*, so this now ships a NAME and nothing else.

    That is not a guess about what is safe, it is what the gates read:
    `check_reference_readiness._engine_report` (:461) returns None unless `workbooks` is a **list**,
    `._unit_names` (:478-483) reads `workbooks[].name` / `datasources[].name`, and
    `check_unit._is_engine_report` (:379) requires the list again. The engine's 29-field workbook row
    is consumed by neither, and the same account of this unit's residual work is in the handover
    slice, which is what `read_handover.py` and `handover.md` are built on. Deleting it removes
    ~90 KB per package and, more importantly, every container-valued field - so there is no deeper
    level left for a fourth round to find.

    The historical leak, for the record: measured on `HR_Dashboard` in the 48-workbook reference
    bundle, **11 of 13** top-level fields were byte-identical to the whole-estate report -
    `input_manifest.assets` listed **67** assets with absolute staged paths, `openable_outputs`
    listed **62** units, and the exact scalar `"Groups"` (a FOREIGN workbook) sat at
    `input_manifest.assets[0].name` and `openable_outputs[44].name`.

    Over-trimming is the opposite failure and is bounded by measurement: `workbooks` and
    `datasources` are always emitted, always as lists, because
    `check_reference_readiness._engine_report` (:461) returns None without it - which silently costs
    a datasource-only unit its earned `NOT_APPLICABLE` - and `check_unit._is_engine_report` (:379)
    stops recognising the package as engine output at all.
    """
    payload = engine_report if isinstance(engine_report, dict) else {}
    narrowed = dict(payload)
    # Assigned unconditionally, which is what GUARANTEES both collections exist as lists in the
    # output - a `setdefault` after projection used to sit below and was dead code, proven by the
    # mutation campaign: removing it changed nothing, because this loop has already run.
    for collection in REPORT_UNIT_LISTS:
        narrowed[collection] = [
            entry for entry in payload.get(collection) or [] if isinstance(entry, dict) and entry.get("name") == unit
        ]
    scoped, dropped = project(narrowed, REPORT_ALLOW)
    return stamp_scope(scoped, unit, dropped, "report.json")


def stamp_scope(scoped: dict[str, Any], unit: str, dropped: list[str], what: str) -> dict[str, Any]:
    """Record how a manifest was narrowed, so the omission is discoverable rather than silent.

    Field PATHS are recorded, never their values: a path like `workbooks[].future_nested` or
    `input_manifest` is engine schema, while the value is exactly the estate content being removed.
    """
    scoped["scoped_by"] = f"package_unit.py: {what} rebuilt for this unit from an allowlist, at every level"
    scoped["scope"] = {
        "unit": unit,
        "kept_fields": sorted(scoped),
        "dropped_fields": dropped,
        "reason": (
            "estate-wide, or not this unit: a one-unit handover package must not carry another "
            "unit's names, paths, status or counts. Field PATHS are listed; values are not."
        ),
    }
    return scoped


def scope_receipt(receipt: Any, unit: str) -> dict[str, Any] | None:
    """The engine receipt, narrowed to the artifacts this package actually contains.

    Copying the bundle receipt verbatim would be 780 KB per unit attesting to 3,138 artifacts, 3,135
    of which are not here. Scoped, it still answers `check_engine_receipts.py`'s only question -
    `engine.version` (:33-35) - and its `artifacts[]` hashes now name real files in the package, with
    the `pbip/<unit>/` prefix rewritten to `fabric/`.

    ⚠️ **Round-2 finding: this was still a denylist**, copying every receipt key except `artifacts`,
    so it shipped **two absolute `C:\\Users\\<user>\\...` paths** at `engine.root` and
    `engine.plugin_root`. It is now projected through `RECEIPT_ALLOW` like every other manifest -
    engine provenance is a VERSION, not a location on the machine that happened to build it.

    It still deliberately does NOT become a credential-gate exemption, and now fails closed one step
    earlier: `credential_gate._receipt_matches_bundle` raises OSError on the package's absent
    `input_manifest.json` before it ever reads the hashes this no longer carries.
    """
    if not isinstance(receipt, dict):
        return None
    prefix = f"pbip/{unit}/"
    narrowed = dict(receipt)
    narrowed["artifacts"] = [
        {**entry, "path": f"fabric/{entry['path'][len(prefix) :]}"}
        for entry in receipt.get("artifacts") or []
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"].startswith(prefix)
    ]
    scoped, dropped = project(narrowed, RECEIPT_ALLOW)
    return stamp_scope(
        scoped, unit, dropped, f"engine-output-receipt.json artifacts[] re-rooted at fabric/ from {prefix}"
    )
