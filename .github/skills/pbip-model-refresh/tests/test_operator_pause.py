"""Exact-run retained evidence controls: synthetic pixels only, never Desktop or a data source."""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import _credential_modal as modal
import _operator_pause as pause

NATIVE = pytest.mark.skipif(sys.platform != "win32", reason="Windows native private-file publication control")
NATIVE_DLL = getattr(ctypes, "WinDLL", None)
PIXELS = b"\0\0\0" * 2 + b"\xff\xff\xff" * 2
PNG = modal._image_png(2, 2, PIXELS)
HASH = hashlib.sha256(PNG).hexdigest()


def make_run(parent: Path, number: int = 1) -> tuple[Path, Path]:
    """Small independent run.json fixture, not the code under test or a host allocator import."""
    root = parent / f"{number:03d}-control"
    scratch = root / "scratch"
    model = root / "bundle" / "pbip" / "unit.SemanticModel"
    scratch.mkdir(parents=True)
    model.mkdir(parents=True)
    (root / "run.json").write_text(
        json.dumps(
            {
                "run": number,
                "unit_key": "control",
                "allocated_dir_name": root.name,
                "allocated_abs_path": str(root),
                "status": "active",
            }
        ),
        encoding="utf-8",
    )
    return scratch, model


@pytest.fixture(name="run_paths")
def run_paths(tmp_path, monkeypatch):
    monkeypatch.delenv(modal.IMAGE_DIRECTORY_ENV, raising=False)
    return make_run(tmp_path)


@pytest.fixture
def context(run_paths):
    return pause.prepare_operator_pause(*run_paths, os.getpid())


def acquisition() -> dict:
    return {
        "captured_at_utc": "2026-09-18T07:00:00.000Z",
        "dialog_hwnd": "222",
        "owner_hwnd": "111",
        "sha256": HASH,
        "dimensions": {"width": "2", "height": "2"},
        "ownership_checks": {"before": True, "after_render": True, "after_write": True},
    }


def publish(context: pause.OperatorPauseContext) -> pause.PausePublication:
    deadline = time.monotonic() + 8
    stage = pause.reserve_operator_pause(context, deadline, lambda: False)
    try:
        # This is the acquisition writer opening the reserved image, not an adopted/copied image.
        modal._write_private_image(stage.image_path, PNG, existing=True)
        stage.publish(acquisition(), deadline, lambda: False)
        return stage
    finally:
        stage.lease.close()


def sample_record() -> dict:
    return {
        "schema": "pbip.operator-pause.v1",
        "pause_id": "0a" * 16,
        "state": "waiting",
        "phase": "pbip_refresh",
        "reason": "DIALOG_UNREADABLE",
        "captured_at_utc": "2026-09-18T07:00:00.000Z",
        "published_at_utc": "2026-09-18T07:00:01.000Z",
        "run": {"number": 1, "directory": "001-control"},
        "model_path": "bundle/pbip/unit.SemanticModel",
        "desktop": {"pid": "123", "process_start": "134026596000000000", "dialog_hwnd": "222", "owner_hwnd": "111"},
        "image": {
            "path": "prompt.png",
            "sha256": HASH,
            "dimensions": {"width": 2, "height": 2},
            "ownership_checks": {"before": True, "after_render": True, "after_write": True},
        },
    }


def test_schema_is_minimal_closed_typed_and_relative() -> None:
    record = sample_record()
    assert pause.validate_pause(record) == record
    encoded = json.dumps(record)
    assert not any(
        value in encoded for value in ("C:\\", "requested_action", "classification", "source_keys", "canaries")
    )


@pytest.mark.parametrize(
    "location,key,value",
    [
        ((), "schema", "future.v2"),
        ((), "pause_id", "../other"),
        ((), "pause_id", "A" * 32),
        ((), "pause_id", "a" * 31),
        ((), "phase", "probe"),
        ((), "reason", "DIALOG_NEEDS_HUMAN"),
        ((), "state", "confirmed"),
        ((), "host_path", "C:\\private\\prompt.png"),
        ((), "raw_text", "PRIVATE_SYNTHETIC_PROMPT"),
        ((), "requested_action", "Continue"),
        ((), "captured_at_utc", "2026-09-18T07:00:00+02:00"),
        ((), "published_at_utc", "2026-09-17T07:00:00.000Z"),
        ((), "captured_at_utc", "2026-02-30T07:00:00.000Z"),
        ((), "model_path", "../other.SemanticModel"),
        ((), "model_path", "/absolute"),
        ((), "model_path", "C:/private/model"),
        ((), "model_path", "a\\..\\b"),
        ((), "model_path", "a//b"),
        ((), "model_path", "a/./b"),
        ((), "model_path", "a/b."),
        (("run",), "number", True),
        (("run",), "number", "1"),
        (("run",), "directory", "002-control"),
        (("desktop",), "pid", 123),
        (("desktop",), "pid", "00123"),
        (("desktop",), "process_start", "unknown"),
        (("desktop",), "pid", "4294967296"),
        (("desktop",), "dialog_hwnd", "111"),
        (("desktop",), "source", "PRIVATE_SYNTHETIC_SOURCE"),
        (("image",), "path", "../prompt.png"),
        (("image",), "path", "C:\\prompt.png"),
        (("image",), "sha256", "f" * 63),
        (("image",), "sha256", "F" * 64),
        (("image", "dimensions"), "width", True),
        (("image", "dimensions"), "height", "2"),
        (("image", "dimensions"), "width", 0),
        (("image", "dimensions"), "width", 4_000_001),
        (("image", "ownership_checks"), "before", 1),
        (("image", "ownership_checks"), "after_write", None),
    ],
)
def test_schema_rejects_wrong_type_traversal_identity_and_extra_fields(location, key, value) -> None:
    record = sample_record()
    target = record
    for part in location:
        target = target[part]
    target[key] = value
    with pytest.raises((ValueError, TypeError)):
        pause.validate_pause(record)


@pytest.mark.parametrize("key", list(sample_record()))
def test_schema_missing_fields_never_mean_good_evidence(key) -> None:
    record = sample_record()
    del record[key]
    with pytest.raises(pause.OperatorPauseUnavailable):
        pause.validate_pause(record)


@NATIVE
def test_context_is_explicit_immutable_and_creates_nothing(context) -> None:
    assert context.run_scratch.name == "scratch"
    assert context.run_number == 1 and context.run_directory == "001-control"
    assert context.process_start == pause.process_start_identity(os.getpid())
    assert not (context.run_scratch / "operator-pauses").exists()
    with pytest.raises(FrozenInstanceError):
        context.desktop_pid = 4
    pause.recheck_operator_pause(context)


@NATIVE
def test_reserved_retained_pixels_survive_handle_close_and_are_ordinary_viewer_readable(context) -> None:
    stage = pause.reserve_operator_pause(context, time.monotonic() + 8, lambda: False)
    try:
        modal._write_private_image(stage.image_path, PNG, existing=True)
        try:
            ordinary_bytes = stage.image_path.read_bytes()
        except OSError:
            ordinary_bytes = None
        assert ordinary_bytes == PNG, "retained pixels must support an ordinary reader, not a shared-delete workaround"
    finally:
        stage.lease.close()
    assert stage.image_path.is_file(), "retained pixels must survive producer handle close without delete-on-close"
    assert stage.image_path.read_bytes() == PNG


@NATIVE
@pytest.mark.parametrize(
    "change",
    [
        "relative",
        "unc",
        "device",
        "wrong-basename",
        "moved",
        "run-number",
        "run-directory",
        "run-path",
        "relative-recorded-path",
        "model-outside",
        "package-model",
        "no-manifest",
        "duplicate-key",
    ],
)
def test_canonical_binding_refusals_create_no_pause(run_paths, tmp_path, change) -> None:
    scratch, model = run_paths
    root = scratch.parent
    manifest = root / "run.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if change == "relative":
        scratch = Path("scratch")
    elif change == "unc":
        scratch = Path(r"\\invalid.invalid\share\001-control\scratch")
    elif change == "device":
        scratch = Path("\\\\?\\" + str(scratch))
    elif change == "wrong-basename":
        scratch = root / "other"
        scratch.mkdir()
    elif change == "moved":
        moved = root.with_name("002-control")
        root.rename(moved)
        scratch, model = moved / "scratch", moved / model.relative_to(root)
    elif change in {"run-number", "run-directory", "run-path", "relative-recorded-path"}:
        key, value = {
            "run-number": ("run", 2),
            "run-directory": ("allocated_dir_name", "002-control"),
            "run-path": ("allocated_abs_path", str(tmp_path)),
            "relative-recorded-path": ("allocated_abs_path", root.name),
        }[change]
        data[key] = value
        manifest.write_text(json.dumps(data), encoding="utf-8")
    elif change == "model-outside":
        model = tmp_path / "outside.SemanticModel"
        model.mkdir()
    elif change == "package-model":
        model = root / "packages" / "unit.SemanticModel"
        model.mkdir(parents=True)
    elif change == "no-manifest":
        manifest.unlink()
    else:
        manifest.write_text('{"run":1,' + json.dumps(data)[1:], encoding="utf-8")
    with pytest.raises(pause.OperatorPauseUnavailable):
        pause.prepare_operator_pause(scratch, model, os.getpid())
    assert not list(tmp_path.rglob("operator-pauses"))


@NATIVE
@pytest.mark.parametrize(
    "ancestor", ["packages", "deliverables", "fabric", "pbip", "models.SemanticModel", "unit.Report"]
)
def test_canonical_run_inside_an_artifact_is_still_refused(tmp_path, ancestor) -> None:
    paths = make_run(tmp_path / ancestor)
    with pytest.raises(pause.OperatorPauseUnavailable):
        pause.prepare_operator_pause(*paths, os.getpid())


@NATIVE
@pytest.mark.parametrize("marker", ["package-manifest.json", "definition.pbism", "definition.pbir", "unit.pbip"])
def test_renamed_artifact_ancestry_is_refused(run_paths, marker) -> None:
    scratch, model = run_paths
    (scratch.parent / marker).write_text("{}", encoding="utf-8")
    with pytest.raises(pause.OperatorPauseUnavailable):
        pause.prepare_operator_pause(scratch, model, os.getpid())


@NATIVE
def test_selected_short_and_repo_local_runs_do_not_require_a_runs_parent(tmp_path) -> None:
    for container in ("short-root", "checkout"):
        parent = tmp_path / container
        parent.mkdir()
        if container == "checkout":
            result = subprocess.run(["git", "init", "--quiet", str(parent)], capture_output=True, check=False)
            assert result.returncode == 0
            (parent / ".gitignore").write_text("/001-control/\n", encoding="utf-8")
        scratch, model = make_run(parent)
        context = pause.prepare_operator_pause(scratch, model, os.getpid())
        stage = publish(context)
        assert pause.load_operator_pause(scratch, stage.pause_id, HASH) == stage.record
        assert pause.cleanup_operator_pause(scratch, stage.pause_id, HASH) == "removed"
        if container == "checkout":
            (parent / ".gitignore").write_text("", encoding="utf-8")
            with pytest.raises(pause.OperatorPauseUnavailable):
                pause.prepare_operator_pause(scratch, model, os.getpid())


@NATIVE
def test_replaced_model_and_reused_pid_cannot_rebind_context(context, monkeypatch) -> None:
    monkeypatch.setattr(pause, "process_start_identity", lambda _pid: str(int(context.process_start) + 1))
    with pytest.raises(pause.OperatorPauseUnavailable):
        pause.recheck_operator_pause(context)
    monkeypatch.undo()
    context.model_dir.rename(context.model_dir.with_name("old.SemanticModel"))
    context.model_dir.mkdir()
    with pytest.raises(pause.OperatorPauseUnavailable):
        pause.recheck_operator_pause(context)


@NATIVE
def test_real_junction_is_refused_on_prepare_load_and_cleanup(context, tmp_path) -> None:
    stage = publish(context)
    moved = context.run_scratch.with_name("original-scratch")
    context.run_scratch.rename(moved)
    done = subprocess.run(
        [os.environ["COMSPEC"], "/c", "mklink", "/J", str(context.run_scratch), str(moved)],
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, "native junction control must be established, not silently skipped"
    try:
        with pytest.raises(pause.OperatorPauseUnavailable):
            pause.prepare_operator_pause(context.run_scratch, context.model_dir, os.getpid())
        with pytest.raises(pause.OperatorPauseUnavailable):
            pause.load_operator_pause(context.run_scratch, stage.pause_id, HASH)
        assert pause.cleanup_operator_pause(context.run_scratch, stage.pause_id, HASH) == "cannot_establish"
        assert (moved / "operator-pauses" / stage.pause_id / "prompt.png").read_bytes() == PNG
    finally:
        context.run_scratch.rmdir()  # exact junction only, never recurse into its target
        moved.rename(context.run_scratch)
    assert pause.cleanup_operator_pause(context.run_scratch, stage.pause_id, HASH) == "removed"


@NATIVE
def test_native_viewers_protected_acl_same_image_rename_and_no_expiry(context, monkeypatch) -> None:
    renamed = []
    rename = pause._rename

    def inspect_rename(source, target):
        assert not (source / "READY").exists() and not target.exists()
        before = (source / "prompt.png").stat()
        rename(source, target)
        after = (target / "prompt.png").stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino), "publication copied pixels"
        assert (target / "prompt.png").read_bytes() == PNG
        renamed.append(target)

    monkeypatch.setattr(pause, "_rename", inspect_rename)
    stage = publish(context)
    assert renamed == [stage.directory]
    assert set(entry.name for entry in stage.directory.iterdir()) == {"prompt.png", "pause.json", "READY"}
    assert (stage.directory / "READY").stat().st_size == 0
    with Image.open(stage.image_path) as image:
        image.load()
        assert image.size == (2, 2) and image.tobytes() == PIXELS
    node = subprocess.run(
        [
            "node",
            "-e",
            "const fs=require('fs'),c=require('crypto');"
            "console.log(c.createHash('sha256').update(fs.readFileSync(process.argv[1])).digest('hex'))",
            str(stage.image_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert node.returncode == 0 and node.stdout.strip() == HASH
    # Independent native ACL oracle, including directory inheritance protection and each final file.
    for path in (stage.directory.parent, stage.directory, *(stage.directory / name for name in sorted(pause._FILES))):
        quoted = str(path).replace("'", "''")
        done = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$ErrorActionPreference='Stop'; $path='{quoted}'; "
                "$a=if([System.IO.Directory]::Exists($path)){[System.IO.Directory]::GetAccessControl($path)}"
                "else{[System.IO.File]::GetAccessControl($path)}; "
                "$r=@($a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])); "
                "[ordered]@{protected=$a.AreAccessRulesProtected;sids=@($r|%{$_.IdentityReference.Value});"
                "inherited=@($r|%{$_.IsInherited})}|ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert done.returncode == 0
        assert json.loads(done.stdout) == {"protected": True, "sids": ["S-1-3-4"], "inherited": [False]}
    assert pause.load_operator_pause(context.run_scratch, stage.pause_id, HASH) == stage.record
    assert "expires_at_utc" not in stage.record and "path" not in stage.record
    assert pause.cleanup_operator_pause(context.run_scratch, stage.pause_id, HASH) == "removed"


@NATIVE
def test_existing_final_id_is_not_replaced_even_by_an_empty_directory(context, monkeypatch) -> None:
    first = publish(context)
    original = {name: (first.directory / name).read_bytes() for name in pause._FILES}
    monkeypatch.setattr(pause.uuid, "uuid4", lambda: SimpleNamespace(hex=first.pause_id))
    with pytest.raises(pause.OperatorPauseUnavailable):
        publish(context)
    assert original == {name: (first.directory / name).read_bytes() for name in pause._FILES}
    assert pause.cleanup_operator_pause(context.run_scratch, first.pause_id, HASH) == "removed"
    first.directory.mkdir()
    # A distinct nonce avoids testing the previous abandoned staging name instead of final collision.
    ids = iter([first.pause_id, "f" * 32])
    monkeypatch.setattr(pause.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    with pytest.raises(pause.OperatorPauseUnavailable):
        publish(context)
    assert first.directory.is_dir() and not list(first.directory.iterdir()), "even an empty final name is reserved"


@NATIVE
def test_two_pauses_exact_load_cleanup_lock_and_repeat(context) -> None:
    first, second = publish(context), publish(context)
    assert first.pause_id != second.pause_id
    assert pause.load_operator_pause(context.run_scratch, first.pause_id, HASH)["pause_id"] == first.pause_id
    second_bytes = {name: (second.directory / name).read_bytes() for name in pause._FILES}
    with first.image_path.open("rb"):
        assert pause.cleanup_operator_pause(context.run_scratch, first.pause_id, HASH) == "cleanup_failed"
        assert pause.load_operator_pause(context.run_scratch, first.pause_id, HASH) == first.record
    assert pause.cleanup_operator_pause(context.run_scratch, first.pause_id, "f" * 64) == "cannot_establish"
    assert first.image_path.read_bytes() == PNG
    assert pause.cleanup_operator_pause(context.run_scratch, first.pause_id, HASH) == "removed"
    assert pause.cleanup_operator_pause(context.run_scratch, first.pause_id, HASH) == "absent"
    assert second_bytes == {name: (second.directory / name).read_bytes() for name in pause._FILES}
    assert pause.cleanup_operator_pause(context.run_scratch, "../" + second.pause_id, HASH) == "cannot_establish"
    assert pause.cleanup_operator_pause(context.run_scratch, second.pause_id, HASH) == "removed"


@NATIVE
@pytest.mark.parametrize(
    "damage", ["image", "metadata", "metadata-hash", "missing-ready", "ready-bytes", "extra-file", "acl"]
)
def test_malformed_pause_is_not_loaded_or_cleaned(context, damage) -> None:
    stage = publish(context)
    if damage == "image":
        stage.image_path.write_bytes(PNG[:-1])
    elif damage == "metadata":
        (stage.directory / "pause.json").write_text('{"schema":', encoding="utf-8")
    elif damage == "metadata-hash":
        record = copy.deepcopy(stage.record)
        record["image"]["sha256"] = "f" * 64
        (stage.directory / "pause.json").write_text(json.dumps(record), encoding="utf-8")
    elif damage == "missing-ready":
        (stage.directory / "READY").unlink()
    elif damage == "ready-bytes":
        (stage.directory / "READY").write_bytes(b"not empty")
    elif damage == "extra-file":
        (stage.directory / "operator-confirmation.json").write_text("{}", encoding="utf-8")
    else:
        done = subprocess.run(["icacls", str(stage.image_path), "/inheritance:e"], capture_output=True, check=False)
        assert done.returncode == 0, "ACL mutation must actually be established"
    with pytest.raises(pause.OperatorPauseUnavailable):
        pause.load_operator_pause(context.run_scratch, stage.pause_id, HASH)
    assert pause.cleanup_operator_pause(context.run_scratch, stage.pause_id, HASH) == "cannot_establish"
    assert stage.directory.is_dir()


@NATIVE
@pytest.mark.parametrize("boundary", ["stage", "image", "metadata", "rename", "READY"])
def test_forced_producer_exit_accepts_only_ready_and_retains_original_pixels(run_paths, boundary) -> None:
    scratch, model = run_paths
    script = r"""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _operator_pause as p
import _credential_modal as m
boundary = sys.argv[4]
def stop(stage):
    print(stage.pause_id, flush=True)
    time.sleep(60)
context = p.prepare_operator_pause(Path(sys.argv[2]), Path(sys.argv[3]), os.getpid())
stage = p.reserve_operator_pause(context, time.monotonic()+8, lambda:False)
if boundary == "stage": stop(stage)
m._write_private_image(stage.image_path, bytes.fromhex(sys.argv[5]), existing=True)
if boundary == "image": stop(stage)
original_rename = p._rename
def rename(source, target):
    if boundary == "metadata": stop(stage)
    original_rename(source, target)
    if boundary == "rename": stop(stage)
p._rename = rename
stage.publish(json.loads(sys.argv[6]), time.monotonic()+8, lambda:False)
stop(stage)
"""
    with subprocess.Popen(
        [
            sys._base_executable,
            "-c",
            script,
            str(Path(pause.__file__).parent),
            str(scratch),
            str(model),
            boundary,
            PNG.hex(),
            json.dumps(acquisition()),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        watchdog = threading.Timer(12, child.kill)
        watchdog.start()
        try:
            pause_id = child.stdout.readline().strip()
            assert len(pause_id) == 32, f"producer failed before the kill boundary: {child.communicate()[1]}"
        finally:
            watchdog.cancel()
        child.kill()
        child.wait(timeout=5)
    if boundary == "READY":
        record = pause.load_operator_pause(scratch, pause_id, HASH)
        assert record["desktop"]["pid"] == str(child.pid)
        with Image.open(scratch / "operator-pauses" / pause_id / "prompt.png") as image:
            assert image.tobytes() == PIXELS
        assert pause.cleanup_operator_pause(scratch, pause_id, HASH) == "removed"
    else:
        with pytest.raises(pause.OperatorPauseUnavailable):
            pause.load_operator_pause(scratch, pause_id, HASH)


@NATIVE
@pytest.mark.parametrize("boundary", ["reserve", "metadata", "rename", "ready-open", "ready-commit"])
@pytest.mark.parametrize("limit", ["closed", "deadline"])
def test_original_deadline_or_closed_state_cannot_publish_a_late_ready(context, monkeypatch, boundary, limit) -> None:
    clock = {"expired": False}
    monotonic = time.monotonic
    if limit == "deadline":
        monkeypatch.setattr(pause.time, "monotonic", lambda: monotonic() + (60 if clock["expired"] else 0))
    deadline = time.monotonic() + 8

    def closed():
        return limit == "closed" and clock["expired"]

    stage = pause.reserve_operator_pause(context, deadline, closed)
    modal._write_private_image(stage.image_path, PNG, existing=True)
    original_open, original_rename, original_disposition = pause._open_file, pause._rename, pause._disposition

    def late_open(path, **kwargs):
        stream = original_open(path, **kwargs)
        if (boundary == "metadata" and path.name == "pause.json") or (
            boundary == "ready-open" and path.name == "READY"
        ):
            clock["expired"] = True
        return stream

    def late_rename(source, target):
        original_rename(source, target)
        if boundary == "rename":
            clock["expired"] = True

    def late_commit(stream, flags, **kwargs):
        original_disposition(stream, flags, **kwargs)
        if boundary == "ready-commit" and flags == 8:
            clock["expired"] = True

    monkeypatch.setattr(pause, "_open_file", late_open)
    monkeypatch.setattr(pause, "_rename", late_rename)
    monkeypatch.setattr(pause, "_disposition", late_commit)
    clock["expired"] = boundary == "reserve"
    try:
        with pytest.raises(pause.OperatorPauseUnavailable):
            stage.publish(acquisition(), deadline, closed)
    finally:
        stage.lease.close()
    assert not (stage.directory / "READY").exists(), "late publication must leave no accepted READY marker"
    with pytest.raises(pause.OperatorPauseUnavailable):
        pause.load_operator_pause(context.run_scratch, stage.pause_id, HASH)
