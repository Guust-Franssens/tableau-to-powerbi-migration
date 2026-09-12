"""
purpose: Current package revisions and the immediate canonical PBIR inventory for review receipts.
usage:   library; imported by iteration_receipt and capture_powerbi_pages.

Filesystem traversal, portable names and strict JSON use package_filesystem's authority. This does
not verify the packaging-time contents manifest: legitimate Phase-2 edits change that baseline.
Only validation/iterations and the declared model's exact .pbi/cache.abf are excluded from the
working revision. Other .pbi bytes, including unapplied changes, remain part of the artifact.
The no-follow walk has package_filesystem's documented non-adversarial, between-syscall race limit.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import package_filesystem as filesystem
from bundle_corpus import is_reparse_entry

REVISION_PREFIX = "sha256:"
CACHE_RELPATH = (".pbi", "cache.abf")
PACKAGE_MANIFEST = "package-manifest.json"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", re.ASCII)


class RevisionError(RuntimeError):
    """Fixed, shareable refusal; never an input path or an exception's diagnostic."""

    def __init__(self, code: str, detail: str = "current artifact input refused") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CacheFacts:
    """Byte identity only, never evidence that data loaded."""

    sha256: str
    byte_count: int


@dataclass(frozen=True)
class PageInventory:
    """One PBIR page and every immediate visual directory it owns."""

    page_id: str
    display_name: str
    visual_ids: tuple[str, ...]


def read_json(path: Path) -> dict[str, Any]:
    """Use the repository strict parser, with fixed UTF-8/IO refusals at the byte boundary."""
    try:
        blob = path.read_bytes()
    except (OSError, ValueError) as error:
        raise RevisionError("JSON_UNREADABLE", "JSON input could not be read") from error
    return parse_json_bytes(blob)


def parse_json_bytes(blob: bytes) -> dict[str, Any]:
    """Parse held bytes so a receipt's JSON and checksum cannot come from different reads."""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RevisionError("JSON_NOT_UTF8", "JSON input is not valid UTF-8") from error
    try:
        return filesystem.parse_manifest_text(text)
    except filesystem._ManifestError as error:  # pylint: disable=protected-access
        raise RevisionError("JSON_INVALID", "JSON must be a unique-key finite object") from error


def tree_files(root: Path) -> tuple[dict[str, Path], set[str]]:
    """Reuse the no-follow walk; refuse root links, portable-name aliases and unreadable entries."""
    try:
        for ancestor in reversed((root.absolute(), *root.absolute().parents)):
            info = os.lstat(ancestor)
            if is_reparse_entry(info):
                raise RevisionError("REPARSE_POINT", "a tree boundary is a link or reparse point")
        if not stat.S_ISDIR(info.st_mode):
            raise RevisionError("MISSING_DIRECTORY", "the tree root is not a directory")
    except (OSError, ValueError) as error:
        raise RevisionError("TREE_UNREADABLE", "the tree boundary could not be inspected") from error
    files, findings, empty_dirs = filesystem.walk_package(root)
    if findings:
        code = (
            "REPARSE_POINT" if any(row.code == filesystem.CODE_ENTRY_REPARSE for row in findings) else "TREE_UNREADABLE"
        )
        raise RevisionError(code, "the no-follow tree walk refused an entry")
    directories = set(empty_dirs)
    for key in (*files, *empty_dirs):
        parts = key.split("/")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    aliases: set[str] = set()
    for key in (*files, *sorted(directories)):
        try:
            key.encode("utf-8")
        except UnicodeEncodeError as error:
            raise RevisionError("UNSAFE_PATH", "a tree entry has an invalid Unicode name") from error
        alias = filesystem.alias_key(key)
        if not filesystem.is_canonical_key(key) or alias in aliases:
            raise RevisionError("UNSAFE_PATH", "tree entries must have unique canonical portable names")
        aliases.add(alias)
    return files, directories


def sha256_of_file(path: Path) -> str:
    """Hash a walked regular file using the repository streaming hasher."""
    digest = filesystem._hash_file(path)  # pylint: disable=protected-access
    if digest is None:
        raise RevisionError("UNREADABLE_FILE", "a measured file could not be read")
    return digest


def _revision(root: Path, *, excluded_file: str | None = None, iterations: bool = False) -> str:
    files, _ = tree_files(root)
    digest = hashlib.sha256()
    for name, path in sorted(files.items()):
        if name == excluded_file or (iterations and name.startswith("validation/iterations/")):
            continue
        digest.update(name.encode("utf-8") + b"\0" + sha256_of_file(path).encode("ascii") + b"\n")
    return REVISION_PREFIX + digest.hexdigest()


def package_working_revision(package: Path, model_dir: Path | None = None) -> str:
    """Current package bytes, not its stale packaging-time contents declaration."""
    cache = model_dir.joinpath(*CACHE_RELPATH).relative_to(package).as_posix() if model_dir else None
    return _revision(package, excluded_file=cache, iterations=True)


def package_manifest_excluded_revision(package: Path) -> str:
    """Current package bytes under the existing revision algorithm, excluding only its manifest."""
    return _revision(package, excluded_file=PACKAGE_MANIFEST)


def report_revision(report_dir: Path) -> str:
    """All report bytes, including every .pbi file."""
    return _revision(report_dir)


def model_revision(model_dir: Path) -> str:
    """All model bytes except the exact, separately recorded persisted cache."""
    return _revision(model_dir, excluded_file="/".join(CACHE_RELPATH))


def cache_facts(model_dir: Path) -> CacheFacts | None:
    """The exact cache bytes, without interpreting existence as data proof."""
    files, _ = tree_files(model_dir)
    cache = files.get("/".join(CACHE_RELPATH))
    if cache is None:
        return None
    return CacheFacts(sha256_of_file(cache), cache.stat().st_size)


def _definition(files: dict[str, Path], name: str, code: str) -> dict[str, Any]:
    if name not in files:
        raise RevisionError(code, "an immediate inventory directory has no canonical definition")
    return read_json(files[name])


def _id_agrees(value: Any, directory: str, code: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or value != directory:
        raise RevisionError(code, "the definition name must equal its canonical directory name")


def _immediate(directories: set[str], prefix: str) -> list[str]:
    return sorted(
        name[len(prefix) :] for name in directories if name.startswith(prefix) and "/" not in name[len(prefix) :]
    )


def report_inventory(report_dir: Path) -> list[PageInventory]:  # pylint: disable=too-many-locals
    """Enumerate directories first; deleting a definition can never shrink the denominator."""
    files, directories = tree_files(report_dir)
    prefix = "definition/pages/"
    order = _definition(files, prefix + "pages.json", "NO_PAGE_ORDER").get("pageOrder")
    if (
        not isinstance(order, list)
        or not order
        or any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in order)
        or len(set(order)) != len(order)
    ):
        raise RevisionError("PAGE_ORDER_INVALID", "pageOrder must contain unique nonempty canonical strings")
    found: dict[str, PageInventory] = {}
    expected_definitions: set[str] = set()
    for page_id in _immediate(directories, prefix):
        page_key = f"{prefix}{page_id}/page.json"
        page = _definition(files, page_key, "PAGE_DEFINITION_MISSING")
        _id_agrees(page.get("name"), page_id, "PAGE_ID_MISMATCH")
        display = page.get("displayName")
        if not isinstance(display, str) or not display.strip():
            raise RevisionError("PAGE_INVALID", "a page must declare a nonempty displayName")
        expected_definitions.add(page_key)
        visual_ids: list[str] = []
        for visual_id in _immediate(directories, f"{prefix}{page_id}/visuals/"):
            key = f"{prefix}{page_id}/visuals/{visual_id}/visual.json"
            visual = _definition(files, key, "VISUAL_DEFINITION_MISSING")
            _id_agrees(visual.get("name"), visual_id, "VISUAL_ID_MISMATCH")
            body = visual.get("visual")
            group = visual.get("visualGroup")
            if not (
                isinstance(body, dict) and isinstance(body.get("visualType"), str) and body["visualType"].strip()
            ) and not isinstance(group, dict):
                raise RevisionError("VISUAL_INVALID", "a visual definition needs a visual type or group")
            expected_definitions.add(key)
            visual_ids.append(visual_id)
        found[page_id] = PageInventory(page_id, display, tuple(visual_ids))
    definitions = {
        key
        for key in files
        if key.startswith(prefix) and key.rsplit("/", 1)[-1].casefold() in {"page.json", "visual.json"}
    }
    if definitions != expected_definitions:
        raise RevisionError("NONCANONICAL_DEFINITION", "a page or visual definition is outside its immediate directory")
    if set(order) != set(found):
        raise RevisionError("PAGE_SET_MISMATCH", "pageOrder and immediate page directories disagree")
    return [found[page_id] for page_id in order]
