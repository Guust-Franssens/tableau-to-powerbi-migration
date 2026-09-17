"""
purpose: Independent fictitious CLI contract: absent required data must never be admitted.
usage:   python oracle.py <case.json>
"""

import json
import sys
from pathlib import Path


def expected(path: Path) -> bool:
    """Judge the input contract without using the broken strict-mode default."""
    return json.loads(path.read_text(encoding="utf-8")).get("value") is not None


if __name__ == "__main__":
    print(json.dumps(expected(Path(sys.argv[1]))))
