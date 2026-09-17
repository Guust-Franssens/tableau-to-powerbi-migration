"""
purpose: Fictitious user acceptance criterion: the requested operation should not be blocked.
usage:   python oracle.py <condition.json> <predicate.json>
"""

import hashlib
import json
import sys
from pathlib import Path


def expected(raw: bytes) -> bool:
    """Return the independent acceptance criterion, not the probe's diagnosis."""
    condition = json.loads(raw)
    assert condition["credential"] in {"present", "missing"}, "not a supported credential control"
    return False


if __name__ == "__main__":
    source = Path(sys.argv[1]).read_bytes()
    RESULT = expected(source)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "command": sys.orig_argv,
                "cwd": str(Path.cwd()),
                "input_sha256": hashlib.sha256(source).hexdigest(),
                "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "predicate_sha256": hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest(),
                "expected": RESULT,
            }
        )
    )
