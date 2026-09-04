"""
purpose: run a generated-artifact fix script, record before/after hashes for tamper detection, and
         write a NAVIGATIONAL replay-manifest row so the script stays findable (issue #259).
usage:   python scripts/declare_generated_edit.py --bundle <dir> --target <generated-path>
                                           --script <_build/fix_*.py>
                                           [--purpose <one-line reason>] [-- <script-args>...]

This is an audit wrapper, not an authorization system. It records what a replayable fix script did so
``check_migration_progress.py --tamper`` can distinguish declared repairs from silent drift. Anyone
who can edit the bundle can also forge the JSON record; review still has to decide whether the script
is a legitimate, idempotent repair.

The replay-manifest row (issue #259)
-------------------------------------
161 replay scripts existed across a real estate with no way to enumerate them: the convention
mandated writing a replay script for every generated edit, but never mandated making it findable.
A drift declaration is written only when the target's hash actually changed - a correct, idempotent
fix script re-run a second time prints ``DECLARE: NO_CHANGE`` and records no declaration, so an
idempotent re-run used to make the script invisible again. The row written here (via
``generated_edit_declarations.write_replay_registration``) is written UNCONDITIONALLY - even on the
`DECLARE: NO_CHANGE` path - and is keyed by TARGET, so re-declaring the same edit updates its own
row instead of accumulating a second, contradictory one.

This manifest is deliberately an INDEX FOR DISCOVERABILITY, not a verification gate: it does not
prove the named script still exists on disk, that it matches any digest, that it belongs to a
package, or that it covers every generated edit in the bundle. Nothing consumes it as a sign-off
condition. A CLI check_replay_manifest.py has been descoped from this change per review; anything
stronger than "find the script that made this edit" is future work tracked under issue #259.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from generated_edit_declarations import append_generated_edit_declaration, write_replay_registration

GENERATED_ARTIFACTS_KEY = "generated_artifacts"


def sha256_file(path: Path) -> str:
    """Hash a file for declaration evidence."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_target(bundle: Path, target: Path) -> Path:
    """Resolve an absolute or bundle-relative target path."""
    return target if target.is_absolute() else bundle / target


def load_generated_run(bundle: Path) -> dict:
    """Read the engine-run identity that declarations bind to."""
    manifest = json.loads((bundle / "input_manifest.json").read_text(encoding="utf-8"))
    generated = manifest.get(GENERATED_ARTIFACTS_KEY) if isinstance(manifest, dict) else None
    if not isinstance(generated, dict) or generated.get("version") != 1 or not generated.get("run_id"):
        raise ValueError(f"{bundle / 'input_manifest.json'} has no generated_artifacts v1 run identity")
    return generated


def script_identity_of(bundle: Path, script: Path) -> str:
    """A replay script's manifest key: bundle-relative when possible, absolute otherwise."""
    return script.relative_to(bundle).as_posix() if script.is_relative_to(bundle) else str(script)


def register_replay_script(bundle: Path, script: Path, target: Path, purpose: str) -> Path:
    """Write this replay script's navigational manifest row (issue #259) - unconditionally, even on
    a `DECLARE: NO_CHANGE` run, since findability doesn't depend on whether the target's hash
    changed this time. This is a discoverability index only, not proof of anything about the
    script or the edit - see the module docstring."""
    generated = load_generated_run(bundle)
    return write_replay_registration(
        bundle,
        {
            "version": 1,
            "run_id": generated["run_id"],
            "target": target.relative_to(bundle).as_posix(),
            "script_identity": script_identity_of(bundle, script),
            "purpose": purpose,
        },
    )


def append_declaration(
    bundle: Path,
    target: Path,
    script: Path,
    baseline_sha256: str | None,
    expected_sha256: str | None,
) -> Path:
    """Append the declaration consumed by ``check_migration_progress.py --tamper``."""
    generated = load_generated_run(bundle)
    rel_target = target.relative_to(bundle).as_posix()
    kind = "added" if baseline_sha256 is None else "missing" if expected_sha256 is None else "changed"
    return append_generated_edit_declaration(
        bundle,
        {
            "version": 1,
            "run_id": generated["run_id"],
            "kind": kind,
            "target": rel_target,
            "baseline_sha256": baseline_sha256,
            "expected_sha256": expected_sha256,
            "script_identity": script.relative_to(bundle).as_posix() if script.is_relative_to(bundle) else str(script),
            "script_sha256": sha256_file(script),
            "reason": "declared generated-artifact repair",
        },
    )


def main(argv: list[str] | None = None) -> int:
    """Run the fix script and record the target's resulting hash."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path, help="migration bundle with input_manifest.json")
    parser.add_argument(
        "--target", required=True, type=Path, help="generated artifact path, absolute or bundle-relative"
    )
    parser.add_argument("--script", required=True, type=Path, help="replayable fix script to run")
    parser.add_argument(
        "--purpose", default="", help="one-line reason this replay script exists, for the navigational manifest"
    )
    parser.add_argument("script_args", nargs=argparse.REMAINDER, help="arguments passed to the fix script after --")
    args = parser.parse_args(argv)

    bundle = args.bundle.resolve()
    target = resolve_target(bundle, args.target).resolve()
    script = args.script.resolve()
    script_args = args.script_args[1:] if args.script_args[:1] == ["--"] else args.script_args
    before = sha256_file(target) if target.is_file() else None

    result = subprocess.run([sys.executable, str(script), *script_args], cwd=bundle, check=False)
    if result.returncode != 0:
        return result.returncode

    registration = register_replay_script(bundle, script, target, args.purpose)

    after = sha256_file(target) if target.is_file() else None
    if before == after:
        print(f"DECLARE: NO_CHANGE {target} (registered {registration})")
        return 0
    declaration = append_declaration(bundle, target, script, before, after)
    print(f"DECLARE: RECORDED {target.relative_to(bundle).as_posix()} -> {declaration} (registered {registration})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
