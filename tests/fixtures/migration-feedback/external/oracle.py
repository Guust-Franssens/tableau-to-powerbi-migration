"""
purpose: Fictitious user acceptance criterion: the requested operation should not be blocked.
usage:   python oracle.py <condition.json>
"""

import json


def expected() -> bool:
    """Return the independent acceptance criterion, not the probe's diagnosis."""
    return False


if __name__ == "__main__":
    print(json.dumps(expected()))
