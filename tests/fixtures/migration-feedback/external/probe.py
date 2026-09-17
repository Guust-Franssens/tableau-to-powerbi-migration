"""
purpose: Simulate a recorded credential condition for offline feedback tests; opens no dialog.
usage:   python probe.py <condition.json>
"""

import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    """Report a fictitious condition, not a real credential probe."""
    raw = Path(sys.argv[1]).read_bytes()
    condition = json.loads(raw)
    output = json.dumps({"blocked": condition["credential"] == "missing"}) + "\n"
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
