"""
purpose: Independent fictitious CLI contract: absent required data must never be admitted.
usage:   python oracle.py <case.json> <predicate.json>
"""

import hashlib
import json
import sys
from pathlib import Path


def expected(raw: bytes) -> bool:
    """Judge the input contract without using the broken strict-mode default."""
    return json.loads(raw).get("value") is not None


if __name__ == "__main__":
    source = Path(sys.argv[1]).read_bytes()
    result = expected(source)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "command": sys.orig_argv,
                "cwd": str(Path.cwd()),
                "input_sha256": hashlib.sha256(source).hexdigest(),
                "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "predicate_sha256": hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest(),
                "expected": result,
            }
        )
    )
