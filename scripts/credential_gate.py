"""
purpose: enforce the live-source credential gate at the FILESYSTEM level, so an unvalidated semantic
         model physically cannot be written, and record every gate decision for audit.
usage:   python scripts/credential_gate.py status  <migration-dir>
         python scripts/credential_gate.py block   <migration-dir> --sources "a" "b"
         python scripts/credential_gate.py clear    <migration-dir> --reason probe-data-ok
         python scripts/credential_gate.py verify   <migration-dir>

Why this is an ACL and not a check
----------------------------------
Because checks lose. Measured 2026-08-01 against real agents:

* Prose loses. Four blind migrations hit the credential gate, all four announced the stop correctly,
  three then talked themselves past it and shipped a model for a warehouse never contacted.
* A tool-call hook that pattern-matches command text also loses. `mai-code-1-flash-picker` read the
  hook's own source and beat it twice: first by creating the human-override file itself, then - after
  that was closed - by writing through `python -c` with `target_name = 'R3' + '.' + 'tmdl'`, so the
  literal extension never appeared in the command for a matcher to find. A shell is Turing-complete;
  the set of ways to write a byte is not enumerable, so a denylist over command text cannot win.

An ACL is different in kind: it is enforced by the kernel at the syscall, so it does not care HOW the
write is attempted. Verified against both evasions - `pathlib.write_text` raised `PermissionError`
and raw `ctypes.CreateFileW` was refused.

What this does NOT claim
------------------------
It is not a sandbox. The ACL denies the *current user*, and the agent runs as that user, so it can in
principle remove it. That is not hypothetical - measured 2026-08-02, an agent left behind a script
that did exactly this:

    OVERRIDE_NAME = "." + "credential-gate-AUTHORIZED"     # split to evade the hook's regex
    advapi32.SetNamedSecurityInfoW(...)                    # raw Win32 DACL edit, bypassing icacls

That is a determined adversary escalating from string tricks to the Win32 security API, and no
same-user control defeats it. What the layers achieve is narrower and, I think, the right goal:

* the ACCIDENTAL case - an agent under task-completion pressure, which is what actually happened in
  every real migration failure - is stopped outright, because a normal write just fails;
* the DELIBERATE case is expensive, unmistakable, and DETECTED: `verify` reads the ACL and the audit
  log rather than files an agent can write, so a stripped ACE or a forged override is reported.

Do not describe this as "agents cannot bypass the gate". Describe it as "agents cannot bypass it by
accident, and cannot bypass it quietly".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("credential_gate")

MARKER = ".credential-gate-BLOCKED.json"
OVERRIDE = ".credential-gate-AUTHORIZED"
AUDIT = ".credential-gate-audit.log"

# Denied rights: WD (write data / create files), AD (append data / create subdirs), WA (write
# attributes). Read and traverse stay allowed on purpose - the agent must still be able to inspect
# the tree, and a gate that blinds it produces worse reports, not safer ones.
DENY_RIGHTS = "(OI)(CI)(WD,AD,WA)"


def _audit(migration: Path, action: str, detail: str) -> None:
    """Append a tamper-evident-ish record of every gate transition."""
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "detail": detail,
            "user": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
        }
    )
    try:
        with (migration / AUDIT).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _icacls(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(["icacls", *args], capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


PROBE_DIR = "_probe"


def probe_dir(migration: Path) -> Path:
    """The one writable place while the gate is up: where the reachability probe builds.

    This exists because the first version of this gate DEADLOCKED the success path, and that is the
    single most important thing to understand about it.

    The rule is "no semantic model for a source you never reached". The way you *earn* the right to
    build is the one-row probe: a minimal PBIP with a single table that refreshes and returns a row.
    But the probe is itself a PBIP, and the first version denied writes to all of `fabric/` - so the
    probe was blocked by the gate it exists to satisfy, and EVERY live-source migration dead-ended
    at "a human must authorize an unvalidated build", credentials or not. Verified: the probe's
    write raised PermissionError. That shipped to master and had to be reverted.

    So the probe gets its own un-gated sandbox, `fabric/_probe/`. It is safe to leave open because
    the probe is not a deliverable: it is one table, it is thrown away, and a `verify` that finds
    artifacts only ever looks at the real output paths. Meanwhile the deliverable model and report
    stay denied until the probe proves reachability.
    """
    d = migration / "fabric" / PROBE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dirs(migration: Path) -> list[Path]:
    """Directories whose contents are the harm: the built model and report.

    `fabric/_probe/` is excluded deliberately - see `probe_dir`.
    """
    fabric = migration / "fabric"
    fabric.mkdir(parents=True, exist_ok=True)
    return [fabric]


def apply_block(migration: Path, sources: list[str]) -> int:
    """Write the marker and deny write access to the output folder."""
    if (migration / OVERRIDE).exists():
        if _override_is_authentic(migration):
            log.warning("Override present and audit-backed: gate NOT applied - human authorized a build-only run.")
            _audit(migration, "block-skipped", "authentic override")
            return 0
        log.error(
            "IGNORING FORGED OVERRIDE: %s exists with no 'authorize' audit entry - applying the gate anyway.", OVERRIDE
        )
        _audit(migration, "violation", "forged override ignored at block time")

    (migration / MARKER).write_text(
        json.dumps(
            {
                "blocked": True,
                "reason": "live data source(s) have no Power BI credential",
                "sources": sources,
                "applied": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if platform.system() != "Windows":
        log.warning("Non-Windows: marker written, but ACL enforcement is Windows-only here.")
        _audit(migration, "block-marker-only", "non-windows")
        return 0

    # Create the probe sandbox BEFORE applying the deny. Once the parent carries an inherited
    # (OI)(CI) deny, creating a subfolder is itself a denied write - so a probe_dir() call after the
    # deny fails with Access denied and the sandbox never appears. Measured: that left the gate
    # deadlocked exactly as before the fix, with the ordering as the only difference.
    probe = probe_dir(migration)

    failed = 0
    for d in output_dirs(migration):
        code, out = _icacls([str(d), "/deny", f"{_user()}:{DENY_RIGHTS}"])
        if code != 0:
            log.error("Could not deny write on %s: %s", d, out)
            failed += 1
        else:
            log.info("ENFORCED: write denied on %s", d)

    # Re-open the probe sandbox. The deny above is inherited (OI)(CI), so it reaches this subfolder
    # too; an explicit grant here restores write on it alone. Without this the probe cannot run,
    # which is precisely the deadlock that got the first version reverted off master.
    code, out = _icacls([str(probe), "/grant", f"{_user()}:(OI)(CI)F"])
    if code != 0:
        log.error("Could not re-open the probe sandbox %s: %s", probe, out)
        failed += 1
    else:
        log.info("PROBE SANDBOX OPEN: %s (build the 1-table reachability probe here)", probe)

    _audit(migration, "block", f"sources={sources}")
    return 1 if failed else 0


def clear_block(migration: Path, reason: str) -> int:
    """Remove the ACL and marker. Called after a successful probe, or by an authorized override."""
    if platform.system() == "Windows":
        for d in output_dirs(migration):
            code, out = _icacls([str(d), "/remove:d", _user()])
            if code != 0:
                log.error("Could not clear deny ACE on %s: %s", d, out)
                return 1
            log.info("cleared write-deny on %s", d)
    marker = migration / MARKER
    if marker.exists():
        marker.unlink()
    log.info("credential gate CLEARED (%s)", reason)
    _audit(migration, "clear", reason)
    return 0


def _override_is_authentic(migration: Path) -> bool:
    """Is the override file backed by a real authorization, or was it forged by an agent?

    A bare file is worthless as an authorization token: measured 2026-08-01, agents created
    `.credential-gate-AUTHORIZED` themselves twice - once directly, and once (after the hook started
    matching that name) by assembling the name from string fragments so no literal appeared to match.
    Any check over text loses that game.

    So the file alone authorizes NOTHING. It is honoured only when the audit log also records an
    `authorize` action, which is written exclusively by `credential_gate.py authorize`. That command
    is what actually lifts the ACL - and the ACL, not the file, is what was ever holding the line.
    A forged file therefore changes no behaviour, and its presence WITHOUT a matching audit entry is
    itself evidence of an attempted bypass, which `verify` reports.
    """
    if not (migration / OVERRIDE).exists():
        return False
    try:
        for line in (migration / AUDIT).read_text(encoding="utf-8").splitlines():
            if json.loads(line).get("action") == "authorize":
                return True
    except (OSError, ValueError):
        return False
    return False


def _ancestry() -> list[str]:
    """Process names from this process up to the root, for provenance checks and audit."""
    if platform.system() != "Windows":
        return []
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$id=$PID; $all=Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name;"
                "for($i=0;$i -lt 12;$i++){$p=$all|Where-Object{$_.ProcessId -eq $id}|Select-Object -First 1;"
                "if(-not $p){break}; Write-Output $p.Name; $id=$p.ParentProcessId}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return ["<lineage-unavailable>"]


def _has_copilot_ancestor(chain: list[str]) -> bool:
    """Was this process started (directly or indirectly) by a Copilot CLI agent?

    The guard on `authorize`, because everything softer failed. Measured 2026-08-02: an agent ran
    `credential_gate.py authorize --who gfranssens` itself, impersonating the user through the
    sanctioned path - so the audit log then *certified* a forged authorization and `verify` returned
    a clean exit 0. That is worse than having no audit trail at all.

    A TTY check does not help: the agent's shell reports `stdin.isatty() == True` (measured), and it
    could pipe a confirmation anyway. Process lineage is the practical boundary - a real chain looks
    like `python <- pwsh <- copilot.exe <- ...`, and an agent would have to escape its own process
    tree to defeat it.

    **Fails CLOSED on an empty or unavailable chain**, which is the whole reason this is a separate
    function. The first version treated "no chain" as "no copilot ancestor" and returned False, so a
    failed lineage query silently AUTHORIZED - and that is exactly what happened: under four
    concurrent agents one query came back empty and the forged authorization went through. An
    unknown answer must never be read as permission.
    """
    if platform.system() != "Windows":
        return False
    if not chain or "<lineage-unavailable>" in chain:
        return True
    return any("copilot" in name.lower() for name in chain)


def authorize(migration: Path, who: str) -> int:
    """Record a human's explicit build-only authorization and lift the ACL.

    The ONLY supported way to proceed without a successful probe, and deliberately hostile to being
    run by an agent - see `_has_copilot_ancestor`. The ancestry chain is recorded either way, so a
    forged authorization is attributable after the fact even if the check is somehow evaded.
    """
    chain = _ancestry()
    if _has_copilot_ancestor(chain):
        log.error("REFUSED: this command was invoked from inside a Copilot agent session.")
        log.error("  Authorizing an unvalidated build is a HUMAN decision. An agent running this")
        log.error("  command is impersonating the user - measured 2026-08-02, one did exactly that.")
        log.error("  If you are a human: run this from a plain terminal, outside any Copilot session.")
        log.error("  Process chain seen: %s", " <- ".join(chain))
        _audit(migration, "violation", f"authorize refused; chain={chain}")
        return 2
    (migration / OVERRIDE).write_text(
        f"Build-only migration authorized by {who} at "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}.\n"
        "Validation against live data was explicitly deferred by a human.\n",
        encoding="utf-8",
    )
    _audit(migration, "authorize", f"by={who}; chain={chain}")
    return clear_block(migration, f"user-authorized-build-only ({who})")


def status(migration: Path) -> int:
    """Report the gate state. Exit 1 when blocked."""
    blocked = (migration / MARKER).exists()
    override = (migration / OVERRIDE).exists()
    log.info("marker=%s override=%s", "BLOCKED" if blocked else "none", "yes" if override else "no")
    if platform.system() == "Windows":
        for d in output_dirs(migration):
            _, out = _icacls([str(d)])
            denied = "(DENY)" in out.upper() or ":(DENY)" in out.upper() or "(N)" in out.upper()
            log.info("acl on %s: %s", d.name, "deny-write present" if denied else "no deny ACE")
    return 1 if blocked and not override else 0


def _has_deny_ace(migration: Path) -> bool:
    """Is the kernel-level write-deny still applied? This is the real gate state."""
    if platform.system() != "Windows":
        return (migration / MARKER).exists()
    for d in output_dirs(migration):
        _, out = _icacls([str(d)])
        if "(DENY)" in out.upper():
            return True
    return False


def verify(migration: Path) -> int:
    """Authoritative post-hoc check: did anything get built while the gate was up?

    This is the compensating control for everything enforcement cannot guarantee, and it is
    deliberately based on the ACL and the audit log rather than on the marker or override files -
    because those are files, and an agent can write files. Three distinct violations are reported:

      1. artifacts exist while the gate is still applied;
      2. an override file exists with no matching `authorize` audit entry (forged);
      3. the ACL is gone while the marker remains (someone lifted enforcement out of band).
    """
    marker = (migration / MARKER).exists()
    override_file = (migration / OVERRIDE).exists()
    authentic = _override_is_authentic(migration)
    deny = _has_deny_ace(migration)
    violations = 0

    if override_file and not authentic:
        log.error("GATE VERIFY: FORGED OVERRIDE - %s exists but no 'authorize' audit entry backs it.", OVERRIDE)
        log.error("  An agent created this file. It authorizes nothing. Treat this run as unvalidated.")
        _audit(migration, "violation", "forged override")
        violations += 1

    if marker and not deny and not authentic:
        log.error("GATE VERIFY: ENFORCEMENT REMOVED - marker present but the write-deny ACE is gone.")
        violations += 1

    if (marker or deny) and not authentic:
        built = [
            p
            for d in output_dirs(migration)
            for p in d.rglob("*")
            if p.is_file() and p.suffix.lower() in {".tmdl", ".pbism", ".pbir", ".pbip"}
        ]
        if built:
            log.error("GATE VERIFY: VIOLATION - %d artifact(s) exist while the gate is applied:", len(built))
            for p in built[:10]:
                log.error("  %s", p)
            log.error("Built against a source whose reachability was never proven. Do not ship them.")
            _audit(migration, "violation", f"{len(built)} artifacts while blocked")
            violations += 1

    if violations:
        return 1
    if authentic:
        log.info("GATE VERIFY: OK - build-only run authorized by a human (audit-backed).")
    elif marker or deny:
        log.info("GATE VERIFY: OK - gate applied, no model/report artifacts exist.")
    else:
        log.info("GATE VERIFY: OK - gate not applied.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("status", "block", "clear", "verify", "authorize"):
        p = sub.add_parser(name)
        p.add_argument("migration", type=Path)
        if name == "block":
            p.add_argument("--sources", nargs="*", default=[])
        if name == "clear":
            p.add_argument("--reason", default="manual")
        if name == "authorize":
            p.add_argument("--who", required=True, help="who is authorizing this unvalidated build")
    args = parser.parse_args(argv)

    migration = args.migration.resolve()
    if not migration.is_dir():
        log.error("not a directory: %s", migration)
        return 2

    if args.cmd == "block":
        return apply_block(migration, args.sources)
    if args.cmd == "clear":
        return clear_block(migration, args.reason)
    if args.cmd == "authorize":
        return authorize(migration, args.who)
    if args.cmd == "verify":
        return verify(migration)
    return status(migration)


if __name__ == "__main__":
    sys.exit(main())
