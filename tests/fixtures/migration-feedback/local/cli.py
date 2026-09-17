"""
purpose: Fictitious local CLI default bug for feedback controls; strict defaults off incorrectly.
usage:   python cli.py <case.json>
"""

import json
import sys
from pathlib import Path


def main() -> None:
    """Incorrectly admit a missing value when the caller omits strict mode."""
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps({"allowed": request.get("value") is not None or not request.get("strict", False)}))


if __name__ == "__main__":
    main()
