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
from typing import Any


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
    if isinstance(spec, dict):
        if not isinstance(payload, dict):
            return {}, [prefix or "."]
        kept: dict[str, Any] = {}
        dropped = []
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in spec:
                dropped.append(path)
                continue
            projected, lost = project(value, spec[key], prefix=path)
            kept[key] = projected
            dropped.extend(lost)
        return kept, sorted(set(dropped))
    raise TypeError(f"unusable allowlist spec at {prefix or '.'}: {spec!r}")


# --------------------------------------------------------------------------------------------
# the value-shaped half: absolute host paths, wherever they appear
# --------------------------------------------------------------------------------------------

#: An absolute path under a user profile, in the forms this repo's artifacts actually produce.
#: Deliberately the same shape `scripts/set_data_folder.py` gates the repo on, so a package cannot
#: ship what a commit could not. Matched against a PARSED string value, never against serialized
#: text - `json.dumps` doubles each separator, and grepping the render for the single-separator form
#: is how an earlier assertion in this feature was silently vacuous.
HOST_PATH_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/]|/Users/|/home/)",
)
REDACTED = "<redacted-absolute-path>"


def redact_host_paths(payload: Any, *, prefix: str = "") -> tuple[Any, list[str]]:
    """`(payload with absolute host paths replaced, JSON paths redacted)`.

    The complement to `project()`, for documents that must ship whole. It closes by VALUE SHAPE, so
    a field name nobody predicted cannot evade it - which is precisely the property the three
    name-shaped rounds lacked. It redacts rather than drops: the handover slice is the agent's work
    queue, and deleting a key it reads would trade a leak for a broken deliverable.
    """
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        hit: list[str] = []
        for key, value in payload.items():
            cleaned, found = redact_host_paths(value, prefix=f"{prefix}.{key}")
            out[key] = cleaned
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
    if isinstance(payload, str) and HOST_PATH_RE.match(payload):
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
    **{leg: ORACLE_LEG_SPEC for leg in ("image", "svg", "pdf", "data")},
}

#: `oracle-manifest.json`. Everything counting the ESTATE RUN is dropped and RECOMPUTED from the
#: packaged views (see `_scope_oracle_manifest`); everything identifying another unit is dropped
#: outright. `render_capability.probe_view_luid`/`probe_view_name`/`probe_view_luids` name the view
#: the capability ladder was probed against, which on the reference estate is `'Overview'` in the
#: FOREIGN `Superstore` workbook; `warnings` is free text that can quote that same view, so it goes
#: too. What survives is what tells a consumer which GRADE of evidence it got.
ORACLE_MANIFEST_ALLOW: dict[str, Any] = {
    **_fields("schema", "captured_at", "server", "site", "rest_api_version"),
    "requested_renders": SCALAR_LIST,
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
