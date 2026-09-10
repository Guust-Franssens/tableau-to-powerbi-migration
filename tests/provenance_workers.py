"""Leaf-worker stand-ins for the supervised provenance phase (issue #576).

These live in their own module for one mechanical reason: the supervisor starts the worker with
``multiprocessing.get_context("spawn")``, so the target is pickled BY REFERENCE and re-imported in a
fresh interpreter. A closure or a local function in a test cannot cross that boundary; a top-level
function in an importable module can, and ``functools.partial`` over one carries its scenario with
it.

They import stdlib only, on purpose - the child pays the import cost on every spawn, and pulling
pytest into it would make each of these tests a second slower for no benefit.

⚠️ Nothing here is production code. What each one simulates is a BLOCK the parent must preempt: an
operation that never returns, a worker that dies mid-flight, a result that arrives after the latch.
"""

from __future__ import annotations

import os
import time
from typing import Any

MESSAGE_INPUTS_DISCOVERED = "inputs-discovered"
MESSAGE_OPERATION = "operation"
MESSAGE_CHECKPOINT = "checkpoint"
MESSAGE_SAFE_SNAPSHOT = "safe-snapshot"
MESSAGE_TERMINAL = "terminal"

#: The derived-only shape a real checkpoint has: sizes, digests and CRCs, never a filename.
CHECKPOINT_SHA = "a" * 64
SECOND_CHECKPOINT_SHA = "b" * 64


def _checkpoint(index: int, digest: str = CHECKPOINT_SHA) -> dict[str, Any]:
    return {
        "kind": MESSAGE_CHECKPOINT,
        "index": index,
        "record": {"input": {"size_bytes": 11 + index, "sha256": digest}},
    }


def _operation(operation: str, completed: int = 0, total: int | None = None) -> dict[str, Any]:
    return {"kind": MESSAGE_OPERATION, "operation": operation, "completed": completed, "total": total}


def _scrubbed_result(files: tuple[str, ...] = ("unit.twb",)) -> dict[str, Any]:
    """A complete, already-scrubbed result - what the worker holds once scrub has succeeded."""
    return {
        "schema": "tableau-source-provenance/1",
        "stamped_at": "2026-09-10T00:00:00Z",
        "input_count": len(files),
        "inputs": [
            {"input": {"file": name, "size_bytes": 11, "sha256": CHECKPOINT_SHA}, "origin": None} for name in files
        ],
        "phase": {"status": "local_only", "errors": []},
    }


def _block_forever() -> None:
    """Stand in for an operation with no bound: a trickled read, a hung scrub, a dead sign-out.

    It deliberately ignores cancellation. Cooperative checks are an optimisation between operations;
    what has to be proved here is that the parent stops a worker that will NEVER stop itself.
    """
    while True:
        time.sleep(3600)


def blocks_in(operation: str, conn, _cancel_event, _payload) -> None:
    """Announce one operation class, then never return."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 2})
    conn.send(_operation(operation, 0, 2))
    _block_forever()


def blocks_in_with_evidence(operation: str, conn, _cancel_event, _payload) -> None:
    """One completed input checkpointed, then a block inside `operation` with the second unfinished."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 2})
    conn.send(_operation("fingerprint", 1, 2))
    conn.send(_checkpoint(0))
    conn.send(_operation(operation, 1, 2))
    _block_forever()


def blocks_after_safe_snapshot(conn, _cancel_event, _payload) -> None:
    """Everything finished and scrubbed, then a sign-out that never answers."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 1})
    conn.send(_operation("fingerprint", 1, 1))
    conn.send(_checkpoint(0))
    conn.send({"kind": MESSAGE_SAFE_SNAPSHOT, "result": _scrubbed_result()})
    conn.send(_operation("sign-out", 0, 1))
    _block_forever()


def sends_a_late_terminal(conn, _cancel_event, _payload) -> None:
    """One accepted checkpoint, then a SUCCESS result that arrives after the deadline has latched."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 2})
    conn.send(_operation("fingerprint", 1, 2))
    conn.send(_checkpoint(0))
    conn.send(_operation("content", 0, None))
    time.sleep(30)
    conn.send({"kind": MESSAGE_TERMINAL, "result": _scrubbed_result(("late-a.twb", "late-b.twb"))})
    time.sleep(30)


def crashes_after_a_checkpoint(conn, _cancel_event, _payload) -> None:
    """Dies mid-phase with a distinctive code, after one input's evidence has already landed."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 2})
    conn.send(_operation("fingerprint", 1, 2))
    conn.send(_checkpoint(0))
    time.sleep(0.05)
    os._exit(7)  # noqa: SLF001  # a crash, not an orderly exit: no atexit, no flush, no terminal


def sends_an_unsafe_checkpoint(secret: str, conn, _cancel_event, _payload) -> None:
    """A checkpoint carrying a copied string - the mutation the parent's protocol must refuse."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 1})
    conn.send(_operation("fingerprint", 1, 1))
    conn.send(
        {
            "kind": MESSAGE_CHECKPOINT,
            "index": 0,
            "record": {"input": {"file": secret, "size_bytes": 11, "sha256": CHECKPOINT_SHA}},
        }
    )
    _block_forever()


def sends_a_secret_bearing_snapshot(secret: str, conn, _cancel_event, _payload) -> None:
    """A legitimately scrubbed snapshot that still holds copied strings; it must never be RENDERED."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 1})
    conn.send(_operation("fingerprint", 1, 1))
    conn.send(_checkpoint(0))
    conn.send({"kind": MESSAGE_SAFE_SNAPSHOT, "result": _scrubbed_result((secret,))})
    conn.send(_operation("sign-out", 0, 1))
    _block_forever()


def succeeds(conn, _cancel_event, _payload) -> None:
    """The ordinary path: discovery, one fingerprint, one terminal result, then exit."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 1})
    conn.send(_operation("fingerprint", 1, 1))
    conn.send(_checkpoint(0))
    conn.send({"kind": MESSAGE_TERMINAL, "result": _scrubbed_result()})
    conn.close()


def sends_an_unknown_message(conn, _cancel_event, _payload) -> None:
    """Speaks outside the closed protocol. Fail closed rather than interpret it."""
    conn.send({"kind": "whatever-i-like", "payload": "trust me"})
    _block_forever()
