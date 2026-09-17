"""
purpose: Independent fictitious expectation; every input has one worksheet regardless of feature.
usage:   python oracle.py <case.twb> <predicate.json>
"""

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def expected(raw: bytes, *, packed: bool) -> int:
    """Count source worksheets independently of the mutated output producer."""
    if packed:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            raw = archive.read(archive.namelist()[0])
    root = ElementTree.fromstring(raw)
    return len(root.findall("column" if root.tag == "datasource" else "./worksheets/worksheet"))


if __name__ == "__main__":
    source = Path(sys.argv[1]).read_bytes()
    result = expected(source, packed=Path(sys.argv[1]).suffix.endswith("x"))
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
