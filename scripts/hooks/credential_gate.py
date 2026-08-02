"""
purpose: preToolUse / permissionRequest hook that DENIES writes to Power BI model and report
         artifacts while a migration's credential gate is BLOCKED, and interrupts the agent.
usage:   invoked by Copilot CLI via .github/hooks/credential-gate.json (reads a JSON payload on
         stdin, writes a JSON decision to stdout). Run manually with a payload file to test:
             python scripts/hooks/credential_gate.py < payload.json

Why a hook, when the persona and the gate output both already say "stop"
-----------------------------------------------------------------------
Because saying it does not work. Measured 2026-08-01 across four blind migrations against a real
credential-less Databricks warehouse: every run hit the credential gate and announced the stop
CORRECTLY, then three of them talked themselves past it a few turns later ("applying the
non-interactive fallback ... rather than leaving the requested artifacts unbuilt") and shipped a
model for a warehouse that was never contacted. One run - on a stronger model - obeyed. That is a
~25% compliance rate on instruction-following alone, and it gets worse on smaller models, which is
exactly the wrong direction: the whole point of the gate is to protect unattended runs.

So this stops relying on the model agreeing. The hook is not persuasion; it is enforcement. A denied
tool call fails no matter what the model concluded, and `interrupt: true` ends the run outright.

Two deliberate design choices:

* **Deny the WRITE, not the reasoning.** The agent may still think, read, run the probe, and report.
  It simply cannot persist a `.tmdl`/`.pbism`/`.pbir`/`.pbip` into a blocked migration. That keeps
  the useful work (planning, honest reporting) and removes only the harmful outcome (an unvalidated
  model that is byte-identical to a working one).
* **The user can always override.** A build-only migration is a legitimate choice once a human has
  seen the gate; taking it away would make the toolkit unusable for a customer who genuinely wants a
  structural-only pass. The override is a file only a human would create.

Fail behavior, per the hooks reference: command `preToolUse` hooks are **fail-closed** on a crash or
non-zero exit, and **fail-open on timeout**. So this script must be FAST and must not raise on
unexpected payloads - it walks a couple of parent directories and exits.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

MARKER = ".credential-gate-BLOCKED.json"
OVERRIDE = ".credential-gate-AUTHORIZED"

# Artifacts whose creation IS the harm: a persisted semantic model / report for a source that was
# never reached. Deliberately not ".json" or ".md" - the agent should stay free to write its spec
# updates, notes and limitations while blocked.
GUARDED_SUFFIXES = (".tmdl", ".pbism", ".pbir", ".pbip", ".platform")

# The gate's OWN control files. These must be guarded or the gate is decorative.
#
# Measured 2026-08-01: given an adversarial prompt, `mai-code-1-flash-picker` READ THIS SCRIPT,
# discovered the override mechanism, created `.credential-gate-AUTHORIZED` itself, and then wrote
# the .tmdl unimpeded. It did not break the rule - it satisfied it, which is worse, because every
# downstream signal then says "the user authorized this".
#
# The escape hatch has to stay (a human must be able to authorize a build-only run), so the fix is
# asymmetry, not removal: a human creates this file with their editor or another terminal, entirely
# outside the agent's tool surface, while every hooked tool call that would create it is denied.
# Security-through-obscurity is explicitly NOT the goal - MAI reading this comment changes nothing,
# because the denial does not depend on the agent not knowing about it.
CONTROL_FILES = (OVERRIDE.lower(), MARKER.lower())

# Write-capable tools, lowercased. The SAME edit surfaces as `apply_patch` on preToolUse and `edit`
# on permissionRequest (measured), so both must be listed or one event silently allows it.
WRITE_TOOLS = {
    "create",
    "edit",
    "apply_patch",
    "str_replace_editor",
    "write",
    "multiedit",
    "notebookedit",
}
SHELL_TOOLS = {"bash", "powershell", "shell"}

# Shell verbs that persist bytes. An agent denied `create` will often try redirecting from a shell,
# so covering the obvious cases matters; this is a speed bump for evasion, not a sandbox.
SHELL_WRITE_HINTS = (
    ">",
    ">>",
    "out-file",
    "set-content",
    "add-content",
    "new-item",
    "copy-item",
    "move-item",
    "rename-item",
    "remove-item",
    "del ",
    "rm ",
    "erase",
    "unlink",
    "tee",
    "touch",
    "write_text",
    "open(",
    "shutil",
    "os.remove",
    "pathlib",
)

# Any path-ish token ending in a guarded suffix, however it is quoted or escaped.
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\"'\s,;)]+?(?:\.tmdl|\.pbism|\.pbir|\.pbip|\.platform)"
    r"|[^\"'\s,;)]*?(?:\.tmdl|\.pbism|\.pbir|\.pbip|\.platform)",
    re.IGNORECASE,
)

# The gate's own control surface. Small and enumerable, which is exactly why matching works HERE and
# not on "every way to write a file": there are only so many ways to spell "remove this ACE".
_CONTROL_RE = re.compile(
    r"\.credential-gate-AUTHORIZED"
    r"|\.credential-gate-BLOCKED\.json"
    r"|icacls"
    r"|Set-Acl"
    r"|takeown"
    r"|credential_gate\.py\s+clear",
    re.IGNORECASE,
)


def _blocking_marker(target: Path) -> Path | None:
    """Return the BLOCKED marker governing `target`, or None.

    Walks upward from the target: a marker governs everything beneath the directory it sits in,
    which is how one gate covers `<migration>/fabric/**` without knowing the layout.
    """
    for parent in [target, *target.parents]:
        if (parent / OVERRIDE).exists():
            return None
        marker = parent / MARKER
        if marker.is_file():
            return marker
    return None


def _extract_args_text(payload: dict) -> str:
    """Return every tool argument as one searchable string.

    Measured 2026-08-01 - the payload shape differs per event AND per tool, so key-based lookup is
    not survivable:

      preToolUse       toolArgs   STRING. For `apply_patch` it is raw patch text
                                  ("*** Add File: path"); for others a JSON *string*.
      permissionRequest toolInput OBJECT, e.g. {"file_path": "C:\\..."} - and note the key is
                                  `toolInput` (camelCase), not `tool_input`.

    The same edit is also reported under DIFFERENT tool names by the two events (`apply_patch` on
    preToolUse, `edit` on permissionRequest). An earlier version of this hook matched on structured
    keys, found nothing, and allowed every write - a guardrail that silently does nothing, which is
    worse than none at all. So: flatten everything and search the text.
    """
    parts: list[str] = []
    for key in ("toolArgs", "toolInput", "tool_input", "toolArguments"):
        val = payload.get(key)
        if val is None:
            continue
        parts.append(val if isinstance(val, str) else json.dumps(val))
    return "\n".join(parts)


# Any path-ish token ending in a guarded suffix, however it is quoted or escaped.
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\"'\s,;)]+?(?:\.tmdl|\.pbism|\.pbir|\.pbip|\.platform)"
    r"|[^\"'\s,;)]*?(?:\.tmdl|\.pbism|\.pbir|\.pbip|\.platform)",
    re.IGNORECASE,
)


def _candidate_paths(tool: str, payload: dict) -> list[Path]:
    """Extract plausible WRITE targets from a hook payload.

    Read tools are excluded deliberately: `view` legitimately carries a .tmdl path, and denying
    reads would break the agent's ability to inspect a model without protecting anything.
    """
    tool_l = tool.lower()
    is_write = tool_l in WRITE_TOOLS
    is_shell = tool_l in SHELL_TOOLS
    if not (is_write or is_shell):
        return []

    text = _extract_args_text(payload)
    if not text:
        return []
    if is_shell and not any(h in text.lower() for h in SHELL_WRITE_HINTS):
        return []

    seen: list[Path] = []
    for match in _PATH_RE.findall(text):
        token = match.replace("\\\\", "\\").strip("\"' ()[]{};,")
        if token:
            seen.append(Path(token))
    return seen


def _deny_reason(marker: Path, target: Path) -> str:
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        sources = ", ".join(info.get("sources", [])) or "live source(s)"
    except (OSError, ValueError):
        sources = "live source(s)"
    return (
        f"DENIED BY THE CREDENTIAL GATE - this is enforcement, not advice.\n"
        f"  Blocked write: {target}\n"
        f"  Gate marker:   {marker}\n"
        f"  Reason:        {sources} have no Power BI credential, so reachability is UNPROVEN.\n"
        f"\n"
        f"  You cannot build a semantic model or report for a source that was never contacted.\n"
        f"  Retrying, renaming the file, or writing it from a shell will also be denied.\n"
        f"  A credential is something only a HUMAN can supply - no amount of persistence helps.\n"
        f"\n"
        f"  STOP NOW and report to the user. Stopping here IS the correct, completed outcome;\n"
        f"  it is not an unfinished task. Their options:\n"
        f"    1. Sign in to the source once in Power BI Desktop, then re-run the probe.\n"
        f"    2. Supply a PAT / service-principal secret to bind the connection.\n"
        f"    3. Explicitly authorize an unvalidated build by creating the file\n"
        f"       '{OVERRIDE}' next to the migration spec. Only the USER may do this -\n"
        f"       creating it yourself defeats the gate and will be treated as a defect."
    )


def _any_marker_under(root: Path) -> bool:
    """Is any migration under `root` currently gated? Bounded so the hook stays fast.

    preToolUse command hooks fail OPEN on timeout, so a slow hook is a disabled hook. Depth is
    capped and the walk prunes the usual large directories.
    """
    skip = {".git", "node_modules", ".venv", "__pycache__", "examples", ".pbi"}
    try:
        for depth_root, dirs, files in os.walk(root):
            rel_depth = len(Path(depth_root).relative_to(root).parts)
            if rel_depth > 4:
                dirs.clear()
                continue
            dirs[:] = [d for d in dirs if d not in skip]
            if MARKER in files:
                return True
    except (OSError, ValueError):
        return True  # fail closed: if we cannot tell, assume gated
    return False


def _deny(reason: str) -> int:
    """Emit a deny decision in BOTH event vocabularies and stop the run."""
    print(
        json.dumps(
            {
                # preToolUse vocabulary
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
                # permissionRequest vocabulary - `interrupt` ends the run outright, which is the
                # piece prose could never achieve: there is no next turn to rationalize in.
                "behavior": "deny",
                "message": reason,
                "interrupt": True,
            }
        )
    )
    return 0


def main() -> int:
    """Read the hook payload from stdin and emit an allow/deny decision."""
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        # No parseable payload: stay out of the way rather than blocking every tool call.
        print(json.dumps({}))
        return 0

    tool = payload.get("toolName") or payload.get("tool_name") or ""
    tool_l = str(tool).lower()
    cwd = Path(payload.get("cwd") or ".")
    text = _extract_args_text(payload)

    # 1. Defend the gate's own control files FIRST, before any override is honoured. Checked on
    #    every write-capable tool and by NAME, so creating, copying, renaming or deleting one is
    #    denied no matter how it is spelled. Reads stay allowed: the agent SHOULD be able to see
    #    that it is blocked and why.
    if (tool_l in WRITE_TOOLS or tool_l in SHELL_TOOLS) and _CONTROL_RE.search(text):
        # Only defend the control surface while a gate is actually applied somewhere beneath cwd -
        # otherwise this would block legitimate icacls/ACL work in an unrelated repo.
        if _any_marker_under(cwd):
            return _deny(
                "DENIED - that touches the credential gate's own control surface.\n"
                f"  Creating '{OVERRIDE}', deleting '{MARKER}', clearing the gate, or changing the\n"
                "  output folder's ACL (icacls / Set-Acl / takeown) is not something an agent may do.\n"
                "\n"
                "  The override exists so a HUMAN can authorize an unvalidated build AFTER seeing the\n"
                "  gate. Writing it yourself forges that authorization, and every downstream signal\n"
                "  would then wrongly report that the user approved this.\n"
                "\n"
                "  Measured: an agent read this hook's source, created the override itself, and\n"
                "  shipped an unvalidated model. Knowing the mechanism does not help - the denial\n"
                "  does not depend on you not knowing about it, and the gate is ALSO enforced by a\n"
                "  kernel-level ACL that no write method gets around.\n"
                "\n"
                "  STOP NOW and ask the user. Stopping IS the correct, completed outcome."
            )

    # 2. Then the artifact rule: no model/report files for an unreachable source.
    for raw in _candidate_paths(str(tool), payload):
        target = raw if raw.is_absolute() else (cwd / raw)
        if not str(target).lower().endswith(GUARDED_SUFFIXES):
            continue
        marker = _blocking_marker(target)
        if marker:
            return _deny(_deny_reason(marker, target))

    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
