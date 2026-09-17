"""
purpose: Simulate a recorded credential condition for offline feedback tests; opens no dialog.
usage:   python probe.py <condition.json>
"""

import json
import sys
from pathlib import Path


def main() -> None:
    """Report a fictitious condition, not a real credential probe."""
    condition = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps({"blocked": condition["credential"] == "missing"}))


if __name__ == "__main__":
    main()
