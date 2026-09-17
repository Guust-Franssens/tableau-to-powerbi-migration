"""
purpose: Independent fictitious expectation; every input has one worksheet regardless of feature.
usage:   python oracle.py <case.twb>
"""

import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def expected(path: Path) -> int:
    """Count source worksheets independently of the mutated output producer."""
    if path.suffix.endswith("x"):
        with zipfile.ZipFile(path) as archive:
            raw = archive.read(archive.namelist()[0])
    else:
        raw = path.read_bytes()
    root = ElementTree.fromstring(raw)
    return len(root.findall("column" if root.tag == "datasource" else "./worksheets/worksheet"))


if __name__ == "__main__":
    print(expected(Path(sys.argv[1])))
