"""
purpose: strict census + deterministic revision digest for ONE local PBIR report and its bound model (#363 slice A1a).
usage:   import pbir_revision; pbir_revision.establish_revision(fabric_root, report_dir)

What this is
------------
Slice A1a of #363 and nothing else: given a `fabric/` root that somebody else has already selected and
ONE report artifact inside it, either return a strict typed census of every byte that a local capture
would render from - plus a deterministic digest over exactly those bytes - or refuse and say why.

Deliberately NOT here (later slices own them): package-manifest selection, Power BI Desktop, PIDs,
`reload`, screenshots, iteration allocation, receipts, comparison, sign-off. There is no live
evidence in this module, so it can never claim that Desktop LOADED these bytes - only what is on
disk right now. The independent #363 A1 audit is explicit that a disk-only check is not a
loaded-revision oracle; A1b adds the PID-scoped barrier on top of this census.

Why a closed census rather than a glob
--------------------------------------
The digest is only worth having if the set it covers is decided by an explicit rule. A permissive
`rglob("*")` silently adopts whatever a future Desktop build writes beside the definition - the exact
shape that lets a report change while its "revision" does not, or lets a machine-local file make two
identical reports disagree. So every entry inside the report and the model is either:

* **included** - a byte that a render or a deploy depends on, or
* **excluded by an explicit named rule** (`.pbi/` local Desktop state, `TMDLScripts/` authoring
  scratch, volatile sidecars), or
* **refused** - unknown, so this module does not get to guess.

The allowed sets below were derived from the committed corpus, not assumed. Measured on this repo at
`bf4a79f1` across `examples/*/fabric` (16 reports, 36 pages, 869 visuals):

* report top level is exactly `.platform`, `definition.pbir`, `definition/`, `StaticResources/`;
* `definition/` is exactly `version.json`, `report.json`, `pages/`;
* a page folder holds `page.json` and (in `fixtures/large-refresh`, zero-visual) an OPTIONAL
  `visuals/`; a visual folder holds exactly `visual.json`;
* **page folder name == `page.json.name` in 36/36 cases**, so the folder IS the page document id;
* **visual folder name != `visual.json.name` in 133/869 cases**, so the folder is NOT the visual
  document id - both are recorded, and uniqueness is asserted on the document id, report-wide;
* model top level is `.platform`, `definition.pbism`, `definition/` plus one committed
  `TMDLScripts/`; `definition/` is `model.tmdl`, `database.tmdl`, `relationships.tmdl`,
  `expressions.tmdl`, `cultures/`, `tables/`.

Anything outside those sets refuses. That is fail-closed on purpose: an unsupported shape is not
clean, and widening the allow-list is a deliberate edit with corpus evidence behind it.

Privacy
-------
Every field this module returns - including refusal detail and evidence - is relative to the
`fabric_root` the caller passed, or an opaque token. No absolute path, drive, user name or machine
path reaches `repr()`, `dataclasses.asdict()` or a refusal message.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Framing tag for the digest. A change in what is covered, or how it is framed, MUST bump this.
VERSION_TAG = "pbir-revision/v1"

REPORT_SUFFIX = ".Report"
MODEL_SUFFIX = ".SemanticModel"
PBIP_SUFFIX = ".pbip"

# --------------------------------------------------------------------------------------------
# The closed allow-lists (see the module docstring for the corpus measurement behind each).
# --------------------------------------------------------------------------------------------

#: Report top-level files that are included in the revision.
REPORT_FILES = (".platform", "definition.pbir")
#: Report top-level directories that are censused.
STATIC_RESOURCES_DIR = "StaticResources"
REPORT_DIRS = ("definition", STATIC_RESOURCES_DIR)
#: `definition/` files that are included in the revision.
DEFINITION_FILES = ("report.json", "version.json")
#: The only directory `definition/` may hold.
DEFINITION_DIRS = ("pages",)
#: The only file a page directory may hold, and the only directory it may hold.
PAGE_FILE = "page.json"
PAGE_DIR = "visuals"
#: The only file a visual directory may hold.
VISUAL_FILE = "visual.json"
#: The only directory `StaticResources/` may hold. `SharedResources` base themes are built into
#: Power BI and are referenced by `report.json` without existing on disk - see `_registered_paths`.
REGISTERED_RESOURCES_DIR = "RegisteredResources"
STATIC_RESOURCE_DIRS = (REGISTERED_RESOURCES_DIR,)
#: Model top-level files: `.platform` is optional (absent in several committed test fixtures).
MODEL_REQUIRED_FILES = ("definition.pbism",)
MODEL_OPTIONAL_FILES = (".platform",)
#: Model `definition/` entries. `model.tmdl` is required; the rest are optional but allowed.
MODEL_DEFINITION_REQUIRED = ("model.tmdl",)
MODEL_DEFINITION_OPTIONAL = ("database.tmdl", "relationships.tmdl", "expressions.tmdl")
MODEL_DEFINITION_DIRS = ("cultures", "tables")

#: Directories excluded by name, with the reason recorded in the census.
EXCLUDED_DIRS = {
    ".pbi": "local-desktop-state",
    "TMDLScripts": "authoring-scratch",
}
#: Volatile file sidecars, excluded by an explicit closed rule rather than by a wildcard sweep. Each
#: clause carries its own control in `tests/test_pbir_revision.py`; a clause nothing exercises is
#: untested surface, so `localSettings.json` was dropped - Desktop writes it inside `.pbi/`, which
#: the directory rule above already covers.
VOLATILE_SUFFIXES = (".abf", ".tmp", ".autosave", ".bak")
VOLATILE_PREFIXES = ("~$",)

# --------------------------------------------------------------------------------------------
# Refusal codes. Every one of them names positive evidence, never "something felt wrong".
# --------------------------------------------------------------------------------------------

ROOT_UNUSABLE = "root_unusable"
REPORT_UNUSABLE = "report_unusable"
REPORT_NOT_CONTAINED = "report_not_contained"
PATH_REPARSE = "path_reparse"
PATH_IDENTITY_MISMATCH = "path_identity_mismatch"
PBIP_ABSENT = "pbip_absent"
PBIP_MALFORMED = "pbip_malformed"
PBIP_REPORT_UNRELATED = "pbip_report_unrelated"
PBIP_REPORT_AMBIGUOUS = "pbip_report_ambiguous"
JSON_MALFORMED = "json_malformed"
JSON_DUPLICATE_KEY = "json_duplicate_key"
JSON_TYPE = "json_type"
ENTRY_UNKNOWN = "entry_unknown"
ENTRY_MISSING = "entry_missing"
ENTRY_TYPE = "entry_type"
BINDING_MALFORMED = "binding_malformed"
BINDING_REMOTE = "binding_remote"
BINDING_AMBIGUOUS = "binding_ambiguous"
MODEL_UNRESOLVED = "model_unresolved"
MODEL_NOT_CONTAINED = "model_not_contained"
MODEL_NOT_A_MODEL = "model_not_a_model"
PAGE_ORDER_MALFORMED = "page_order_malformed"
PAGE_MISSING = "page_missing"
PAGE_ORPHAN = "page_orphan"
PAGE_ID_MISMATCH = "page_id_mismatch"
PAGE_ID_DUPLICATE = "page_id_duplicate"
VISUAL_ID_INVALID = "visual_id_invalid"
VISUAL_ID_DUPLICATE = "visual_id_duplicate"
RESOURCE_MALFORMED = "resource_malformed"
RESOURCE_DANGLING = "resource_dangling"

#: Opaque stand-ins used when the offending path cannot be expressed relative to `fabric_root`.
OPAQUE_ROOT = "<fabric-root>"
OPAQUE_REPORT = "<report-dir>"
OPAQUE_MODEL = "<model-dir>"


class _Refused(Exception):
    """Internal control flow: a refusal raised deep in the walk and caught at the entry point."""

    def __init__(self, code: str, detail: str, evidence: Iterable[str] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.evidence = tuple(evidence)


@dataclass(frozen=True)
class VisualCensus:
    """One visual: its folder identity AND its document identity, which routinely differ."""

    page_id: str
    folder: str
    document_id: str
    file: str


@dataclass(frozen=True)
class PageCensus:
    """One page in `pageOrder` position, with its visuals in folder order."""

    page_id: str
    folder: str
    display_name: str
    file: str
    visuals: tuple[VisualCensus, ...] = ()


@dataclass(frozen=True)
class RevisionRefusal:
    """An explicit refusal. Falsy so that `if revision:` fails closed rather than fails open."""

    code: str
    detail: str
    evidence: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return False


@dataclass(frozen=True)
class PbirRevision:  # pylint: disable=too-many-instance-attributes  # one census: locators, order, pages, files, digest
    """The census of one report plus its bound model, and the digest over exactly those bytes."""

    version: str
    pbip: str
    report: str
    model: str
    model_binding: str
    page_order: tuple[str, ...]
    pages: tuple[PageCensus, ...]
    files: tuple[tuple[str, str], ...]
    excluded: tuple[tuple[str, str], ...]
    digest: str

    def __bool__(self) -> bool:
        return True

    @property
    def visual_count(self) -> int:
        """Total visuals across all pages - the number a capture wrapper reports."""
        return sum(len(page.visuals) for page in self.pages)


@dataclass
class _Walk:
    """Mutable accumulator for one establishment attempt. Never returned to a caller."""

    root: Path
    included: dict[str, bytes] = field(default_factory=dict)
    excluded: list[tuple[str, str]] = field(default_factory=list)

    def rel(self, path: Path) -> str:
        """Fabric-relative POSIX spelling, or an opaque token when the path escapes the root."""
        try:
            return Path(os.path.relpath(_lexical(path), _lexical(self.root))).as_posix()
        except ValueError:
            return OPAQUE_ROOT

    def include(self, path: Path) -> bytes:
        """Read one file into the revision and return its bytes."""
        rel = self.rel(path)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise _Refused(ENTRY_MISSING, f"{rel} could not be read ({error.strerror})", [rel]) from error
        self.included[rel] = data
        return data

    def exclude(self, path: Path, reason: str) -> None:
        """Record an entry that an explicit rule keeps OUT of the revision."""
        self.excluded.append((self.rel(path), reason))


# --------------------------------------------------------------------------------------------
# Path primitives
# --------------------------------------------------------------------------------------------


def _lexical(path: Path) -> str:
    """Absolute spelling WITHOUT resolving reparse points - the caller's own spelling, normalised."""
    return os.path.abspath(str(path))


def _is_reparse(path: Path) -> bool:
    """Whether this entry itself is a symlink, junction or any other reparse point."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _components(root: Path, path: Path) -> list[Path]:
    """Every path component from ``root`` (inclusive) down to ``path`` (inclusive)."""
    relative = Path(os.path.relpath(_lexical(path), _lexical(root)))
    walked = [root]
    current = root
    for part in relative.parts:
        current = current / part
        walked.append(current)
    return walked


def _reject_reparse(walk: _Walk, root: Path, path: Path) -> None:
    """Refuse when any component between ``root`` and ``path`` is a reparse point.

    Ancestors ABOVE the root are the caller's declared boundary and are out of scope here; A1b owns
    the identity of the root itself against a live Desktop instance.
    """
    for component in _components(root, path):
        if _is_reparse(component):
            rel = walk.rel(component)
            raise _Refused(PATH_REPARSE, f"{rel} is a symlink/junction/reparse point", [rel])


def _reject_uncontained(walk: _Walk, root: Path, path: Path, code: str, token: str) -> str:
    """Refuse unless ``path`` is a strict descendant of ``root`` both lexically and resolved."""
    lexical = os.path.relpath(_lexical(path), _lexical(root))
    try:
        resolved = os.path.relpath(str(path.resolve()), str(root.resolve()))
    except (OSError, ValueError) as error:
        raise _Refused(code, f"{token} could not be resolved for containment", [token]) from error
    if lexical.startswith("..") or os.path.isabs(lexical) or lexical == os.curdir:
        raise _Refused(code, f"{token} is not contained under the fabric root", [token])
    if lexical != resolved:
        raise _Refused(
            PATH_IDENTITY_MISMATCH,
            f"{token} spells one path lexically and another once resolved",
            [token],
        )
    _reject_reparse(walk, root, path)
    return Path(lexical).as_posix()


def _entries(walk: _Walk, directory: Path) -> list[Path]:
    """Directory children in a filesystem-order-independent sequence.

    Sorted on the UTF-8 bytes of the entry name so two machines that enumerate a directory in
    different orders - or with different locales - produce the same census and the same digest.
    """
    try:
        children = list(directory.iterdir())
    except OSError as error:
        rel = walk.rel(directory)
        raise _Refused(ENTRY_MISSING, f"{rel} could not be listed ({error.strerror})", [rel]) from error
    return sorted(children, key=lambda child: child.name.encode("utf-8"))


def _volatile(name: str) -> bool:
    """Whether a file name is an excluded volatile sidecar, by the explicit closed rule."""
    lowered = name.casefold()
    return any(lowered.endswith(suffix) for suffix in VOLATILE_SUFFIXES) or any(
        name.startswith(prefix) for prefix in VOLATILE_PREFIXES
    )


# --------------------------------------------------------------------------------------------
# Strict JSON
# --------------------------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """`json.load` keeps the LAST duplicate key silently; this refuses the document instead."""
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-JSON constant {name}")


def _load_object(walk: _Walk, path: Path) -> dict[str, object]:
    """Read one JSON object strictly: duplicate keys, NaN/Infinity and non-objects all refuse."""
    rel = walk.rel(path)
    data = walk.include(path)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _Refused(JSON_MALFORMED, f"{rel} is not UTF-8", [rel]) from error
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as error:
        code = JSON_DUPLICATE_KEY if "duplicate key" in str(error) else JSON_MALFORMED
        raise _Refused(code, f"{rel} is not strict JSON: {error}", [rel]) from error
    if not isinstance(document, dict):
        raise _Refused(JSON_TYPE, f"{rel} is not a JSON object", [rel])
    return document


def _string(walk: _Walk, document: dict[str, object], key: str, path: Path) -> str:
    """One required non-empty string field."""
    rel = walk.rel(path)
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _Refused(JSON_TYPE, f"{rel} has no non-empty string {key!r}", [rel])
    return value


def _reject_unknown_keys(walk: _Walk, document: dict[str, object], allowed: Iterable[str], path: Path) -> None:
    """Refuse a load-bearing document that carries a key this module does not understand."""
    unknown = sorted(set(document) - set(allowed))
    if unknown:
        rel = walk.rel(path)
        raise _Refused(ENTRY_UNKNOWN, f"{rel} has unsupported keys {unknown}", [rel])


# --------------------------------------------------------------------------------------------
# Directory census helpers
# --------------------------------------------------------------------------------------------


def _classify(walk: _Walk, entry: Path) -> str:
    """`excluded`, `dir` or `file` - and a refusal for a reparse point or an odd entry type."""
    if _is_reparse(entry):
        rel = walk.rel(entry)
        raise _Refused(PATH_REPARSE, f"{rel} is a symlink/junction/reparse point", [rel])
    if entry.is_dir():
        reason = EXCLUDED_DIRS.get(entry.name)
        if reason:
            walk.exclude(entry, reason)
            return "excluded"
        return "dir"
    if entry.is_file():
        if _volatile(entry.name):
            walk.exclude(entry, "volatile-sidecar")
            return "excluded"
        return "file"
    rel = walk.rel(entry)
    raise _Refused(ENTRY_TYPE, f"{rel} is neither a regular file nor a directory", [rel])


def _census_dir(walk: _Walk, directory: Path, files: Iterable[str], dirs: Iterable[str]) -> dict[str, Path]:
    """Census one directory against a closed allow-list and return the entries that were kept.

    An entry that is neither an allowed file nor an allowed directory refuses - this is the rule
    that stops a future Desktop build's new sidecar from being silently adopted into a revision.
    """
    allowed_files, allowed_dirs = set(files), set(dirs)
    kept: dict[str, Path] = {}
    for entry in _entries(walk, directory):
        kind = _classify(walk, entry)
        if kind == "excluded":
            continue
        expected = allowed_files if kind == "file" else allowed_dirs
        if entry.name not in expected:
            rel = walk.rel(entry)
            raise _Refused(ENTRY_UNKNOWN, f"{rel} is not a supported {kind} entry", [rel])
        kept[entry.name] = entry
    return kept


def _require(walk: _Walk, kept: dict[str, Path], names: Iterable[str], parent: Path) -> None:
    """Refuse when a required entry is absent from a censused directory."""
    for name in names:
        if name not in kept:
            rel = walk.rel(parent / name)
            raise _Refused(ENTRY_MISSING, f"{rel} is required and absent", [rel])


def _include_tree(walk: _Walk, directory: Path) -> None:
    """Include every regular file under ``directory``, applying the same exclusion rules."""
    for entry in _entries(walk, directory):
        kind = _classify(walk, entry)
        if kind == "excluded":
            continue
        if kind == "dir":
            _include_tree(walk, entry)
        else:
            walk.include(entry)


# --------------------------------------------------------------------------------------------
# PBIP relation
# --------------------------------------------------------------------------------------------


def _artifact_report_paths(walk: _Walk, pbip: Path) -> list[str]:
    """The report paths one `.pbip` declares, refusing any unknown artifact kind or odd path."""
    rel = walk.rel(pbip)
    document = _load_object(walk, pbip)
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise _Refused(PBIP_MALFORMED, f"{rel} has no artifacts array", [rel])
    paths: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or sorted(artifact) != ["report"]:
            raise _Refused(PBIP_MALFORMED, f"{rel} has an artifact that is not exactly one report", [rel])
        report = artifact["report"]
        if not isinstance(report, dict) or not isinstance(report.get("path"), str):
            raise _Refused(PBIP_MALFORMED, f"{rel} has a report artifact without a string path", [rel])
        paths.append(report["path"])
    return paths


def _relative_child(base: Path, spelling: str, code: str, token: str) -> Path:
    """Resolve a declared relative path, refusing absolute, drive-shaped or backslash spellings."""
    if not spelling or spelling != spelling.strip():
        raise _Refused(code, f"{token} declares an empty or padded path", [token])
    if "\\" in spelling or os.path.isabs(spelling) or ":" in spelling:
        raise _Refused(code, f"{token} declares a non-portable path spelling", [token])
    return base / spelling


def _matching_pbip(walk: _Walk, root: Path, report_dir: Path) -> Path:
    """The single `.pbip` in the fabric root that declares THIS report.

    Every immediate `.pbip` is parsed, not just the first match: proving that exactly one project
    claims this report is the whole point, and a malformed sibling would otherwise hide a second
    claim.     A fabric root with no `.pbip` at all refuses - Power BI Desktop opens the `.pbip`, so a root
    without one is not a local capture target. Matching is on the EXACT declared spelling: a
    `.pbip` that names a differently-cased report is treated as declaring another report, because
    the audit measured Desktop preserving whatever spelling it was opened with.
    """
    candidates: list[Path] = []
    for entry in _entries(walk, root):
        if entry.suffix.casefold() != PBIP_SUFFIX:
            continue
        kind = _classify(walk, entry)
        if kind == "dir":
            rel = walk.rel(entry)
            raise _Refused(ENTRY_TYPE, f"{rel} is a directory, not a .pbip project file", [rel])
        if kind == "file":
            candidates.append(entry)
    if not candidates:
        raise _Refused(PBIP_ABSENT, "the fabric root holds no .pbip project file", [OPAQUE_ROOT])
    target = _lexical(report_dir)
    matches: list[Path] = []
    for pbip in candidates:
        rel = walk.rel(pbip)
        declared = [
            _relative_child(root, spelling, PBIP_MALFORMED, rel) for spelling in _artifact_report_paths(walk, pbip)
        ]
        hits = [path for path in declared if _lexical(path) == target]
        if len(hits) > 1:
            raise _Refused(PBIP_REPORT_AMBIGUOUS, f"{rel} declares this report more than once", [rel])
        if hits:
            matches.append(pbip)
    if not matches:
        raise _Refused(PBIP_REPORT_UNRELATED, "no .pbip in the fabric root declares this report", [OPAQUE_REPORT])
    if len(matches) > 1:
        raise _Refused(
            PBIP_REPORT_AMBIGUOUS,
            "more than one .pbip declares this report",
            sorted(walk.rel(match) for match in matches),
        )
    # Keep only the matching project's bytes: a sibling project is not part of this revision.
    for pbip in candidates:
        if pbip != matches[0]:
            walk.included.pop(walk.rel(pbip), None)
            walk.exclude(pbip, "other-project")
    return matches[0]


# --------------------------------------------------------------------------------------------
# Report definition census
# --------------------------------------------------------------------------------------------


def _page_order(walk: _Walk, pages_json: Path) -> tuple[list[str], str | None]:
    """`pages.json` is the page-order authority; it must be a non-empty list of unique ids."""
    rel = walk.rel(pages_json)
    document = _load_object(walk, pages_json)
    _reject_unknown_keys(walk, document, ("$schema", "pageOrder", "activePageName"), pages_json)
    order = document.get("pageOrder")
    if not isinstance(order, list) or not order:
        raise _Refused(PAGE_ORDER_MALFORMED, f"{rel} has no non-empty pageOrder", [rel])
    if any(not isinstance(name, str) or not name.strip() for name in order):
        raise _Refused(PAGE_ORDER_MALFORMED, f"{rel} has a non-string pageOrder entry", [rel])
    if len(set(order)) != len(order):
        raise _Refused(PAGE_ORDER_MALFORMED, f"{rel} repeats a pageOrder entry", [rel])
    active = document.get("activePageName")
    if active is not None and (not isinstance(active, str) or active not in order):
        raise _Refused(PAGE_ORDER_MALFORMED, f"{rel} names an activePageName outside pageOrder", [rel])
    return list(order), active


def _visuals(walk: _Walk, page_dir: Path, page_id: str) -> list[VisualCensus]:
    """Census one page's `visuals/`. The folder is not identity; `visual.json.name` is."""
    visuals_dir = page_dir / PAGE_DIR
    if not visuals_dir.is_dir():
        return []
    censused: list[VisualCensus] = []
    for entry in _entries(walk, visuals_dir):
        kind = _classify(walk, entry)
        if kind == "excluded":
            continue
        if kind != "dir":
            rel = walk.rel(entry)
            raise _Refused(ENTRY_UNKNOWN, f"{rel} is a file directly inside visuals/", [rel])
        kept = _census_dir(walk, entry, (VISUAL_FILE,), ())
        _require(walk, kept, (VISUAL_FILE,), entry)
        document = _load_object(walk, kept[VISUAL_FILE])
        censused.append(
            VisualCensus(
                page_id=page_id,
                folder=entry.name,
                document_id=_string(walk, document, "name", kept[VISUAL_FILE]),
                file=walk.rel(kept[VISUAL_FILE]),
            )
        )
    return censused


def _page_directories(walk: _Walk, pages_dir: Path) -> dict[str, Path]:
    """Every page directory under `definition/pages/`, refusing any other entry there.

    Deliberately NOT expressed as an allow-list of the `pageOrder` names: a directory that is not in
    `pageOrder` has to refuse as a page ORPHAN (with the page-order authority named), not as a
    generic unknown entry, or the caller cannot tell a stale page from a stray file.
    """
    found: dict[str, Path] = {}
    for entry in _entries(walk, pages_dir):
        kind = _classify(walk, entry)
        if kind == "excluded":
            continue
        if kind == "file":
            if entry.name != "pages.json":
                rel = walk.rel(entry)
                raise _Refused(ENTRY_UNKNOWN, f"{rel} is not a supported file entry", [rel])
            continue
        found[entry.name] = entry
    return found


def _one_page(walk: _Walk, page_dir: Path, page_id: str) -> PageCensus:
    """Census one page directory. The folder name IS the page document id (36/36 committed pages)."""
    entries = _census_dir(walk, page_dir, (PAGE_FILE,), (PAGE_DIR,))
    _require(walk, entries, (PAGE_FILE,), page_dir)
    document = _load_object(walk, entries[PAGE_FILE])
    document_id = _string(walk, document, "name", entries[PAGE_FILE])
    if document_id != page_id:
        rel = walk.rel(entries[PAGE_FILE])
        raise _Refused(PAGE_ID_MISMATCH, f"{rel} names {document_id!r}, not its folder", [rel])
    return PageCensus(
        page_id=page_id,
        folder=page_dir.name,
        display_name=_string(walk, document, "displayName", entries[PAGE_FILE]),
        file=walk.rel(entries[PAGE_FILE]),
        visuals=tuple(_visuals(walk, page_dir, page_id)),
    )


def _pages(walk: _Walk, pages_dir: Path, order: list[str]) -> tuple[PageCensus, ...]:
    """Census every page directory, requiring exact agreement with `pageOrder`."""
    kept = _page_directories(walk, pages_dir)
    missing = sorted(set(order) - set(kept))
    if missing:
        raise _Refused(PAGE_MISSING, f"pageOrder names {missing} with no page directory", missing)
    orphan = sorted(set(kept) - set(order))
    if orphan:
        raise _Refused(PAGE_ORPHAN, f"page directories {orphan} are absent from pageOrder", orphan)
    censused: list[PageCensus] = []
    seen_visuals: dict[str, str] = {}
    for page_id in order:
        page = _one_page(walk, kept[page_id], page_id)
        for visual in page.visuals:
            if visual.document_id in seen_visuals:
                raise _Refused(
                    VISUAL_ID_DUPLICATE,
                    f"visual document id {visual.document_id!r} appears twice in this report",
                    sorted({seen_visuals[visual.document_id], visual.file}),
                )
            seen_visuals[visual.document_id] = visual.file
        censused.append(page)
    return tuple(censused)


def _registered_paths(walk: _Walk, report_json: Path) -> list[str]:
    """Registered-resource paths declared by `report.json`.

    Only `RegisteredResources` packages are resolved on disk. `SharedResources` base themes
    (`BaseThemes/CY24SU10.json` in 15 of 16 committed examples) ship inside Power BI and are
    correctly absent from the report folder, so resolving them would refuse every real report.
    """
    rel = walk.rel(report_json)
    document = _load_object(walk, report_json)
    packages = document.get("resourcePackages", [])
    if not isinstance(packages, list):
        raise _Refused(RESOURCE_MALFORMED, f"{rel} has a non-list resourcePackages", [rel])
    declared: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            raise _Refused(RESOURCE_MALFORMED, f"{rel} has a non-object resource package", [rel])
        if package.get("type") != REGISTERED_RESOURCES_DIR:
            continue
        items = package.get("items")
        if not isinstance(items, list):
            raise _Refused(RESOURCE_MALFORMED, f"{rel} has a resource package without items", [rel])
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise _Refused(RESOURCE_MALFORMED, f"{rel} has a resource item without a string path", [rel])
            declared.append(item["path"])
    return declared


def _static_resources(walk: _Walk, report_dir: Path, declared: list[str]) -> None:
    """Include every committed static resource, and refuse a registration that resolves nowhere.

    The asymmetry is deliberate. A registration with no file BREAKS the render, so it refuses. A file
    with no registration does not, so it is included: its bytes still move the digest, which is what
    makes an edit to an unregistered theme visible.
    """
    static_dir = report_dir / STATIC_RESOURCES_DIR
    registered = static_dir / REGISTERED_RESOURCES_DIR
    if static_dir.is_dir():
        _census_dir(walk, static_dir, (), STATIC_RESOURCE_DIRS)
        _include_tree(walk, static_dir)
    for spelling in declared:
        target = _relative_child(registered, spelling, RESOURCE_MALFORMED, "report.json")
        rel = walk.rel(target)
        if not target.is_file() or _is_reparse(target):
            raise _Refused(RESOURCE_DANGLING, f"report.json registers {spelling!r}, which is not a file", [rel])


# --------------------------------------------------------------------------------------------
# Model binding and census
# --------------------------------------------------------------------------------------------


def _bound_model(walk: _Walk, root: Path, report_dir: Path, pbir: Path) -> tuple[Path, str]:
    """The single local `byPath` model this report binds to, refusing every other binding shape."""
    rel = walk.rel(pbir)
    document = _load_object(walk, pbir)
    _reject_unknown_keys(walk, document, ("$schema", "version", "datasetReference"), pbir)
    reference = document.get("datasetReference")
    if not isinstance(reference, dict) or not reference:
        raise _Refused(BINDING_MALFORMED, f"{rel} has no datasetReference object", [rel])
    if "byConnection" in reference:
        raise _Refused(BINDING_REMOTE, f"{rel} binds byConnection, which is not a local capture target", [rel])
    if sorted(reference) != ["byPath"]:
        raise _Refused(BINDING_AMBIGUOUS, f"{rel} does not bind exactly one byPath", [rel])
    by_path = reference["byPath"]
    if not isinstance(by_path, dict) or sorted(by_path) != ["path"] or not isinstance(by_path["path"], str):
        raise _Refused(BINDING_MALFORMED, f"{rel} has a malformed byPath", [rel])
    spelling = by_path["path"]
    candidate = _relative_child(report_dir, spelling, BINDING_MALFORMED, rel)
    if _lexical(candidate) == _lexical(report_dir):
        raise _Refused(MODEL_NOT_A_MODEL, f"{rel} binds the report directory as its model", [rel])
    if not candidate.is_dir():
        raise _Refused(MODEL_UNRESOLVED, f"{rel} binds a path that is not an existing directory", [rel])
    if not candidate.name.endswith(MODEL_SUFFIX):
        raise _Refused(MODEL_NOT_A_MODEL, f"{rel} binds a directory that is not a {MODEL_SUFFIX}", [rel])
    _reject_uncontained(walk, root, candidate, MODEL_NOT_CONTAINED, OPAQUE_MODEL)
    return candidate, spelling


def _census_model(walk: _Walk, model_dir: Path) -> None:
    """Include every deployable model definition byte; exclude local state by the named rules."""
    top = _census_dir(walk, model_dir, MODEL_REQUIRED_FILES + MODEL_OPTIONAL_FILES, ("definition",))
    _require(walk, top, MODEL_REQUIRED_FILES + ("definition",), model_dir)
    for name in MODEL_REQUIRED_FILES + MODEL_OPTIONAL_FILES:
        if name in top:
            _load_object(walk, top[name])
    definition = top["definition"]
    allowed_files = MODEL_DEFINITION_REQUIRED + MODEL_DEFINITION_OPTIONAL
    kept = _census_dir(walk, definition, allowed_files, MODEL_DEFINITION_DIRS)
    _require(walk, kept, MODEL_DEFINITION_REQUIRED, definition)
    for name, path in kept.items():
        if name in MODEL_DEFINITION_DIRS:
            for entry in _entries(walk, path):
                kind = _classify(walk, entry)
                if kind == "excluded":
                    continue
                if kind != "file" or not entry.name.endswith(".tmdl"):
                    rel = walk.rel(entry)
                    raise _Refused(ENTRY_UNKNOWN, f"{rel} is not a .tmdl file", [rel])
                walk.include(entry)
        else:
            walk.include(path)


# --------------------------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------------------------


def _framed(digest: Any, payload: bytes) -> None:
    """Length-delimit every field so no concatenation of two fields can imitate another."""
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def revision_digest(files: Iterable[tuple[str, bytes]]) -> str:
    """SHA-256 over the version tag plus every included path and its raw bytes, length-delimited.

    Sorted on the UTF-8 bytes of the fabric-relative path, so the digest never depends on directory
    enumeration order, and framed so that a path/content boundary cannot be shifted silently.
    """
    digest = hashlib.sha256()
    _framed(digest, VERSION_TAG.encode("utf-8"))
    for rel, data in sorted(files, key=lambda item: item[0].encode("utf-8")):
        _framed(digest, rel.encode("utf-8"))
        _framed(digest, data)
    return digest.hexdigest()


def _file_hashes(walk: _Walk) -> tuple[tuple[str, str], ...]:
    """Per-file SHA-256, so a consumer can name WHICH file moved when a digest changes."""
    return tuple(
        (rel, hashlib.sha256(data).hexdigest())
        for rel, data in sorted(walk.included.items(), key=lambda item: item[0].encode("utf-8"))
    )


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def _census_report_definition(
    walk: _Walk, report_dir: Path, definition_dir: Path
) -> tuple[list[str], tuple[PageCensus, ...]]:
    """The report's `definition/` tree plus its static resources, in `pageOrder` authority order."""
    definition = _census_dir(walk, definition_dir, DEFINITION_FILES, DEFINITION_DIRS)
    _require(walk, definition, DEFINITION_FILES + DEFINITION_DIRS, definition_dir)
    _load_object(walk, definition["version.json"])
    _static_resources(walk, report_dir, _registered_paths(walk, definition["report.json"]))
    order, _active = _page_order(walk, definition["pages"] / "pages.json")
    return order, _pages(walk, definition["pages"], order)


def _establish(walk: _Walk, root: Path, report_dir: Path) -> PbirRevision:
    """The whole census, with every refusal raised as `_Refused` and caught by the caller."""
    if not root.is_dir():
        raise _Refused(ROOT_UNUSABLE, "the fabric root is not an existing directory", [OPAQUE_ROOT])
    if not report_dir.is_dir():
        raise _Refused(REPORT_UNUSABLE, "the report artifact is not an existing directory", [OPAQUE_REPORT])
    if not report_dir.name.endswith(REPORT_SUFFIX):
        raise _Refused(REPORT_UNUSABLE, f"the report artifact is not a {REPORT_SUFFIX} directory", [OPAQUE_REPORT])
    report_rel = _reject_uncontained(walk, root, report_dir, REPORT_NOT_CONTAINED, OPAQUE_REPORT)

    pbip = _matching_pbip(walk, root, report_dir)
    top = _census_dir(walk, report_dir, REPORT_FILES, REPORT_DIRS)
    _require(walk, top, REPORT_FILES + ("definition",), report_dir)
    _load_object(walk, top[".platform"])
    model_dir, binding = _bound_model(walk, root, report_dir, top["definition.pbir"])
    order, pages = _census_report_definition(walk, report_dir, top["definition"])
    _census_model(walk, model_dir)

    return PbirRevision(
        version=VERSION_TAG,
        pbip=walk.rel(pbip),
        report=report_rel,
        model=walk.rel(model_dir),
        model_binding=binding,
        page_order=tuple(order),
        pages=pages,
        files=_file_hashes(walk),
        excluded=tuple(sorted(walk.excluded)),
        digest=revision_digest(walk.included.items()),
    )


def establish_revision(fabric_root: Path | str, report_dir: Path | str) -> PbirRevision | RevisionRefusal:
    """Census one already-selected report under ``fabric_root`` and digest its revision.

    Returns a :class:`PbirRevision` when every rule above holds, or a :class:`RevisionRefusal`
    naming the first contradicted rule. It never raises for a bad tree, never repairs anything, and
    never reads or writes outside ``fabric_root``.
    """
    root, report = Path(fabric_root), Path(report_dir)
    walk = _Walk(root=root)
    try:
        return _establish(walk, root, report)
    except _Refused as refusal:
        return RevisionRefusal(code=refusal.code, detail=refusal.detail, evidence=refusal.evidence)


def iter_included(revision: PbirRevision) -> Iterator[str]:
    """The fabric-relative paths the revision covers, in digest order."""
    for rel, _sha in revision.files:
        yield rel
