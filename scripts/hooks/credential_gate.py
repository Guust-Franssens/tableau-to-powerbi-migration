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

# The gate's own control surface. Small and enumerable, which is exactly why matching works HERE and
# not on "every way to write a file": there are only so many ways to spell "remove this ACE".
#
# ⚠️ These match MUTATIONS, never MENTIONS. The first version matched the bare word `icacls`, plus
# the control files' names anywhere in the payload. Measured 2026-08-03, that denied a great deal of
# legitimate work: a read-only `icacls <path>` with no flags, any shell command that merely READ the
# audit log, tearing down finished test fixtures - and, worst, writing a TEST FILE whose source text
# quotes a control file's name. It ended in a genuine deadlock: with a leftover marker anywhere under
# the repo, the guard was armed repo-wide and blocked the edit that fixes the guard. A human had to
# clear the fixtures by hand. Same shape as the v1 sandbox deadlock: a guard that forbids the action
# needed to satisfy it.
_ACL_MUTATION_RE = re.compile(
    r"icacls\b[^\n;|]*?/(?:grant|deny|remove|setowner|inheritance|reset|restore|substitute)"
    r"|Set-Acl"
    r"|takeown",
    re.IGNORECASE,
)

# A control FILE is defended only against commands that would create, destroy or overwrite it.
# Reading one is not merely harmless, it is encouraged - the agent SHOULD see that it is gated.
_MUTATE_VERB = r"(?:Remove-Item|Rename-Item|Move-Item|Copy-Item|Set-Content|Add-Content|Out-File|New-Item|del|erase|rm)"
_CONTROL_FILE = rf"(?:{re.escape(OVERRIDE)}|{re.escape(MARKER)})"
_CONTROL_FILE_MUTATION_RE = re.compile(
    rf"{_MUTATE_VERB}[^\n;|]*{_CONTROL_FILE}|{_CONTROL_FILE}[^\n;|]*?(?:>|\|\s*{_MUTATE_VERB})",
    re.IGNORECASE,
)

# `credential_gate.py clear` has NO internal defense against being run unearned: `clear_block()`
# lifts the ACL unconditionally and only labels the audit entry differently (`manual-clear` vs
# `probe-cleared`) for `verify` to flag AFTERWARDS - detection, not prevention. `authorize` DOES
# self-defend via `_has_copilot_ancestor`, but `clear` does not, so THIS hook is the only thing
# stopping a subagent from lifting its own armed gate by invoking the documented teardown verb
# directly. An earlier edit to this file deleted the match for both verbs, reasoning that
# credential_gate.py "checks the caller lineage and audit evidence" for both - true for `authorize`,
# false for `clear`. Restored here, but made TARGET-AWARE (see `_targets_an_armed_gate`) so it does
# not reintroduce the tree-wide false positive that motivated the bad fix in the first place.
_CLEAR_OR_AUTHORIZE_RE = re.compile(
    r"credential_gate\.py\s+(?:clear|authorize)\s+(\"[^\"]+\"|'[^']+'|[^\s;|&]+)",
    re.IGNORECASE,
)
_ICACLS_TARGET_RE = re.compile(r"icacls\s+(\"[^\"]+\"|'[^']+'|[^\s;|&]+)", re.IGNORECASE)
_MARKER_PATH_RE = re.compile(rf"([^\"'\s;|&]*?[\\/])?({re.escape(OVERRIDE)}|{re.escape(MARKER)})", re.IGNORECASE)


def _command_targets(text: str, cwd: Path) -> list[Path]:
    """Best-effort extraction of the directory a control-surface command is aimed at."""
    targets: list[Path] = []
    for rx in (_CLEAR_OR_AUTHORIZE_RE, _ICACLS_TARGET_RE):
        match = rx.search(text)
        if match:
            raw = match.group(1).strip("\"'")
            candidate = Path(raw)
            targets.append(candidate if candidate.is_absolute() else (cwd / candidate))
    for match in _MARKER_PATH_RE.finditer(text):
        dirpart = (match.group(1) or "").rstrip("\\/")
        candidate = Path(dirpart) if dirpart else cwd
        targets.append(candidate if candidate.is_absolute() else (cwd / candidate))
    return targets


def _mutates_control_surface(text: str) -> bool:
    """Does this command CHANGE the gate's control surface, rather than merely name it?"""
    return bool(
        _ACL_MUTATION_RE.search(text) or _CONTROL_FILE_MUTATION_RE.search(text) or _CLEAR_OR_AUTHORIZE_RE.search(text)
    )


def _targets_an_armed_gate(text: str, cwd: Path) -> bool:
    """Is the SPECIFIC migration this command names currently gated?

    Target-aware on purpose. Its predecessor, `_any_marker_under(cwd)`, armed the guard for the
    WHOLE cwd tree whenever ANY leftover marker existed anywhere beneath it. Measured 2026-08-03,
    that blocked legitimate teardown of one finished fixture merely because a DIFFERENT, unrelated
    fixture nearby was still gated - and once blocked, the edit that would have fixed the hook was
    denied too, by the very same over-broad check. Falls back to the tree-wide check only when no
    specific target can be parsed from the command text, which keeps the conservative default for
    shapes this cannot recognise.
    """
    targets = _command_targets(text, cwd)
    if not targets:
        return _any_marker_under(cwd)
    return any(_blocking_marker(t) is not None for t in targets)


def _writes_a_control_file(payload: dict, tool: str, cwd: Path) -> bool:
    """Is a file-writing tool aimed AT a gate control file? Judged on the PATH ARGUMENT alone.

    This is the distinction that produced the deadlock: creating the override IS forgery, while
    creating `tests/test_credential_gate.py` - whose text necessarily quotes that name - is the
    suite that defends it. So the path argument is extracted structurally and the file BODY is never
    consulted; matching anywhere in the payload text would deny writing any file that merely
    mentions a control file, including this hook's own tests and its own repair.

    Deliberately NOT built on `_candidate_paths`: that filters to model/report suffixes (.tmdl,
    .pbip, ...) for the artifact rule, so a control file - which has no such suffix - can never come
    back from it. Reusing it silently allowed the override to be forged (caught by test, 2026-08-03).
    """
    if tool.lower() not in WRITE_TOOLS:
        return False
    for value in _path_arguments(payload):
        target = Path(value)
        target = target if target.is_absolute() else (cwd / target)
        if target.name in (OVERRIDE, MARKER):
            return True
    return False


def _path_arguments(payload: dict) -> list[str]:
    """Pull just the path-shaped ARGUMENTS out of a hook payload, never free text or file bodies."""
    keys = ("path", "file_path", "filePath", "notebook_path", "target", "destination")
    found: list[str] = []
    candidates: list[dict] = []

    args = payload.get("toolInput") or payload.get("tool_input")
    if isinstance(args, dict):
        candidates.append(args)

    raw = payload.get("toolArgs") or payload.get("tool_args")
    if isinstance(raw, dict):
        candidates.append(raw)
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            candidates.append(parsed)

    for block in candidates:
        for key in keys:
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
    return found


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
    """Explain the denial without asserting anything the gate has not measured.

    This text used to say the sources "have no Power BI credential", and told the agent a credential
    was the blocker. The gate cannot know that - it arms at parse time, having opened no connection.
    Measured 2026-08-03: a model read the equivalent claim in the marker file, reported "no
    credential" to the user, and never probed. The denial must say what is true (writes are blocked
    because reachability is unproven) and point at the measurement.
    """
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        sources = ", ".join(info.get("sources", [])) or "live source(s)"
    except (OSError, ValueError):
        sources = "live source(s)"
    return (
        f"DENIED BY THE CREDENTIAL GATE - this is enforcement, not advice.\n"
        f"  Blocked write: {target}\n"
        f"  Gate marker:   {marker}\n"
        f"  Reason:        reachability of {sources} is UNPROVEN - nothing has contacted it yet.\n"
        f"\n"
        f"  You cannot build a semantic model or report for a source that was never contacted.\n"
        f"  Retrying, renaming the file, or writing it from a shell will also be denied.\n"
        f"\n"
        f"  This is NOT a statement that a credential is missing - the gate has not measured\n"
        f"  anything. Your next action is the measurement, and its result decides:\n"
        f"      python scripts/probe_live_source.py --spec <this-migration>/migration-spec.json\n"
        f"    DATA_OK        -> the gate lifts itself; build for real.\n"
        f"    NO_CREDENTIAL  -> stop and ask a human; no retry can conjure a credential.\n"
        f"    UNREACHABLE    -> stop; report the address/network fault. Nobody needs to sign in.\n"
        f"\n"
        f"  Do NOT report a credential problem before the probe has returned one. Sending the\n"
        f"  user to fix a sign-in they do not need is its own wrong answer.\n"
        f"\n"
        f"  If the probe returns NO_CREDENTIAL, stopping IS the correct, completed outcome;\n"
        f"  it is not an unfinished task. The user's options are then:\n"
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

    # 1. Defend the gate's own control files FIRST, before any override is honoured. Write tools are
    #    judged on their TARGET PATH alone (unconditional - there is no legitimate reason to create a
    #    file literally named after the marker/override, gated or not). Shell commands are judged on
    #    whether they MUTATE, and denied only when the SPECIFIC migration they name is currently
    #    armed - see `_targets_an_armed_gate` for why target-awareness matters here.
    control_hit = (tool_l in WRITE_TOOLS and _writes_a_control_file(payload, str(tool), cwd)) or (
        tool_l in SHELL_TOOLS and _mutates_control_surface(text) and _targets_an_armed_gate(text, cwd)
    )
    if control_hit:
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
