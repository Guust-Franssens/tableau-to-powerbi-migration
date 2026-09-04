"""
purpose: refuse a named migration-target request that does not resolve UNAMBIGUOUSLY within its
         declared CLASS - the guard for issue #368, where a request to migrate the PUBLISHED
         datasource `DS_CAPS` was silently applied instead to a similarly (but not identically)
         named, workbook-EMBEDDED datasource `IA_CAPS_DS`. That workbook was then built, validated
         and reported on - the wrong artifact shipped clean.
usage:   python scripts/resolve_datasource_target.py --raw <assess_estate --out dir> \\
             --name DS_CAPS --class published
         python scripts/resolve_datasource_target.py --raw <assess_estate --out dir> \\
             --name DS_CAPS --class published --json out.json

Why a CLASS guard, not a name check
------------------------------------
`DS_CAPS` and `IA_CAPS_DS` are not the same string - a duplicate-name check would not have caught
this. The failure was an agent free-hand matching a REQUESTED name against the wrong CLASS of
object, because nothing forced it to declare which class it meant and have a tool CONFIRM the
answer. A published datasource and a workbook-embedded one are different objects that can
legitimately share a display name; resolving by name alone, across both classes, is the defect.

Three outcomes, kept structurally distinguishable
--------------------------------------------------
A caller must not be able to collapse "nothing matched" into "something matched, ambiguously", or
the reverse - that is exactly how a wrong target passes as a right one. So there are three, and
only three, outcomes:

  RESOLVED   exactly one candidate in the REQUESTED class, and none in the OTHER class - proceed.
  ABSENT     no candidate in the requested class ANYWHERE in the estate - refuse. May still name a
             same-name hit in the OTHER class (the informative half of the trap: never mention a
             wrong-class hit as if it satisfied the request, but never hide it either).
  AMBIGUOUS  more than one candidate in the requested class, or at least one in EACH class - refuse.

Nothing here falls back to a normalized or fuzzy name. `object_identity.normalize()` is quarantined
to `object_identity.py` itself (enforced by `check_identity_normalization.py`), and a near-match is
precisely the failure mode this script exists to refuse, not paper over: matching is EXACT string
equality only, never case-folded, never whitespace-collapsed.

Data source
-----------
Reads the raw evidence `assess_estate.py` already collects and writes to `<out>/raw/*.json`
(``--raw`` may point at either that directory or the ``--out`` directory itself):

  * ``datasources.json`` - REST ``/datasources`` listing: PUBLISHED datasources, each with an `id`
    (LUID) and a `project`.
  * ``structure.json``   - one GraphQL call: `workbooks[].embeddedDatasources[].name` (EMBEDDED,
    scoped to the owning workbook - an embedded datasource has no LUID of its own).
  * ``workbooks.json``   - REST ``/workbooks`` listing, joined by NAME to `structure.json`'s
    workbook nodes to recover the owning workbook's LUID/project for an embedded hit. A workbook
    name that is itself not unique in the estate leaves that join unresolved rather than guessing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("resolve_datasource_target")

#: The two classes a named target may be requested against. Deliberately just these two - a
#: request naming neither is a usage error, not a silent default.
CLASS_PUBLISHED = "published"
CLASS_EMBEDDED = "embedded"
CLASSES = (CLASS_PUBLISHED, CLASS_EMBEDDED)

#: The three distinguishable outcomes. Only ``RESOLVED`` may proceed.
RESOLVED = "resolved"
ABSENT = "absent"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Candidate:
    """One datasource-shaped object found in the raw estate evidence, exact name kept as-is."""

    name: str
    cls: str
    luid: str | None
    project: str | None
    owner_workbook: str | None = None


class _ExactIndex:
    """Candidates grouped by their EXACT name, readable only through a cardinality check.

    Same idiom as ``check_unit.NormalizedIndex``: candidates are appended (never overwritten) so
    multiplicity survives, and the only accessor collapses BOTH "nothing answers to this name" and
    "more than one thing does" to ``None`` - a caller cannot pick ``[0]`` and call it resolved.
    Unlike that index, the key here is the name EXACTLY as given - no lossy fold - because an
    inexact match is the one thing this guard must never paper over.
    """

    def __init__(self) -> None:
        self.__buckets: dict[str, list[Candidate]] = {}

    def add(self, candidate: Candidate) -> None:
        """Record one candidate under its exact name, keeping earlier ones with the same name."""
        self.__buckets.setdefault(candidate.name, []).append(candidate)

    def matches(self, name: str) -> list[Candidate]:
        """Every candidate exactly named ``name``. Reporting only - never index one out."""
        return list(self.__buckets.get(name, ()))

    def all_names(self) -> list[str]:
        """Every name that has at least one candidate, for an estate-wide sweep."""
        return sorted(self.__buckets)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_raw(raw_dir: Path) -> dict[str, Any]:
    """Load the three raw evidence files `assess_estate.py` writes, tolerating a missing one.

    ``raw_dir`` may be the ``--out`` directory itself (which holds a `raw/` subfolder) or that
    `raw/` subfolder directly, so this works whether a caller points at the assessment root or the
    evidence folder within it.
    """
    candidates = raw_dir / "raw" if (raw_dir / "raw").is_dir() else raw_dir
    raw: dict[str, Any] = {}
    for key in ("datasources", "structure", "workbooks"):
        path = candidates / f"{key}.json"
        raw[key] = _read_json(path) if path.is_file() else (None if key == "structure" else [])
    return raw


def published_candidates(raw: dict[str, Any]) -> list[Candidate]:
    """PUBLISHED datasources from the REST `/datasources` listing - each carries its own LUID."""
    return [
        Candidate(
            name=item.get("name") or "",
            cls=CLASS_PUBLISHED,
            luid=item.get("id"),
            project=(item.get("project") or {}).get("name"),
        )
        for item in raw.get("datasources") or []
        if item.get("name")
    ]


def _workbook_index(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """REST `/workbooks` rows grouped by exact name, for joining an embedded hit to its owner."""
    index: dict[str, list[dict[str, Any]]] = {}
    for wb in raw.get("workbooks") or []:
        name = wb.get("name")
        if name:
            index.setdefault(name, []).append(wb)
    return index


def embedded_candidates(raw: dict[str, Any]) -> list[Candidate]:
    """EMBEDDED datasources from the GraphQL structure pass, one per workbook that declares one.

    An embedded datasource has no LUID of its own; the owning workbook's LUID/project are reported
    instead, and only when the workbook's OWN name is unique in the estate - a workbook name that
    collides leaves that join unresolved (``None``) rather than attributing to the wrong owner.
    """
    structure = raw.get("structure") or {}
    workbooks_by_name = _workbook_index(raw)
    out: list[Candidate] = []
    for node in structure.get("workbooks") or []:
        owner_name = node.get("name")
        owners = workbooks_by_name.get(owner_name, [])
        owner = owners[0] if len(owners) == 1 else None
        for ds in node.get("embeddedDatasources") or []:
            name = ds.get("name")
            if not name:
                continue
            out.append(
                Candidate(
                    name=name,
                    cls=CLASS_EMBEDDED,
                    luid=owner.get("id") if owner else None,
                    project=(owner.get("project") or {}).get("name") if owner else None,
                    owner_workbook=owner_name,
                )
            )
    return out


def build_index(raw: dict[str, Any]) -> _ExactIndex:
    """Index every published AND embedded candidate the raw evidence carries, by exact name."""
    index = _ExactIndex()
    for candidate in (*published_candidates(raw), *embedded_candidates(raw)):
        index.add(candidate)
    return index


@dataclass(frozen=True)
class Resolution:
    """The result of resolving one named target against its declared class."""

    outcome: str
    requested_name: str
    requested_class: str
    target: Candidate | None
    requested_class_matches: tuple[Candidate, ...]
    other_class_matches: tuple[Candidate, ...]

    def as_dict(self) -> dict[str, Any]:
        """The JSON-serialisable shape, for `--json` output and tests."""

        def _c(candidate: Candidate) -> dict[str, Any]:
            return {
                "name": candidate.name,
                "class": candidate.cls,
                "luid": candidate.luid,
                "project": candidate.project,
                "owner_workbook": candidate.owner_workbook,
            }

        return {
            "outcome": self.outcome,
            "requested_name": self.requested_name,
            "requested_class": self.requested_class,
            "target": _c(self.target) if self.target else None,
            "requested_class_matches": [_c(c) for c in self.requested_class_matches],
            "other_class_matches": [_c(c) for c in self.other_class_matches],
        }


def resolve(raw: dict[str, Any], name: str, cls: str) -> Resolution:
    """Resolve ``name`` within the requested class ``cls``. Never falls back across classes.

    ``cls`` must be one of :data:`CLASSES`; a caller asking for anything else is a usage error, not
    a silent default (checked by the CLI, ``argparse.choices``).
    """
    other_cls = CLASS_EMBEDDED if cls == CLASS_PUBLISHED else CLASS_PUBLISHED
    all_matches = build_index(raw).matches(name)
    requested = tuple(c for c in all_matches if c.cls == cls)
    other = tuple(c for c in all_matches if c.cls == other_cls)

    if len(requested) == 1 and not other:
        outcome, target = RESOLVED, requested[0]
    elif not requested:
        # Matches nothing in the requested class - genuinely absent, whether or not the OTHER
        # class happens to answer to the same name. Never promoted to a resolution: that promotion
        # is exactly issue #368 (a published request silently satisfied by an embedded hit).
        outcome, target = ABSENT, None
    else:
        # requested has >1 candidate (duplicate name within the class), or the OTHER class also
        # answers to this exact name - either way the name matches ACROSS classes or within one
        # ambiguously, and picking one by preference order is the failure this guard exists for.
        outcome, target = AMBIGUOUS, None

    return Resolution(
        outcome=outcome,
        requested_name=name,
        requested_class=cls,
        target=target,
        requested_class_matches=requested,
        other_class_matches=other,
    )


def datasource_class_hazards(raw: dict[str, Any]) -> dict[str, Any]:
    """Estate-wide, once-computed hazard report: every name that is not a safe unique lookup.

    Two hazard shapes, reported separately so an operator reads WHY a name is risky rather than
    just THAT it is:

      duplicate_within_class - the same exact name answers to more than one candidate of ONE class
                                (e.g. two projects each publish a datasource named the same).
      cross_class            - the same exact name answers to at least one candidate in EACH class.

    Computed once from the same evidence :func:`resolve` reads, so the hazard a request would hit
    is visible to a human BEFORE anyone picks a target, not discovered mid-migration.
    """
    index = build_index(raw)
    duplicate_within_class: list[dict[str, Any]] = []
    cross_class: list[dict[str, Any]] = []
    for name in index.all_names():
        matches = index.matches(name)
        by_class: dict[str, list[Candidate]] = {}
        for candidate in matches:
            by_class.setdefault(candidate.cls, []).append(candidate)
        for cls, entries in by_class.items():
            if len(entries) > 1:
                duplicate_within_class.append({"name": name, "class": cls, "count": len(entries)})
        if len(by_class) > 1:
            cross_class.append({"name": name, "classes": sorted(by_class)})
    return {
        "duplicate_within_class": sorted(duplicate_within_class, key=lambda row: (row["name"], row["class"])),
        "cross_class": sorted(cross_class, key=lambda row: row["name"]),
    }


def _render(resolution: Resolution) -> str:
    lines = [f"target      : {resolution.requested_name!r} (requested class: {resolution.requested_class})"]
    if resolution.outcome == RESOLVED:
        target = resolution.target
        assert target is not None  # narrowed by outcome == RESOLVED
        lines += [
            "outcome     : RESOLVED - proceed",
            f"  class     : {target.cls}",
            f"  luid      : {target.luid or '<none>'}",
            f"  project   : {target.project or '<none>'}",
        ]
        if target.owner_workbook:
            lines.append(f"  owned by workbook : {target.owner_workbook}")
        return "\n".join(lines)
    if resolution.outcome == ABSENT:
        lines.append(f"outcome     : ABSENT - no {resolution.requested_class} datasource named this exists - REFUSED")
        if resolution.other_class_matches:
            lines.append(
                f"  NOTE: {len(resolution.other_class_matches)} match(es) exist in the OTHER class "
                f"({resolution.other_class_matches[0].cls}) - that is NOT this request; fix --class "
                "or --name, do not proceed on it."
            )
        return "\n".join(lines)
    lines.append("outcome     : AMBIGUOUS - REFUSED")
    if len(resolution.requested_class_matches) > 1:
        lines.append(f"  {len(resolution.requested_class_matches)} candidate(s) within {resolution.requested_class}:")
        for candidate in resolution.requested_class_matches:
            lines.append(f"    - luid={candidate.luid or '<none>'} project={candidate.project or '<none>'}")
    if resolution.other_class_matches:
        lines.append(f"  {len(resolution.other_class_matches)} candidate(s) in the OTHER class also match this name:")
        for candidate in resolution.other_class_matches:
            lines.append(
                f"    - class={candidate.cls} luid={candidate.luid or '<none>'} project={candidate.project or '<none>'}"
            )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", required=True, type=Path, help="assess_estate.py --out dir, or its raw/ subfolder")
    parser.add_argument("--name", required=True, help="the EXACT requested target name")
    parser.add_argument("--class", dest="cls", required=True, choices=CLASSES, help="the requested object class")
    parser.add_argument("--json", type=Path, help="also write the machine-readable resolution here")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit 0 RESOLVED, 1 ABSENT, 2 AMBIGUOUS."""
    args = _build_parser().parse_args(argv)
    raw = load_raw(args.raw)
    resolution = resolve(raw, args.name, args.cls)
    print(_render(resolution))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(resolution.as_dict(), indent=2) + "\n", encoding="utf-8")
    return {RESOLVED: 0, ABSENT: 1, AMBIGUOUS: 2}[resolution.outcome]


if __name__ == "__main__":
    sys.exit(main())
