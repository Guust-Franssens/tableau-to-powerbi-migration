"""Round-3 mutation proof for the revision key.

⚠️ Every mutant carries a PROBE that calls the patched object and asserts a mutation-specific result
BEFORE the anchor test runs. This repo has a measured case of 22/22 mutations scored "caught" that
were all false positives - an import error made the test command exit non-zero - so "the anchor
failed" is not evidence unless the mutation demonstrably took effect.

usage: uv run python tests/mutate_revision_key.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (label, file, find, replace, probe expression -> must be True under the mutant, anchor, control)
MUTATIONS: list[tuple[str, str, str, str, str, str, str]] = [
    (
        "revision_key: hash the container raw instead of its contents",
        "scripts/object_identity.py",
        "    digest = hashlib.sha256()\n"
        "    for name, data in members:\n"
        '        digest.update(name.encode("utf-8"))\n'
        "        digest.update(hashlib.sha256(data).digest())\n"
        "    return RevisionKey(algo=REVISION_ALGO_ARCHIVE, value=digest.hexdigest())",
        "    return RevisionKey(algo=REVISION_ALGO_ARCHIVE, value=hashlib.sha256(payload).hexdigest())",
        # A repacked archive must now produce a DIFFERENT key - which is the defect being re-created.
        "probe_repack_now_differs",
        "tests/test_workbook_identity.py::test_a_repack_changes_the_raw_digest_and_not_the_revision_key",
        "tests/test_workbook_identity.py::test_a_key_never_compares_across_algorithms",
    ),
    (
        "revision_key: drop the member NAME from the digest",
        "scripts/object_identity.py",
        '        digest.update(name.encode("utf-8"))\n        digest.update(hashlib.sha256(data).digest())',
        "        digest.update(hashlib.sha256(data).digest())",
        "probe_rename_now_agrees",
        "tests/test_workbook_identity.py::test_genuinely_different_content_still_differs",
        "tests/test_workbook_identity.py::test_a_repack_changes_the_raw_digest_and_not_the_revision_key",
    ),
    (
        "revision_key: silently fall back to a raw hash for an unreadable archive",
        "scripts/object_identity.py",
        "    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):\n        return None",
        "    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):\n"
        "        return RevisionKey(algo=REVISION_ALGO_ARCHIVE, value=hashlib.sha256(payload).hexdigest())",
        "probe_truncated_now_keyed",
        "tests/test_workbook_identity.py::test_an_unreadable_archive_yields_no_key_rather_than_a_raw_one",
        "tests/test_workbook_identity.py::test_a_repack_changes_the_raw_digest_and_not_the_revision_key",
    ),
    (
        "RevisionKey: compare across algorithms instead of refusing",
        "scripts/object_identity.py",
        "        if other is None or self.algo != other.algo:\n            return None",
        "        if other is None:\n            return None",
        "probe_cross_algo_now_compares",
        "tests/test_workbook_identity.py::test_a_key_never_compares_across_algorithms",
        "tests/test_workbook_identity.py::test_a_repack_changes_the_raw_digest_and_not_the_revision_key",
    ),
    (
        "revision_status: a missing revision_match is read as drift",
        "scripts/reference_evidence.py",
        '    return REVISION_CONFIRMED if origin.get("match") == "sha256" else REVISION_UNCONFIRMED',
        '    return REVISION_CONFIRMED if origin.get("match") == "sha256" else REVISION_MISMATCH',
        "probe_missing_key_now_mismatch",
        "tests/test_reference_evidence.py::test_a_manifest_stamped_before_the_key_existed_is_unconfirmed_not_drifted",
        "tests/test_reference_evidence.py::test_a_byte_confirmed_provenance_luid_certifies_normally",
    ),
    (
        "revision_status: ignore revision_match and go back to the raw verdict",
        "scripts/reference_evidence.py",
        '    declared = origin.get("revision_match")',
        "    declared = None",
        "probe_revision_match_ignored",
        "tests/test_reference_evidence.py::test_a_repacked_archive_is_confirmed_rather_than_merely_unconfirmed",
        "tests/test_reference_evidence.py::test_a_byte_confirmed_provenance_luid_certifies_normally",
    ),
    (
        "fingerprint: stop recording the local revision key",
        "scripts/stamp_tableau_provenance.py",
        '        record["revision_key"] = key.as_json()',
        "        pass",
        "probe_fingerprint_has_no_key",
        "tests/test_stamp_tableau_provenance.py::test_a_repacked_site_copy_is_recorded_as_the_same_revision",
        "tests/test_stamp_tableau_provenance.py::test_matching_bytes_are_recorded_as_a_sha256_match",
    ),
    (
        "revision_key: stop normalising the server build stamp",
        "scripts/object_identity.py",
        '    return XML_COMMENT_RE.sub(b"", data)',
        "    return data",
        "probe_build_stamp_now_differs",
        "tests/test_workbook_identity.py::test_the_server_build_stamp_does_not_count_as_a_changed_workbook",
        "tests/test_workbook_identity.py::test_a_repack_changes_the_raw_digest_and_not_the_revision_key",
    ),
    (
        "revision_key: a flat XML payload is hashed raw again",
        "scripts/object_identity.py",
        "        if _is_xml(payload):",
        "        if False:",
        "probe_flat_xml_now_raw",
        "tests/test_workbook_identity.py::test_a_non_archive_uses_its_own_shape_and_says_so",
        "tests/test_workbook_identity.py::test_a_repack_changes_the_raw_digest_and_not_the_revision_key",
    ),
]

PROBES = r"""
import hashlib, io, sys, zipfile
sys.path.insert(0, "scripts")
import object_identity as oid
import reference_evidence as ev
import stamp_tableau_provenance as prov
from pathlib import Path

M = [("Book.twb", b"<workbook/>"), ("Data/x.hyper", b"\x00extract")]

def _zip(members, dt):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in members:
            z.writestr(zipfile.ZipInfo(n, date_time=dt), d)
    return buf.getvalue()

A = _zip(M, (2026, 1, 1, 0, 0, 0))
B = _zip(list(reversed(M)), (2026, 9, 3, 11, 22, 33))

def probe_repack_now_differs():
    return oid.revision_key(A) != oid.revision_key(B)

def probe_rename_now_agrees():
    renamed = _zip([("Cook.twb", M[0][1]), M[1]], (2026, 1, 1, 0, 0, 0))
    return oid.revision_key(A).agrees_with(oid.revision_key(renamed)) is True

def probe_truncated_now_keyed():
    return oid.revision_key(A[:64]) is not None

def probe_cross_algo_now_compares():
    a = oid.RevisionKey(algo=oid.REVISION_ALGO_ARCHIVE, value="a" * 64)
    f = oid.RevisionKey(algo=oid.REVISION_ALGO_FLAT, value="a" * 64)
    return a.agrees_with(f) is not None

def probe_missing_key_now_mismatch():
    return ev.revision_status({"match": "name_only"}, []) == ev.REVISION_MISMATCH

def probe_revision_match_ignored():
    return ev.revision_status({"match": "name_only", "revision_match": "same"}, []) != ev.REVISION_CONFIRMED

def probe_fingerprint_has_no_key():
    p = Path(sys.argv[2])
    p.write_bytes(A)
    return "revision_key" not in prov.fingerprint(p)

LOCAL = b"<?xml version='1.0'?>\n<!-- build 1 -->\n<workbook><worksheets/></workbook>"
REMOTE = b"<?xml version='1.0'?>\n<!-- build 2 -->\n<workbook><worksheets/></workbook>"

def probe_build_stamp_now_differs():
    return oid.revision_key(LOCAL) != oid.revision_key(REMOTE)

def probe_flat_xml_now_raw():
    return oid.revision_key(LOCAL).algo == oid.REVISION_ALGO_FLAT

print("PROBE_RESULT", bool(globals()[sys.argv[1]]()))
"""


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True, check=False, encoding="utf-8"
    )


def main() -> int:
    """Apply each mutation; require the PROBE to fire, the anchor to fail and the control to pass."""
    probe_file = ROOT / "_probes_tmp.py"
    probe_file.write_text(PROBES, encoding="utf-8")
    scratch_twbx = ROOT / "_probe_tmp.twbx"
    failures = []
    for label, relative, find, replace, probe, anchor, control in MUTATIONS:
        path = ROOT / relative
        original = path.read_text(encoding="utf-8")
        if original.count(find) != 1:
            print(f"UNAPPLIED  {label} (pattern x{original.count(find)} in {relative})")
            failures.append(label)
            continue
        path.write_text(original.replace(find, replace), encoding="utf-8")
        try:
            probed = run(str(probe_file), probe, str(scratch_twbx))
            fired = "PROBE_RESULT True" in probed.stdout
            anchor_code = run("-m", "pytest", anchor, "-q", "--no-header", "-p", "no:cacheprovider").returncode
            control_code = run("-m", "pytest", control, "-q", "--no-header", "-p", "no:cacheprovider").returncode
        finally:
            path.write_text(original, encoding="utf-8")
        ok = fired and anchor_code != 0 and control_code == 0
        verdict = "KILLED" if ok else ("PROBE DID NOT FIRE" if not fired else "SURVIVED/CONTROL BROKE")
        print(f"{verdict:<22} {label}\n{'':<22} probe_fired={fired} anchor={anchor_code} control={control_code}")
        if not fired and probed.stderr:
            print(f"{'':<22} probe stderr: {probed.stderr.strip().splitlines()[-1][:120]}")
        if not ok:
            failures.append(label)
    print(f"\n{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} killed with a firing probe")
    probe_file.unlink(missing_ok=True)
    scratch_twbx.unlink(missing_ok=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
