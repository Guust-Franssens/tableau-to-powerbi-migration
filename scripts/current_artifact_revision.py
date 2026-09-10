#!/usr/bin/env python
"""
purpose: Derive the CURRENT revision and page/visual inventory of a phase-2 package's own bytes.
usage:   library, no CLI. Imported by scripts/iteration_receipt.py (and, later, by check_unit.py).

Why this exists
---------------
Every phase-2 completion claim is a claim about a MOMENT: "these screenshots, this report, this
model". A receipt that names no revision cannot be stale, so it can never be wrong - it silently
certifies whatever is on disk at read time. `package_unit.package_contents` is the packaging-time
BASELINE (what packaging wrote), which is a different question and goes stale the instant an agent
legitimately edits the working copy, so it is deliberately not reused here.

Three separate revisions, because three different edits must be distinguishable:

* :func:`package_working_revision` - the whole package EXCEPT ``validation/iterations``. That
  exclusion is not cosmetic: a receipt lives inside the package, so a revision that included it
  could never be recorded in the thing it measures.
* :func:`report_revision` - every file that affects the rendered report (``definition.pbir``,
  ``definition/**``, theme and static resources), so a theme swap or a filter edit invalidates a
  capture just as a `visual.json` edit does.
* :func:`model_revision` - the model's definition files (``definition/**``, ``definition.pbism``).

``.pbi/`` is excluded from both artifact revisions and hashed separately by :func:`cache_facts`:
``cache.abf`` is DATA, not definition, and ``localSettings.json`` is Desktop-local churn that would
otherwise invalidate a receipt merely because someone opened the file.

Every walk refuses a reparse point (junction/symlink) rather than following it. A package is
addressed by a caller-supplied path; following a link out of it would let evidence be sourced from
somewhere the package does not own, and on Windows a junction is invisible to a lexical check.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REVISION_PREFIX = "sha256:"

#: Package-relative directories a package revision must NOT include. ``validation/iterations`` holds
#: the receipts themselves; including it makes the revision unrecordable (self-reference).
PACKAGE_EXCLUDED_RELPATHS = ("validation/iterations",)

#: Desktop-local state that lives inside a `.Report`/`.SemanticModel` folder but is not definition.
DESKTOP_LOCAL_DIRNAME = ".pbi"

CACHE_RELPATH = (DESKTOP_LOCAL_DIRNAME, "cache.abf")


class RevisionError(RuntimeError):
    """A named refusal. ``code`` is the machine-readable half; tests assert on it, not on prose."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CacheFacts:
    """What the model's persisted data cache currently IS, when there is one."""

    sha256: str
    byte_count: int


@dataclass(frozen=True)
class PageInventory:
    """One current PBIR page: its id, its display name, and the visual ids it currently carries."""

    page_id: str
    display_name: str
    visual_ids: tuple[str, ...]


def sha256_of_file(path: Path) -> str:
    """sha256 of one file's bytes, or a named refusal when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise RevisionError("UNREADABLE_FILE", f"{_shown(path)} could not be read ({error.strerror})") from error


def _shown(path: Path) -> str:
    """A path rendered for an ERROR message only - never for a receipt (see iteration_receipt)."""
    return path.name


def _is_reparse_point(path: Path) -> bool:
    """True for a symlink, junction or any other reparse point, WITHOUT following it."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def assert_no_reparse_points(root: Path) -> None:
    """Refuse a tree containing any link. Evidence must come from bytes the package itself owns."""
    if _is_reparse_point(root):
        raise RevisionError("REPARSE_POINT", f"{_shown(root)} is a symlink/junction, not a real directory")
    for parent, dir_names, file_names in os.walk(root):
        for name in list(dir_names) + list(file_names):
            candidate = Path(parent) / name
            if _is_reparse_point(candidate):
                raise RevisionError("REPARSE_POINT", f"{name} is a symlink/junction inside {_shown(root)}")


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _tree_files(
    root: Path,
    *,
    excluded_relpaths: tuple[str, ...] = (),
    excluded_dir_names: tuple[str, ...] = (),
) -> list[tuple[str, Path]]:
    """`(package-relative posix path, file)` for every file the revision covers, sorted."""
    if not root.is_dir():
        raise RevisionError("MISSING_DIRECTORY", f"{_shown(root)} is not a directory")
    assert_no_reparse_points(root)
    found: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = _relative_posix(path, root)
        parts = relative.split("/")
        if any(name in excluded_dir_names for name in parts[:-1]):
            continue
        if any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in excluded_relpaths):
            continue
        found.append((relative, path))
    return found


def _digest_of(entries: list[tuple[str, Path]]) -> str:
    """One revision over a set of files: path AND content, so a rename is a change too."""
    digest = hashlib.sha256()
    for relative, path in entries:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(sha256_of_file(path).encode("ascii"))
        digest.update(b"\n")
    return f"{REVISION_PREFIX}{digest.hexdigest()}"


def package_working_revision(package: Path) -> str:
    """The package's CURRENT bytes, excluding the receipts it stores about itself.

    ``.pbi/`` is excluded for the same reason it is excluded from the artifact revisions: merely
    OPENING the report in Desktop rewrites ``localSettings.json``, and a revision that churned on
    that would invalidate every iteration for a reason that has nothing to do with the artifact. The
    data cache inside it is not lost - :func:`cache_facts` hashes it separately and the receipt
    records it, so a refresh is still detected.
    """
    return _digest_of(
        _tree_files(
            package,
            excluded_relpaths=PACKAGE_EXCLUDED_RELPATHS,
            excluded_dir_names=(DESKTOP_LOCAL_DIRNAME,),
        )
    )


def report_revision(report_dir: Path) -> str:
    """Every file that affects the rendered report - not only `visual.json`."""
    return _digest_of(_tree_files(report_dir, excluded_dir_names=(DESKTOP_LOCAL_DIRNAME,)))


def model_revision(model_dir: Path) -> str:
    """The model's current DEFINITION files. `.pbi/` is data, and is reported separately."""
    return _digest_of(_tree_files(model_dir, excluded_dir_names=(DESKTOP_LOCAL_DIRNAME,)))


def cache_facts(model_dir: Path) -> CacheFacts | None:
    """The persisted data cache's identity, or None when the model has none.

    ⚠️ Existence is NOT data proof and this function does not claim it is - it answers "which cache
    bytes was this receipt written against", so that a later refresh invalidates the claim.
    """
    cache = model_dir.joinpath(*CACHE_RELPATH)
    if not cache.is_file():
        return None
    if _is_reparse_point(cache):
        raise RevisionError("REPARSE_POINT", "cache.abf is a symlink/junction, not a real file")
    return CacheFacts(sha256=sha256_of_file(cache), byte_count=cache.stat().st_size)


def _page_document(page_json: Path) -> dict[str, Any]:
    try:
        payload = json.loads(page_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RevisionError("PAGE_UNREADABLE", f"{page_json.parent.name}/page.json could not be read") from error
    if not isinstance(payload, dict):
        raise RevisionError("PAGE_UNREADABLE", f"{page_json.parent.name}/page.json is not a JSON object")
    return payload


def _visual_ids(page_dir: Path) -> tuple[str, ...]:
    """Current visual ids on one page, read from each `visual.json`'s own declared name."""
    visuals_root = page_dir / "visuals"
    if not visuals_root.is_dir():
        return ()
    ids: list[str] = []
    for visual_json in sorted(visuals_root.rglob("visual.json")):
        try:
            payload = json.loads(visual_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RevisionError(
                "VISUAL_UNREADABLE", f"{visual_json.parent.name}/visual.json could not be read"
            ) from error
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise RevisionError("VISUAL_UNREADABLE", f"{visual_json.parent.name}/visual.json declares no name")
        ids.append(name)
    if len(set(ids)) != len(ids):
        raise RevisionError("VISUAL_ID_DUPLICATE", f"two visuals on page {page_dir.name} declare the same id")
    return tuple(sorted(ids))


def report_inventory(report_dir: Path) -> list[PageInventory]:
    """The report's CURRENT page and visual inventory, in `pages.json` order.

    ``pages.json`` is REQUIRED and must list exactly the pages that have definitions. That is the
    report's own statement of which pages exist; without it there is nothing to check the discovered
    folders against, and a capture output would end up being its own denominator.
    """
    pages_root = report_dir / "definition" / "pages"
    if not pages_root.is_dir():
        raise RevisionError("NO_PAGES", f"{report_dir.name} has no definition/pages folder")
    assert_no_reparse_points(pages_root)
    found: dict[str, PageInventory] = {}
    for page_json in sorted(pages_root.rglob("page.json")):
        payload = _page_document(page_json)
        page_id = payload.get("name")
        if not isinstance(page_id, str) or not page_id.strip():
            raise RevisionError("PAGE_UNREADABLE", f"{page_json.parent.name}/page.json declares no page id")
        if page_id in found:
            raise RevisionError("PAGE_ID_DUPLICATE", f"two page definitions both declare the id {page_id!r}")
        display = payload.get("displayName")
        found[page_id] = PageInventory(
            page_id=page_id,
            display_name=display if isinstance(display, str) and display.strip() else page_id,
            visual_ids=_visual_ids(page_json.parent),
        )
    order = _page_order(pages_root)
    declared = [str(item) for item in order]
    if sorted(set(declared)) != sorted(found):
        raise RevisionError(
            "PAGE_SET_MISMATCH",
            "pages.json and the page definitions do not describe the same page set",
        )
    return [found[page_id] for page_id in declared]


def _page_order(pages_root: Path) -> list[Any]:
    try:
        payload = json.loads((pages_root / "pages.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RevisionError("NO_PAGE_ORDER", "pages.json is missing or unreadable") from error
    order = payload.get("pageOrder") if isinstance(payload, dict) else None
    if not isinstance(order, list) or not order:
        raise RevisionError("NO_PAGE_ORDER", "pages.json declares no non-empty list pageOrder")
    return order
