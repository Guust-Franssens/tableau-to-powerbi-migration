"""
purpose: Fictitious local CLI default bug for feedback controls; strict defaults off incorrectly.
usage:   python cli.py <case.json>
"""

import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    """Incorrectly admit a missing value when the caller omits strict mode."""
    raw = Path(sys.argv[1]).read_bytes()
    request = json.loads(raw)
    output = json.dumps({"allowed": request.get("value") is not None or not request.get("strict", False)}) + "\n"
    sys.stdout.buffer.write(output.encode("utf-8"))
    print(
        json.dumps(
            {
                "schema_version": 1,
                "command": sys.orig_argv,
                "cwd": str(Path.cwd()),
                "owner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "input_sha256": hashlib.sha256(raw).hexdigest(),
                "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            }
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
