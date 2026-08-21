"""
purpose: run a generated-artifact fix script and record before/after hashes for tamper detection.
usage:   python scripts/declare_generated_edit.py --bundle <dir> --target <generated-path>
                                           --script <_build/fix_*.py> [-- <script-args>...]

This is an audit wrapper, not an authorization system. It records what a replayable fix script did so
``check_migration_progress.py --tamper`` can distinguish declared repairs from silent drift. Anyone
who can edit the bundle can also forge the JSON record; review still has to decide whether the script
is a legitimate, idempotent repair.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from generated_edit_declarations import append_generated_edit_declaration

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

    after = sha256_file(target) if target.is_file() else None
    if before == after:
        print(f"DECLARE: NO_CHANGE {target}")
        return 0
    declaration = append_declaration(bundle, target, script, before, after)
    print(f"DECLARE: RECORDED {target.relative_to(bundle).as_posix()} -> {declaration}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
