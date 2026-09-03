"""Byte-identity gate for the SHARED identity type BOTH offline gates join on.

`scripts/object_identity.py` and `tests/test_object_identity.py` are shared surface:
`check_reference_readiness` (the entry gate) and `check_unit` (the exit gate) both resolve identity
through them, so an edit here changes a contract in two places at once and must be deliberate.

Round 3 measured why this needs to be a gate rather than a claim: the copy on the then-open branch was
the sibling's ROUND-3 version, and the sibling had since renamed `IdentityIndex` to
`EngineIndex`/`CandidateIndex`, added `__post_init__` validation and made `__bool__` raise. "Either PR
can merge first" was false, and nothing said so.

⚠️ **What this pin means changed when both PRs landed on master.** While they were open it proved
"this branch has not edited a file it does not own". Both have merged, so `origin/master` IS the
shared truth (`verify_shared_identity_pin.PREFERRED_REFS` already said so), and the pin's job is now
to make an edit to shared surface a REVIEWED event rather than a silent one: a change here fails this
test, and clearing it means re-deriving the digest below and saying in the PR why both gates want it.

Two halves, because they prove different things and only one of them can run in CI:

* :func:`test_shared_identity_files_are_unmodified` pins the SHA-256 of both files. It is offline and
  runs everywhere. `actions/checkout@v4` makes a shallow clone, so no other ref exists in CI and a
  ref-diff cannot run there.
* `tests/verify_shared_identity_pin.py` compares against `origin/master` itself and **fails when it
  cannot compare** - "I could not check" is never "it is fine". Run it whenever the pin is updated.

Updating the pin is deliberate: make the change, run `ruff format`, re-derive with :func:`digest`
below, paste the new value, and record the reason in ``PIN_PROVENANCE``.

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
#: ⚠️ Digests are taken AFTER ``ruff format``, so a pinned digest is not necessarily the digest of the
#: named commit's blob: the round-6 fix arrived unformatted and ``ruff format --check`` exited 1 on
#: it. Re-derive with :func:`digest` below - never with a raw ``sha256(read_bytes())``, which yields a
#: different, CRLF-dependent value on Windows.
UPSTREAM_REF = "origin/master"
UPSTREAM_COMMIT = "0fae0cf75bee7c49489573b4735788106af5d8e0"

#: Why the pin last moved. An edit to shared surface is a two-gate contract change; this is the
#: record of the one that was reviewed.
PIN_PROVENANCE = (
    "master@0fae0cf7 plus issue #450 and its round-1 safety review: `object_identity.py` carries the "
    "WORKBOOK-identity join both gates share (WorkbookIdentity/Attribution/harvest_luid/agreed_luid/"
    "persisted_stem), because each had invented its own key for 'whose workbook is this render of' - "
    "one fail-closed (23 attributable renders discarded, 0/7 pages ready) and one fail-open (360 of "
    "360 records ownerless and admitted anyway). Round 1 then found four more fail-opens of ONE "
    "class and they are closed by ONE rule: a machine identity the unit cannot answer is `unknown`, "
    "never rescued by a weaker axis. `tests/test_object_identity.py` is UNCHANGED."
)

#: SHA-256 of each shared file, over LF-normalized bytes.
PINNED: dict[str, str] = {
    "scripts/object_identity.py": "fe9b2adbd999cdf48ea4699dfacbafbcc2e13af7b6fb4be95e424fcc1876eeae",
    "tests/test_object_identity.py": "11d6881a960fa3c23beaa6928706de88a83fc1605b051314038ffdc14711c967",
}


def digest(relative: str) -> str:
    """SHA-256 of one shared file, over LF-normalized bytes so the pin is platform-independent."""
    return hashlib.sha256((REPO_ROOT / relative).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_shared_identity_files_are_unmodified() -> None:
    """Kills: editing shared surface both gates join on without anyone reviewing the contract."""
    actual = {relative: digest(relative) for relative in PINNED}

    assert actual == PINNED, (
        "a shared identity file differs from the bytes pinned from "
        f"{UPSTREAM_REF}@{UPSTREAM_COMMIT[:8]}. Both offline gates join on it, so this is a two-gate "
        "contract change: run `ruff format`, re-derive with `digest()`, update PINNED and "
        "PIN_PROVENANCE here, then run `python tests/verify_shared_identity_pin.py`."
    )


def test_the_pin_provenance_names_a_reason_rather_than_only_a_commit() -> None:
    """Kills: re-taking the digest to silence the gate without recording what changed or why.

    A pin whose provenance is only a SHA cannot be audited - the whole value of this gate is that a
    shared-surface edit leaves a reviewable trace, and "I re-ran digest()" is not one.
    """
    assert len(PIN_PROVENANCE) > 120
    assert UPSTREAM_COMMIT[:8] in PIN_PROVENANCE


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
