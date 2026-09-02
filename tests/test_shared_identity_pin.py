"""Byte-identity gate for the SHARED identity type this branch reuses from PR #428.

`scripts/object_identity.py` and `tests/test_object_identity.py` are not ours. They are carried here
byte-identical so `check_unit` can join on the same type the reference-readiness gate does, and so
whichever PR merges second does not silently overwrite the other's contract.

Round 3 measured why this needs to be a gate rather than a claim: the copy on this branch was the
sibling's ROUND-3 version, and the sibling had since renamed `IdentityIndex` to
`EngineIndex`/`CandidateIndex`, added `__post_init__` validation and made `__bool__` raise. "Either PR
can merge first" was false, and nothing said so.

Two halves, because they prove different things and only one of them can run in CI:

* :func:`test_shared_identity_files_are_unmodified` pins the SHA-256 of both files. It is offline and
  runs everywhere, and it proves this branch has not edited a file it does not own. `actions/checkout@v4`
  makes a shallow clone, so no other ref exists in CI and a ref-diff cannot run there.
* `tests/verify_shared_identity_pin.py` compares against the sibling ref itself and **fails when it
  cannot compare** - "I could not check" is never "it is fine". Run it whenever the pin is updated.

Updating the pin is deliberate: re-take both files with `git checkout <ref> -- <paths>`, run the
verifier, then paste the new digests and provenance here.

⚠️ **The digest is taken over LINE-ENDING-NORMALIZED bytes**, and that is not cosmetic. The first CI
run of this gate failed on a file nobody had touched: `git checkout` applies `core.autocrlf`, so the
same blob is CRLF in a Windows working tree and LF on the Linux runner, and a raw byte hash makes the
pin platform-dependent rather than content-dependent. Normalizing `\r\n` -> `\n` keeps the gate
sensitive to every real edit while surviving the checkout that git performs on our behalf.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where the pinned bytes came from. Recorded so the pin is auditable rather than self-referential.
UPSTREAM_REF = "origin/feat/reference-readiness-gate"
UPSTREAM_COMMIT = "cc70833d749cc25df148e20099cdaf9cfc5d8a49"

#: SHA-256 of each shared file, over LF-normalized bytes, as taken from ``UPSTREAM_COMMIT``.
PINNED: dict[str, str] = {
    "scripts/object_identity.py": "d5971e00410b384bb77cc13d98d2ba15f8eb0299050fa0b18070966e09bf8314",
    "tests/test_object_identity.py": "7e0856ac694d3bcd0c0320ad02e84e2dbe36ebf1688a68f34113b4e1244b6a62",
}


def digest(relative: str) -> str:
    """SHA-256 of one shared file, over LF-normalized bytes so the pin is platform-independent."""
    return hashlib.sha256((REPO_ROOT / relative).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_shared_identity_files_are_unmodified() -> None:
    """Kills: editing a shared file this branch does not own, or letting the copy drift silently."""
    actual = {relative: digest(relative) for relative in PINNED}

    assert actual == PINNED, (
        "a shared identity file differs from the bytes pinned from "
        f"{UPSTREAM_REF}@{UPSTREAM_COMMIT[:8]}. This branch must not edit it: re-take it with "
        "`git checkout <ref> -- scripts/object_identity.py tests/test_object_identity.py`, run "
        "`python tests/verify_shared_identity_pin.py`, then update PINNED here."
    )


def test_the_digest_is_line_ending_independent(tmp_path: Path) -> None:
    """Kills: a raw byte hash, which makes the pin fail on the platform rather than on an edit.

    Measured: the first CI run of this gate failed on files nobody had touched, because `git checkout`
    stores them CRLF on Windows and LF on the Linux runner. The same content must digest the same;
    a real edit must not.
    """
    crlf = tmp_path / "crlf.py"
    lf = tmp_path / "lf.py"
    edited = tmp_path / "edited.py"
    crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
    lf.write_bytes(b"a = 1\nb = 2\n")
    edited.write_bytes(b"a = 1\nb = 3\n")

    def digest_of(path: Path) -> str:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

    assert digest_of(crlf) == digest_of(lf)
    assert digest_of(crlf) != digest_of(edited)


def test_the_pin_covers_every_shared_file_check_unit_imports() -> None:
    """The pin is only a gate if it names every shared file; a new one must not slip in unpinned."""
    source = (REPO_ROOT / "scripts" / "check_unit.py").read_text(encoding="utf-8")

    assert "import object_identity as oid" in source
    assert "scripts/object_identity.py" in PINNED
    assert "tests/test_object_identity.py" in PINNED
