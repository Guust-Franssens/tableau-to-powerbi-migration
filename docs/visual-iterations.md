# Visual iteration lifecycle

How Power BI capture evidence is produced, verified, and gated — from the first
screenshot to the sign-off that `check_unit.py` enforces.

## Layout

```text
<package>/validation/iterations/
  001/
    capture.json            ← written by capture_powerbi_pages.py
    pages/<page>.png        ← stable screenshots
    comparison/pages/<page-id>.json  ← pending templates, then validator edits
  002/
    ...
```

Each numbered directory is an **immutable iteration**. The number is its
identity; it is never reused, never renamed. `capture_powerbi_pages.py` allocates
the next number atomically (same `mkdir(exist_ok=False)` primitive as
`work_dirs.allocate_run`).

## Artifacts

### `capture.json`

Written by `capture_powerbi_pages.py --package <package>`. Records:

- `version`, `timestamp`, `iteration`, `report` (the `.Report` path)
- `all_converged` (bool: did every page pass the render-stability check?)
- Per-page: `display_name`, `screenshot` (relative path), `sha256`, `converged`,
  `frames` (how many frames were captured), `seconds` (wall-clock time)

The per-page settle evidence is the same data the script already computes
internally (the whole reason it exists: the same map captured immediately showed
411 distinct colours vs 41,185 once settled). Previously this was discarded.

### `comparison/pages/<page-id>.json`

Generated as a **pending template** by `capture_powerbi_pages.py` alongside the
capture. The template contains:

- `page_id`, `display_name`, `mode` (initially `"pending"`)
- `capture_sha256` (pins the comparison to a specific capture)
- `visuals`: a map of visual ID → `{status, finding, disposition}`, all initially
  `"pending"`

The **validator** edits only the constrained judgement fields: `mode` (to
`"sign-off"`, `"triage"`, or `"spot-check"`), and each visual's `status`
(`"no_discrepancy"`, `"finding"`, `"accepted_limitation"`), `finding`, and
`disposition`.

## The gate checks

`check_unit.py` verifies the latest iteration and replaces three former
`CLAIMED_ONLY` rows:

| Old ID | New ID | What it checks |
|---|---|---|
| `visual-layer-done` | `visual-capture` | capture.json exists, every screenshot exists with matching hash, all pages converged |
| `visual-comparison-done` | `visual-comparison` | comparison file exists for every page, mode is not pending, capture hash matches, every visual resolved |
| `finalized` | `sign-off` | both capture and comparison are complete; the combination constitutes sign-off |

All three are now **blocking**: a NOT_CHECKED result blocks `check_unit.py` exit 0
(previously CLAIMED_ONLY was non-blocking).

## The iteration loop

1. Builder fixes the report
2. `capture_powerbi_pages.py --package <package> ...` allocates the next iteration,
   captures all pages, writes `capture.json` and pending comparison templates
3. Validator edits comparison files with findings/dispositions
4. `check_unit.py` verifies the latest iteration
5. If findings remain → go to step 1
6. When all three gates pass → `promote_unit.py` can ship

## What lives where

| Content | Location | Prunable? |
|---|---|---|
| Iteration capture + comparison evidence | `<package>/validation/iterations/` | Travels with the package |
| Disposable probe scripts | `<run>/scratch/` | Yes (`scratch/` is the only subdir a future `--prune` may delete) |
| Replay scripts | `<package>/_build/` | Tracked, not prunable |
| Bundle engine output | `<run>/bundle/` | Not prunable (embeds absolute self-paths) |
