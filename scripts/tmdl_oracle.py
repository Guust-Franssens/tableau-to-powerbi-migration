"""
purpose: TMDL oracle driver - ask the real parser (AMO TmdlSerializer) whether a model loads, and
         read the parse back to find properties an expression silently swallowed.
usage:   imported by scripts/check_datamodel.py; run that, not this.
internal: true
internal-reason: a library with no CLI of its own. `python scripts/check_datamodel.py` is the
                 agent-facing entry point and already carries this gate.

Why an oracle instead of a scanner
----------------------------------
Two hand-written TMDL grammars shipped in issue #254 - one matching property NAMES, one enforcing
the documented INDENTATION contract - and blind review broke both, in each case with false
negatives AND false positives:

  * `measure Probe =` / `1` / `isHidden`, indented five or eight spaces, parses clean and loses the
    property; the indentation scanner passed it, because it capped an indentation level at one tab
    while AMO accepts wider units.
  * `measure Probe = 1` / `IsHidden` is VALID (AMO sets IsHidden=True; TMDL keywords are
    case-insensitive) and the scanner rejected it.
  * `role R` / `tablePermission A = ...` / `tablePermission B = ...` is VALID and the scanner
    rejected the second one, because `tablePermission` was missing from one of two hand-kept lists.

The pattern is the defect: a grammar re-implementation is a completeness claim, and a completeness
claim over someone else's parser cannot be finished. So this module makes no such claim. It asks
`TmdlSerializer.DeserializeDatabaseFromFolder` - the parser Power BI Desktop itself uses - and
reports what it says. Whatever AMO accepts is accepted here, by construction.

The one thing the parser cannot answer is silent absorption: a property written at the wrong indent
is swallowed into the preceding expression, and the document is still well-formed, so nothing
throws. That is answered by READBACK - the expression text is compared against the property
vocabulary of the object that owns it, and that vocabulary is taken by REFLECTION over the TOM type
(see tools/tmdl_oracle/Program.cs), never from a list kept by hand.

Requires the .NET SDK. `scripts/preflight.ps1` already checks for it; where it is genuinely absent
the caller degrades loudly rather than reporting a pass it did not earn.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tmdl_checks import TmdlFinding

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = REPO_ROOT / "tools" / "tmdl_oracle"
DLL = PROJECT_DIR / "bin" / "Release" / "net9.0" / "tmdl_oracle.dll"

# A first build restores the AMO package from nuget.org; later runs reuse it and take ~2s.
BUILD_TIMEOUT_SECONDS = 600
RUN_TIMEOUT_SECONDS = 600
# Keep a command line comfortably inside the Windows 32 767-character limit.
BATCH_SIZE = 32

log = logging.getLogger("tmdl_oracle")

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_COLON_PROPERTY_RE = re.compile(rf"^({_IDENTIFIER})[ \t]*:")
_BARE_PROPERTY_RE = re.compile(rf"^({_IDENTIFIER})$")


class OracleUnavailable(RuntimeError):
    """The oracle could not be run at all - never confuse this with a clean model."""


@dataclass(frozen=True)
class Absorption:
    """One expression line that TMDL read as expression text but names a real property."""

    index: int
    name: str
    text: str


def scrub(text: str) -> str:
    """Blank out string literals and comments, preserving line structure and length.

    Property-shaped text inside an M block comment or a multi-line string is expression content,
    not a swallowed property. An earlier revision of this gate reported exactly that as a defect,
    so the scrub runs before anything is classified. Length is preserved so line numbers and
    column offsets still line up with the original.
    """
    out = list(text)
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            index = _blank_string(text, out, index)
            continue
        if char == "#" and index + 1 < length and text[index + 1] == '"':
            out[index] = " "
            index = _blank_string(text, out, index + 1)
            continue
        if text.startswith("//", index) or text.startswith("--", index):
            index = _blank_until(text, out, index, "\n")
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = _blank_until(text, out, index, "*/") if end >= 0 else _blank_until(text, out, index, "\x00")
            continue
        index += 1
    return "".join(out)


def _blank_string(text: str, out: list[str], start: int) -> int:
    """Blank a double-quoted literal beginning at `start`, honouring the `""` escape."""
    out[start] = " "
    index = start + 1
    while index < len(text):
        if text[index] == '"':
            if index + 1 < len(text) and text[index + 1] == '"':
                out[index] = out[index + 1] = " "
                index += 2
                continue
            out[index] = " "
            return index + 1
        if text[index] != "\n":
            out[index] = " "
        index += 1
    return index


def _blank_until(text: str, out: list[str], start: int, terminator: str) -> int:
    """Blank from `start` up to and including `terminator` (newlines survive)."""
    end = text.find(terminator, start + 1)
    stop = len(text) if end < 0 else end + len(terminator)
    for index in range(start, stop):
        if text[index] != "\n":
            out[index] = " "
    return stop


def absorbed_properties(text: str, names: set[str], unset_booleans: set[str]) -> list[Absorption]:
    """Expression lines that TMDL would have read as properties had they been indented correctly.

    Two shapes, and the split between them is what keeps this free of false positives:

      * ``name: value`` - unambiguous TMDL property syntax. Neither DAX nor M produces an
        identifier followed by a colon at the start of a line, once strings and comments are
        scrubbed, so any property name is enough.
      * a bare ``name`` - TMDL's boolean shortcut, but also what an M query looks like on its last
        line (`in` / `Source`). So a bare word only counts when it names a BOOLEAN property that is
        still sitting at false in the parsed model - i.e. one that demonstrably did not take effect.

    The first line is never a candidate: it is the expression itself.
    """
    lines = scrub(text).splitlines()
    found: list[Absorption] = []
    for index, raw in enumerate(lines):
        if index == 0:
            continue
        stripped = raw.strip()
        if not stripped:
            continue
        colon = _COLON_PROPERTY_RE.match(stripped)
        if colon and colon.group(1).lower() in names:
            found.append(Absorption(index, colon.group(1), stripped))
            continue
        bare = _BARE_PROPERTY_RE.match(stripped)
        if bare and bare.group(1).lower() in unset_booleans:
            found.append(Absorption(index, bare.group(1), stripped))
    return found


def dotnet_executable() -> str | None:
    """The `dotnet` the oracle should run under, or None when the SDK is absent."""
    override = os.environ.get("TMDL_ORACLE_DOTNET")
    if override:
        return override if Path(override).exists() else None
    return shutil.which("dotnet")


def _sources_newer_than_build() -> bool:
    """Whether the helper needs rebuilding."""
    if not DLL.exists():
        return True
    stamp = DLL.stat().st_mtime
    return any(source.stat().st_mtime > stamp for source in PROJECT_DIR.glob("*.cs")) or (
        (PROJECT_DIR / "tmdl_oracle.csproj").stat().st_mtime > stamp
    )


def ensure_built(dotnet: str) -> Path:
    """Build tools/tmdl_oracle once, returning the assembly path."""
    if not _sources_newer_than_build():
        return DLL
    log.info("Building the TMDL oracle (tools/tmdl_oracle) - first run also restores AMO from nuget.org.")
    try:
        completed = subprocess.run(
            [dotnet, "build", str(PROJECT_DIR), "-c", "Release", "--nologo", "-v", "quiet"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BUILD_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OracleUnavailable(f"could not run `dotnet build`: {exc}") from exc
    if completed.returncode != 0 or not DLL.exists():
        raise OracleUnavailable(
            f"`dotnet build {PROJECT_DIR}` failed (exit {completed.returncode}):\n"
            f"{(completed.stdout + completed.stderr).strip()[:2000]}"
        )
    return DLL


def _run_batch(dotnet: str, dll: Path, definitions: list[Path]) -> dict:
    """Invoke the helper over one batch of definition folders."""
    try:
        completed = subprocess.run(
            [dotnet, str(dll), *[str(path) for path in definitions]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OracleUnavailable(f"could not run the TMDL oracle: {exc}") from exc
    if completed.returncode != 0:
        raise OracleUnavailable(
            f"the TMDL oracle exited {completed.returncode}: {(completed.stdout + completed.stderr).strip()[:2000]}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OracleUnavailable(f"the TMDL oracle produced output that is not JSON: {exc}") from exc


def _resolve_document(definition: Path, document: str | None) -> Path:
    """Turn AMO's `./tables/Date` document id into a path a user can open."""
    if not document:
        return definition
    relative = document.lstrip("./").replace("\\", "/")
    candidate = definition / f"{relative}.tmdl"
    return candidate if candidate.exists() else definition


def _rejection(model: dict) -> TmdlFinding:
    """Report a model the real parser refuses - the model does not open in Desktop either."""
    definition = Path(model["definition"])
    error = model.get("error") or {}
    return TmdlFinding(
        "TMDL_PARSER_REJECTED",
        f"{error.get('type', 'error')}: {error.get('message', 'no message')}\n      This is the "
        f"verdict of TmdlSerializer, the parser Power BI Desktop uses, so Desktop will not open "
        f"this model either.",
        _resolve_document(definition, error.get("document")),
        int(error.get("line") or 1),
    )


def _locate(definition: Path, path_label: str, line_text: str) -> tuple[Path, int]:
    """Find the document and line that carries a swallowed property, for an actionable report."""
    names = re.findall(r"'([^']+)'", path_label)
    documents = sorted(definition.rglob("*.tmdl"))
    preferred = [
        doc
        for doc in documents
        if names and all(name in doc.read_text(encoding="utf-8", errors="replace") for name in names)
    ]
    for document in preferred or documents:
        for number, raw in enumerate(document.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            # a prefix match, because `line_text` came from the SCRUBBED expression: a trailing
            # `// note` on the real line has been blanked away by then
            if raw.strip().startswith(line_text):
                return document, number
    return definition, 1


def _absorption_finding(definition: Path, expression: dict, absorption: Absorption) -> TmdlFinding:
    """Build the finding for one swallowed property."""
    document, line = _locate(definition, expression["path"], absorption.text)
    return TmdlFinding(
        "TMDL_EXPRESSION_ABSORBS_PROPERTY",
        f"'{absorption.name}' was read as part of the {expression['property']} of "
        f"{expression['path']}, not as a property of it - so the property is silently LOST while "
        f"the model still parses cleanly. The parsed expression carries the line "
        f"'{absorption.text}'. A multi-line expression must be indented one level deeper than the "
        f"object's own properties; this one is not, so everything after it is absorbed.",
        document,
        line,
    )


def _model_findings(model: dict, vocabulary: dict) -> list[TmdlFinding]:
    """Every finding for one inspected model."""
    if not model.get("ok"):
        return [_rejection(model)]
    definition = Path(model["definition"])
    findings: list[TmdlFinding] = []
    for expression in model.get("expressions", []):
        names = {entry["name"].lower() for type_name in expression["types"] for entry in vocabulary.get(type_name, [])}
        unset = {name.lower() for name in expression.get("unsetBooleans", [])}
        for absorption in absorbed_properties(expression["text"], names, unset & names):
            findings.append(_absorption_finding(definition, expression, absorption))
    return findings


def check_models(model_dirs: list[Path]) -> tuple[list[TmdlFinding], int]:
    """Run the oracle over `.SemanticModel` folders; returns (findings, models inspected).

    Raises OracleUnavailable when the oracle could not run - the caller must NOT read that as a
    pass. Models without a `definition/` folder are skipped and excluded from the count, so
    "nothing was inspected" stays visible instead of masquerading as a clean result.
    """
    dotnet = dotnet_executable()
    if not dotnet:
        raise OracleUnavailable(
            "the .NET SDK is not on PATH. Install it (https://dotnet.microsoft.com/download) or set "
            "TMDL_ORACLE_DOTNET; `scripts/preflight.ps1` checks for it too."
        )
    definitions = [model / "definition" for model in model_dirs if (model / "definition").is_dir()]
    if not definitions:
        return [], 0
    dll = ensure_built(dotnet)
    findings: list[TmdlFinding] = []
    inspected = 0
    for start in range(0, len(definitions), BATCH_SIZE):
        payload = _run_batch(dotnet, dll, definitions[start : start + BATCH_SIZE])
        vocabulary = payload.get("vocabulary", {})
        for model in payload.get("models", []):
            inspected += 1
            findings.extend(_model_findings(model, vocabulary))
    return findings, inspected
