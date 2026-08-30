"""
purpose: TMDL oracle driver - ask the real parser (AMO TmdlSerializer) whether a semantic model
         loads at all, which is the failure issue #254 actually reported.
usage:   imported by scripts/check_datamodel.py; run that, not this.
internal: true
internal-reason: a library with no CLI of its own. `python scripts/check_datamodel.py` is the
                 agent-facing entry point and already carries this gate.

Why an oracle instead of a scanner
----------------------------------
Issue #254 is a model that Power BI Desktop REFUSES TO OPEN: `TMDL Format Error: Unexpected line
type: Other!`, no file named, no line named. Deciding which layouts do that means knowing the TMDL
grammar exactly, and two hand-written attempts at it (a property-name allowlist, then the documented
indentation contract) each shipped false negatives AND false positives - rejecting valid TMDL such as
a case-different `IsHidden` or a second `tablePermission` in one role.

Re-implementing someone else's grammar is a completeness claim, and a completeness claim over a
parser you do not own cannot be finished by patching. So this module makes no such claim. It asks
`TmdlSerializer.DeserializeDatabaseFromFolder` - the parser Desktop itself uses - and reports what it
says. Whatever AMO accepts is accepted here, by construction: false positives are structurally
impossible.

What this deliberately does NOT do
----------------------------------
It does not detect SILENT ABSORPTION - a property written at the wrong indent, swallowed into the
preceding DAX/M while the document stays well-formed. That is not a gap in this implementation, it
is measurably outside what any readback can decide: an absorbed `isHidden` and an `isHidden` that is
ordinary expression content one level deeper produce a BYTE-IDENTICAL parse (same Expression string,
same IsHidden=False). The only carrier of the distinction is source indentation, which the parser
normalises away. Issue #404 tracks it, with the measurement and the three mechanisms already falsified.

Requires the .NET SDK. `scripts/preflight.ps1` checks for it. If it is missing the gate reports
UNASSESSABLE (a distinct exit code) rather than a pass - see check_datamodel.EXIT_UNASSESSABLE.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from tmdl_checks import TmdlFinding

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = REPO_ROOT / "tools" / "tmdl_oracle"
PROJECT_FILE = PROJECT_DIR / "tmdl_oracle.csproj"
DLL = PROJECT_DIR / "bin" / "Release" / "net9.0" / "tmdl_oracle.dll"

# A first build restores the AMO package from nuget.org; later runs reuse it and take ~2s.
BUILD_TIMEOUT_SECONDS = 600
RUN_TIMEOUT_SECONDS = 600
# Keep a command line comfortably inside the Windows 32 767-character limit.
BATCH_SIZE = 32

log = logging.getLogger("tmdl_oracle")

_AMO_VERSION_RE = re.compile(r'Include="Microsoft\.AnalysisServices\.NetCore[^"]*"\s+Version="([^"]+)"')


class OracleUnavailable(RuntimeError):
    """The oracle could not be run at all - never confuse this with a clean model."""


def pinned_amo_version() -> str:
    """The AMO version tools/tmdl_oracle is pinned to, read from the csproj itself.

    Single source of truth on purpose: a verdict is only as trustworthy as the parser that produced
    it, so the version is not duplicated into a constant that can drift away from the build.
    """
    match = _AMO_VERSION_RE.search(PROJECT_FILE.read_text(encoding="utf-8"))
    if not match:
        raise OracleUnavailable(f"could not read the pinned AMO version from {PROJECT_FILE}")
    return match.group(1)


def _check_amo_version(reported: str | None) -> None:
    """Refuse a payload that did not come from the pinned parser.

    Without this, anything that prints plausible JSON on stdout - a stub on TMDL_ORACLE_DOTNET, a
    stale build, a machine-wide AMO upgrade - is accepted as an authoritative verdict.
    """
    expected = pinned_amo_version()
    parts = (reported or "").split(".")
    if ".".join(parts[:3]) != expected:
        raise OracleUnavailable(
            f"the TMDL oracle reported AMO version {reported!r}, but tools/tmdl_oracle is pinned to "
            f"{expected}. A verdict from an unknown parser is not evidence; rebuild the helper "
            f"(`dotnet build tools/tmdl_oracle -c Release`) or reconcile the pin."
        )


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
        PROJECT_FILE.stat().st_mtime > stamp
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
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OracleUnavailable(f"the TMDL oracle produced output that is not JSON: {exc}") from exc
    _check_amo_version(payload.get("amoVersion"))
    return payload


def _resolve_document(definition: Path, document: str | None) -> Path:
    """Turn AMO's `./tables/Date` document id into a path a user can open."""
    if not document:
        return definition
    relative = document.lstrip("./").replace("\\", "/")
    candidate = definition / f"{relative}.tmdl"
    return candidate if candidate.exists() else definition


def rejection(model: dict) -> TmdlFinding:
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


def check_models(model_dirs: list[Path]) -> tuple[list[TmdlFinding], int]:
    """Hand every `.SemanticModel` to the real parser; returns (findings, models inspected).

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
        for model in payload.get("models", []):
            inspected += 1
            if not model.get("ok"):
                findings.append(rejection(model))
    return findings, inspected
