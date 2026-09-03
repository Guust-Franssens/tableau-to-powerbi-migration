"""
purpose: answer path questions in the FLAVOUR of the literal being asked about - Windows or POSIX -
         rather than in the flavour of whichever machine happens to be running. Containment,
         separator choice, composition and leaf extraction all change answer with flavour, and a
         packager that reads a customer's `.tmdl` on one platform and ships it to another must not
         let the host decide.
usage:   library only - imported by scripts/package_unit.py and scripts/set_data_folder.py.

Why this module exists at all, measured (blind review of PR #463, round 2):

* `_inside()` in the packager always used `PureWindowsPath`, whose comparison is CASE-INSENSITIVE.
  On Linux a source at `/data/Extract.csv` was therefore judged to be inside a package at
  `/DATA`, was skipped by localization AND by the post-rewrite scan, and landed in the clean
  bucket - no shipment, no omission, no rewrite. Silence, on a data-loss-shaped question.
* `_classify_source()` used the host's `Path`. On Windows `Path("/Users/x/README.md").is_file()`
  is resolved against the CURRENT DRIVE, so a foreign macOS literal happily matched
  `C:\\Users\\x\\README.md` and unrelated local bytes were packaged as the customer's source.
* `set_data_folder.py` composed every rewritten value with a literal backslash, so on POSIX it
  wrote `/tmp/package\\data\\...` - one path segment with backslashes inside it - reported the
  folder missing, exited 1, and left the file already rewritten to that invalid value.

All three have the same shape: a question about a STRING was answered with the semantics of the
machine. So flavour is decided lexically, first, and everything else follows from it.

⚠️ **Nothing here touches the filesystem.** Containment is a question about the string; probing a
UNC literal blocks on SMB name resolution for minutes (PR #462 measured one test module going from
30 seconds to 52 minutes), and `Path.resolve()` on a foreign literal is exactly the reinterpretation
described above. Callers that must probe ask :func:`is_host_native` first.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from pathlib import PurePath, PurePosixPath, PureWindowsPath

#: The two flavours a path literal can be written in. A value that is neither - a relative path, or
#: this repo's `<REPO_ROOT>` / `<PACKAGE_ROOT>` placeholder - has NO flavour, and every function
#: here treats that as "unknown", never as a default.
WINDOWS = "windows"
POSIX = "posix"

DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
UNC_RE = re.compile(r"^\\\\")


def flavour(value: str) -> str | None:
    """`WINDOWS` / `POSIX` / None for one path literal, decided LEXICALLY.

    A drive letter (`C:\\`, `C:/`) and a UNC prefix (`\\\\host\\share`) are unambiguously Windows.
    A leading `/` is POSIX. Everything else - a relative path, an empty string, a placeholder token -
    has no flavour, and callers must not invent one for it.
    """
    if not isinstance(value, str) or not value:
        return None
    if DRIVE_RE.match(value) or UNC_RE.match(value):
        return WINDOWS
    if value.startswith("/"):
        return POSIX
    return None


def host_flavour() -> str:
    """The flavour of the machine running this process."""
    return WINDOWS if os.name == "nt" else POSIX


def is_host_native(value: str) -> bool:
    """Whether ``value`` may safely be handed to `Path` on THIS machine.

    False for a foreign-flavour literal, which the host would silently reinterpret rather than
    refuse: on Windows `Path("/Users/x/f.csv")` resolves against the current drive.
    """
    return flavour(value) == host_flavour()


def pure(kind: str) -> type[PurePath]:
    """The `PurePath` class for one flavour - Windows compares case-insensitively, POSIX does not."""
    return PureWindowsPath if kind == WINDOWS else PurePosixPath


def normalize(value: str, kind: str) -> str:
    """Collapse `.`, `..` and duplicate separators IN ``kind``'s semantics, touching no filesystem.

    `os.path.normpath` is the host's, which is the defect this module exists for; `ntpath` and
    `posixpath` are importable everywhere and are pure string manipulation.
    """
    return (ntpath if kind == WINDOWS else posixpath).normpath(value)


def inside(root: str | os.PathLike[str], value: str) -> bool:
    """Whether the literal ``value`` points inside ``root``, judged lexically and per flavour.

    Two literals of DIFFERENT flavour are never contained in one another - a `C:\\...` source is not
    inside a `/tmp/...` package however the strings compare - and a flavourless literal (relative,
    or a placeholder) is not absolute, so it is not "outside the package" either; callers that care
    about that distinction ask :func:`flavour` directly.
    """
    kind = flavour(value)
    if kind is None or kind != flavour(str(root)):
        return False
    cls = pure(kind)
    try:
        candidate = cls(normalize(value, kind))
        anchor = cls(normalize(str(root), kind))
    except (TypeError, ValueError):
        return False
    return candidate == anchor or candidate.is_relative_to(anchor)


def separator(base: str) -> str:
    """The separator a value EXTENDED from ``base`` must use, so the finished path is one flavour.

    Flavour first, so `C:/runs/out` (a Windows path written with forward slashes) still answers
    `\\`. Only a flavourless base falls back to what it happens to contain, and only an empty one
    to the host - "does it contain a backslash" is a guess, and is the inference Finding 4 named.
    """
    kind = flavour(base)
    if kind is not None:
        return "\\" if kind == WINDOWS else "/"
    if "\\" in base:
        return "\\"
    if "/" in base:
        return "/"
    return os.sep


def join(base: str, *segments: str, trailing: bool = False) -> str:
    """Compose ``base`` with ``segments`` using BASE's own separator, optionally trailing.

    The trailing separator is load-bearing wherever a model concatenates a file name onto a folder
    parameter (`File.Contents(#"SourceFolder" & "Sample.xlsx")`), so it is produced here rather than
    by each caller appending its own guess.

    Each segment may arrive with either separator - a package-relative destination is stored POSIX -
    so both are rewritten to the base's.
    """
    sep = separator(base)
    stem = base.rstrip("\\/")
    if not stem:
        stem = "" if base.startswith(("/", "\\")) else base
    parts = [stem]
    for segment in segments:
        parts.extend(part for part in re.split(r"[\\/]+", segment) if part)
    return sep.join(parts) + (sep if trailing else "")


def leaf(value: str) -> str:
    """The last segment of a path literal, read with BOTH separators.

    `Path(value).name` is the host's answer: on POSIX the Windows source id
    `_runs\\999-x\\assets\\minimal.twb` has no separators at all, so its "name" is the whole string
    and an asset that is present cannot be resolved.
    """
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or value
