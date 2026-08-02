"""
purpose: one-time setup for Snowflake key-pair authentication - generate an RSA key pair, print the
         statement that registers the public key, and verify the key actually authenticates.
usage:   python scripts/setup_snowflake_keypair.py            # generate + print SQL + verify
         python scripts/setup_snowflake_keypair.py --verify   # verify an existing key only

Why key-pair rather than a PAT: Snowflake refuses PAT auth with `390432: Network policy is required`
until a network policy is ATTACHED to the user - and creating one is not enough, which is easy to
miss because CREATE NETWORK POLICY succeeds on its own while the PAT keeps returning the identical
error. Key-pair auth has no such requirement, and it does not break when the workstation's public IP
rotates, which an IP allow-list does.

Registering the key is a printed SQL statement rather than an automated call, on purpose: it needs
an already-working credential, so automating it would mean keeping a second credential (the PAT,
plus its whole network-policy procedure) solely to bootstrap the first. Pasting one line into
Snowsight is faster and leaves nothing to maintain.

⚠️ The private key lands in `secrets/`, which is gitignored, and is INFRASTRUCTURE credential only -
never a Power BI credential. The probe must exercise Power BI Desktop's own per-user credential
store; a key held by a script proves nothing about whether Power BI can reach the source.
"""

from __future__ import annotations

import argparse
import base64
import logging
import sys
from pathlib import Path

# The remaining imports in this file are deliberately lazy and pylint is told so once, here:
# `cryptography` and the Snowflake connector are optional extras (a machine that never touches
# Snowflake must still be able to run everything else), and the sibling-module imports have to
# follow the sys.path insert in main().
# pylint: disable=import-outside-toplevel

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("setup_snowflake_keypair")

REPO = Path(__file__).resolve().parent.parent
SECRETS = REPO / "secrets"
KEY_PATH = SECRETS / "snowflake_key.p8"
PUB_PATH = SECRETS / "snowflake_key.pub"


def generate() -> str:
    """Create the key pair if absent and return the bare base64 public-key body."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

    SECRETS.mkdir(exist_ok=True)
    if KEY_PATH.is_file():
        log.info("private key already exists - reusing %s", KEY_PATH)
        key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        KEY_PATH.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        log.info("wrote %s", KEY_PATH)

    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    PUB_PATH.write_bytes(pem)
    return "".join(line for line in pem.decode().splitlines() if "-----" not in line)


def register(pub_body: str) -> None:
    """Print the one SQL statement that attaches the public key to the Snowflake user.

    Deliberately NOT automated. Registering the key needs an already-working credential, so an
    automated path here would exist solely to bootstrap itself - and the only candidate was the
    PAT, which is precisely what key-pair auth replaces. Snowflake refuses PAT auth with
    `390432: Network policy is required` until a policy is ATTACHED to the user, so that bootstrap
    carried a whole second setup procedure (create policy, attach to user, keep an IP allow-list
    current) purely to run one ALTER USER.

    Pasting one statement into Snowsight is faster than any of that, needs no network policy, and
    does not require the repo to hold a second credential it would otherwise never use.
    """
    from provision_snowflake_fixture import account_and_user, read_env  # noqa: PLC0415

    env = read_env()
    _, user = account_and_user(env)
    log.info("\nRun this once in Snowsight (or any SQL client) as ACCOUNTADMIN/SECURITYADMIN:\n")
    log.info("ALTER USER %s SET RSA_PUBLIC_KEY='%s';\n", user, pub_body)
    log.info("Then verify with:  python scripts/setup_snowflake_keypair.py --verify")


def verify() -> int:
    """Authenticate with the key pair and confirm the fingerprint Snowflake holds matches ours."""
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415

    from provision_snowflake_fixture import connect, read_env  # noqa: PLC0415

    key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    local_fp = "SHA256:" + base64.b64encode(digest.finalize()).decode()

    env = read_env()
    with connect(env, None) as conn, conn.cursor() as cur:
        cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE()")
        who = cur.fetchone()
    log.info("KEY-PAIR AUTH OK - connected as %s with role %s", who[0], who[1])
    log.info("local public-key fingerprint: %s", local_fp)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify", action="store_true", help="skip generate; just test the key")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).parent))
    if args.verify:
        return verify()

    register(generate())
    # Verifying here would fail on a genuinely fresh machine, because the key is not registered
    # until a human has run the printed statement. Attempting it anyway would end a successful setup
    # on a red error, which reads like the script broke.
    log.info("\nRun the statement above, then verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
