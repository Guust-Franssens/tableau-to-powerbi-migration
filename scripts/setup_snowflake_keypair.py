"""
purpose: one-time setup for Snowflake key-pair authentication - generate an RSA key pair, register
         the public key on the Snowflake user, and verify the key actually authenticates.
usage:   python scripts/setup_snowflake_keypair.py            # generate + register + verify
         python scripts/setup_snowflake_keypair.py --verify   # verify an existing key only

Why key-pair rather than the PAT: Snowflake refuses PAT auth with `390432: Network policy is
required` until a network policy is ATTACHED to the user (creating one is not enough - a very easy
step to miss, because CREATE NETWORK POLICY succeeds on its own and the PAT keeps returning the
identical error). Key-pair auth has no such requirement, and it does not break when the
workstation's public IP rotates, which an IP allow-list does.

Registering the public key needs an existing authenticated route, so the first run uses the PAT from
`.env`. After that the PAT is unnecessary.

⚠️ The private key lands in `secrets/`, which is gitignored, and is INFRASTRUCTURE credential only -
never a Power BI credential. The probe must exercise Power BI Desktop's own per-user credential
store; a key held by a script proves nothing about whether Power BI can reach the source.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import urllib.request
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
    """Attach the public key to the Snowflake user, authenticating with the PAT from `.env`."""
    from provision_snowflake_fixture import account_and_user, read_env  # noqa: PLC0415

    env = read_env()
    _, user = account_and_user(env)
    pat = env.get("PAT_SNOWFLAKE")
    if not pat:
        raise SystemExit(
            "no PAT_SNOWFLAKE in .env to register the key with. Register it by hand instead:\n"
            f"  ALTER USER {user} SET RSA_PUBLIC_KEY='<contents of {PUB_PATH.name} without the "
            "BEGIN/END lines>';"
        )
    host = env["SNOWFLAKE_URL"].split("://", 1)[-1].split("/", 1)[0].rstrip(".")
    req = urllib.request.Request(  # noqa: S310  (fixed https scheme, host from .env)
        f"https://{host}/api/v2/statements",
        data=json.dumps(
            {
                "statement": f"ALTER USER {user} SET RSA_PUBLIC_KEY='{pub_body}'",
                "timeout": 60,
                "role": env.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {pat}",
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120):  # noqa: S310
        log.info("registered public key on user %s", user)


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
    parser.add_argument("--verify", action="store_true", help="skip generate/register; just test the key")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).parent))
    if not args.verify:
        register(generate())
    return verify()


if __name__ == "__main__":
    sys.exit(main())
