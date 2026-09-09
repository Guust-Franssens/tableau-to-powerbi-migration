# Capture receipt — Power BI page capture evidence

Slice A of #363.  The capture receipt is **generated evidence** recording what was captured, when,
and how — it is never an agent-authored verdict.

## Location

```
<package>/validation/iterations/<NNN>/
    capture.json          <- the receipt
    pages/<page-id>.png   <- one screenshot per captured page
```

Iteration numbers are three-digit zero-padded (``001``, ``002``, …), atomically allocated, and
**never reused or overwritten**.  A non-canonical name (e.g. ``1`` beside ``001``) blocks further
allocation until removed.

## Receipt schema (v1.0.0)

``capture.json`` is a strict JSON document.  No additional properties are permitted at any level.

### Top-level fields

| Field | Type | Description |
|---|---|---|
| ``$schema`` | string | Schema identifier |
| ``version`` | string | Schema version |
| ``iteration_id`` | string | The zero-padded iteration number |
| ``mode`` | string | ``"sign-off"`` (all pages) or ``"triage"`` (subset) |
| ``scope`` | string | ``"all-pages"`` or ``"subset"`` |
| ``package_root`` | string | Resolved absolute path to the package |
| ``pbip_path`` | string | Resolved absolute path to the ``.pbip`` file |
| ``pbip_sha256`` | string | SHA-256 of the ``.pbip`` at capture time |
| ``report_folder`` | string | Resolved absolute path to the ``.Report`` folder |
| ``definition_pbir_sha256`` | string | SHA-256 of ``definition.pbir`` |
| ``current_file_path`` | string | Exact ``currentFilePath`` from the Desktop bridge status |
| ``tool_version`` | string | Version of the capture receipt schema code |
| ``timestamp`` | string | ISO 8601 UTC timestamp of the capture |
| ``stable_dwell`` | object | ``{stable_seconds, poll_seconds, max_wait_seconds}`` |
| ``pages`` | array | Per-page capture evidence |

### Per-page fields

| Field | Type | Description |
|---|---|---|
| ``page_id`` | string | PBIR page folder name |
| ``display_name`` | string | The page's ``displayName`` from ``page.json`` |
| ``visual_ids`` | array of string | Every visual ID on this page at capture time |
| ``screenshot.relative_path`` | string | Iteration-relative path to the PNG |
| ``screenshot.sha256`` | string | SHA-256 of the screenshot bytes |
| ``screenshot.bytes`` | integer | File size in bytes |
| ``convergence.converged`` | boolean | Whether the render stabilised |
| ``convergence.frames`` | integer | Number of frames captured |
| ``convergence.elapsed_seconds`` | number | Wall-clock seconds for this page |

## Integrity properties

1. **Atomic failure.** A failed, zero-byte, or unconverged page capture removes the entire
   iteration directory — no partial receipts exist.
2. **Sign-off requires all pages.** A subset capture is explicitly labeled ``"triage"`` and cannot
   claim ``"sign-off"``.
3. **Bridge identity verified.** The receipt records the exact ``currentFilePath`` from the
   PID-scoped Desktop bridge status, matched against the canonical ``.pbip``.
4. **Content digests.** The ``.pbip`` and ``definition.pbir`` SHA-256 hashes pin the artifacts that
   were open at capture time.
5. **Path containment.** Screenshot paths are iteration-relative with no ``..`` or absolute
   components.

## Usage

```
python scripts/capture_powerbi_pages.py --package <package-root> --pid <desktop-pid>
```

Legacy mode (no receipt, no iteration numbering) remains available:

```
python scripts/capture_powerbi_pages.py <report-folder> <output-dir> --pid <desktop-pid>
```

## What this does NOT cover (Slices B and C)

- **Comparison artifacts** (Slice B): per-page/per-visual comparison against Tableau evidence.
- **check_unit integration** (Slice C): upgrading ``CLAIMED_ONLY`` gates to use this evidence.
- **Promotion** (Slice C): the ``publish_capture.py`` / ``published.json`` projection.

These remain required before #363 can close.
