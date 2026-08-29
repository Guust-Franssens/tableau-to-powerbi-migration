# Windows path limits, and what they cost a shipped bundle

> **Headline, measured 2026-08-29: Power BI Desktop is NOT long-path aware.** A bundle whose deepest
> file exceeds **259 characters** cannot be opened, on any Windows machine, regardless of the
> `LongPathsEnabled` registry setting. Issue
> [#235](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/235) left this open as
> "the highest-value unknown currently open on the toolkit", because answering it needs a Desktop
> launch. This page is the answer and the experiment.

Run the check:

```
python scripts/check_path_ceiling.py <bundle> [--json report.json] [--min-root-budget 60]
```

Exit `0` clean · `1` findings · `2` usage · `3` could not evaluate.

---

## 1. Why this was invisible

Windows enforces `MAX_PATH = 260` unless **both** of the following hold:

1. `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem` → `LongPathsEnabled = 1` (**default is 0**), and
2. the process's application manifest declares `<longPathAware>true</longPathAware>`.

A process can also bypass both by prefixing paths with `\\?\`.

Python 3.6+ ships the manifest declaration. So on a build machine with the registry opt-in, our
generator writes paths that a stock customer machine — and, as it turns out, Power BI Desktop *on any
machine* — cannot read. Nothing in the chain warns.

| machine | `LongPathsEnabled` | files | over 260 | longest |
|---|---:|---:|---:|---:|
| our build machine (issue #235) | **1** (non-default) | — | 93 | 269 |
| customer SES machine A | 0 | 1,408 | 0 | 258 |
| customer SES machine B | 0 | 4,458 | 0 | 248 |
| `_runs/coldrun-2.339.0-20260829/bundle` (2026-08-29) | 1 | 73 | 0 | 251 |

We were over the limit and protected. Both customer machines were under it and unprotected, by 2 and
12 characters.

---

## 2. Experiment A — the embedded application manifest (static, machine-independent)

A binary's long-path awareness lives in its `RT_MANIFEST` resource, which is baked into the shipped
`.exe`. Extract it with the Win32 resource API:

```python
import ctypes
from ctypes import wintypes as w

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.LoadLibraryExW.restype = ctypes.c_void_p
k.LoadLibraryExW.argtypes = [w.LPCWSTR, w.HANDLE, w.DWORD]
k.FindResourceW.restype = ctypes.c_void_p
k.FindResourceW.argtypes = [ctypes.c_void_p] * 3
k.SizeofResource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k.LoadResource.restype = ctypes.c_void_p
k.LoadResource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k.LockResource.restype = ctypes.c_void_p
k.LockResource.argtypes = [ctypes.c_void_p]

h = k.LoadLibraryExW(exe_path, None, 0x2 | 0x20)   # AS_DATAFILE | AS_IMAGE_RESOURCE
res = k.FindResourceW(ctypes.c_void_p(h), ctypes.c_void_p(1), ctypes.c_void_p(24))  # id 1, RT_MANIFEST
size = k.SizeofResource(ctypes.c_void_p(h), ctypes.c_void_p(res))
ptr = k.LockResource(ctypes.c_void_p(k.LoadResource(ctypes.c_void_p(h), ctypes.c_void_p(res))))
print("longPathAware" in ctypes.string_at(ptr, size).decode("utf-8", "replace"))
```

Results against Power BI Desktop **2.157.828.0** (Store package, `.../bin/`):

| binary | role | `longPathAware` |
|---|---|---|
| `PBIDesktop.exe` | the application | **absent** (manifest id 1, 1929 bytes: `dependency` + `compatibility` only, no `windowsSettings`) |
| `msmdsrv.exe` | Analysis Services engine that loads the model | **absent** |
| `Microsoft.Mashup.Container.NetFX45.exe` | M engine that reads source files | **absent** |
| `python.exe` | **control (positive)** | present |
| `explorer.exe` | **control (negative)** | absent |

### The static result alone is NOT conclusive — and knowing why matters

A program without the manifest declaration can still handle long paths by prefixing `\\?\` itself.
Measured here, on this machine:

| binary | manifest says | actually read a 262-char path? |
|---|---|---|
| `node.exe` | `longPathAware` absent | **yes** — libuv prefixes `\\?\` internally |
| `pwsh.exe` | `longPathAware` absent | **yes** — .NET Core prefixes in `PathInternal` |
| `cmd.exe`, `powershell.exe`, `notepad.exe` | present | yes |
| `robocopy.exe` | absent | (not probed) |

That is why issue #235's earlier probe found *every* consumer succeeding at 460 characters and
concluded "the masking is total": several of those tools were never depending on the registry at all.
So the manifest was treated here as a **hypothesis**, and a live A/B was run to settle it.

---

## 3. Experiment B — the live A/B in Power BI Desktop (conclusive)

Two **byte-identical** copies of `examples/shipping-kpis/fabric` (27 files), differing only in the
length of their root, opened on this machine with `LongPathsEnabled = 1`:

| copy | longest path | `.pbip` entry point | result at t+190 s |
|---|---:|---:|---|
| control | 159 | 78 | **opens** — window title resolves to `ShippingKPIs` |
| long | 268 | 187 | **fails** — title stuck at `Untitled - Power BI Desktop` |

Both entry-point `.pbip` paths were far below any limit, so the failure is attributable to a deep
child file and nothing else. The long copy raised a modal dialog, **"Issues were found"**:

> Cannot read `'<root>\ShippingKPIs.Report\definition\pages\51c062066e7c504dcbb5\visuals\2069ecf9b8b2e1212571\visual.json'`.
> The specified path, file name, or both are too long. **The fully qualified file name must be less
> than 260 characters, and the directory name must be less than 248 characters.**

That is a `System.IO.PathTooLongException`. The control proves the harness works; the A/B proves the
cause is length.

### Reproducing it

```powershell
# 1. control + long copies (Python is long-path aware, so it can WRITE the long one)
#    place the long copy so its deepest file lands ~268 chars, e.g. a 169-char root
#    ("examples/shipping-kpis/fabric" has a 99-char deepest tail)

# 2. launch each, naming the PID
$p = Start-Process -FilePath $env:PBI_DESKTOP_PATH -ArgumentList "`"<path>\ShippingKPIs.pbip`"" -PassThru
$p.Id

# 3. wait >= 90 s. MainWindowTitle is a LOADING STATE before it is a verdict.
Get-Process -Id <literal pid> | Select-Object Id, MainWindowTitle

# 4. clean up with a LITERAL pid
Stop-Process -Id <literal pid> -Force
```

> ⚠️ Deleting the long fixture afterwards needs the `\\?\` prefix
> (`shutil.rmtree("\\\\?\\" + str(root))` from Python); Explorer and many shells cannot remove it.

---

## 4. The ceilings this repo enforces

Taken verbatim from Desktop's own error text, not from folklore:

| constant | value | Desktop's wording |
|---|---:|---|
| `FILE_CEILING` | **259** | "fully qualified file name must be **less than 260** characters" |
| `DIR_CEILING` | **247** | "the directory name must be **less than 248** characters" |

Both are enforced by `scripts/check_path_ceiling.py`.

**Why the directory rule earns its place.** In a PBIR tree the deepest file is `visual.json`, whose
`\visual.json` tail is exactly 12 characters — and `260 - 248` is also exactly 12. For that file the
two rules bite at the same point, so including the directory rule adds no false-alarm surface. It
only becomes the *stricter* of the two for shorter names (`page.json`, `.platform`) — precisely the
case a file-only check would miss. Measured on the long fixture above, the check reported **24**
paths over ceiling, and the `visuals\<id>` **directories** were among them.

**282 is not encoded.** It remains an unreproduced archive anecdote (`docs/deterministic-tier-integration.md:414`).
Pass `--ceiling 282` explicitly if you want to test that budget.

---

## 5. The portable number: tail length and root budget

"93 files over 260" is a fact about where a bundle happens to sit on *one* disk. The customer will
put it somewhere else. The number that survives relocation is the longest **tail** — the path
relative to the bundle root — and the budget it leaves:

```
root_budget = 259 - longest_tail
```

That is the longest install root the bundle tolerates before Desktop refuses it. A customer unpacking
to `C:\Users\<name>\Documents\migrations\` consumes ~40 characters before the bundle contributes
anything.

Measured on `_runs/coldrun-2.339.0-20260829/bundle` (2026-08-29):

```
measured   : 114 paths (73 files, 41 directories)
longest    : 251 chars - .../pbip/Meridian Revenue by Region/Meridian Revenue by Region.Report/
                          definition/pages/page-ws-Revenuebb7d27f78/visuals/v-RevenuebyRegio864a62f6/visual.json
longest tail: 153 chars  ->  root budget 106 chars
near ceiling (> 240): 3 paths
```

Clean, with 8 characters of absolute headroom — and the tail is driven by a **26-character workbook
name appearing twice** (`Meridian Revenue by Region\Meridian Revenue by Region.Report`, 59 chars).
A 70-character customer workbook name adds ~88 characters to that tail and breaches the ceiling
**regardless of any directory convention we adopt**. Our own prefix is ~12 characters, so shortening
it cannot rescue a long workbook name. That doubled segment is upstream
(`Yarbrdab000/tableau-fabric-skills`) and is the single largest lever available.

`--min-root-budget N` turns the budget into a gate. It is opt-in because the reasonable value depends
on where the customer unpacks.

---

## 6. What the check deliberately does NOT do

* **It never asks the OS whether a path can be opened.** The verdict is computed arithmetically from
  path strings, so there is no code path by which the host's registry setting — or the host's
  operating system — can soften it. Linux CI produces the same numbers for the same tree. The registry
  value *is* read and printed, because #235 exists entirely because nobody printed it, but it is
  context and never an input.
* **A path it cannot measure is `unknown`, never passing.** Unreadable directories are counted, named,
  and force a non-zero exit.
* **An empty target is `no_paths`, not `ok`.** Nothing to measure is not the same as nothing wrong.

---

## 7. What is still unverified

* **The directory rule (247) was not isolated experimentally.** In a real PBIR tree you cannot
  construct a case where a directory violates 247 while every file stays within 259 — the layout's
  12-character `\visual.json` tail makes the two rules coincide. The value is taken from Desktop's own
  stated rule, and an artificial fixture that separated them would not generalise.
* **The exact failure boundary was not bisected.** We measured 159 → opens and 268 → fails, and took
  259/247 from the implementation's own error message rather than by binary search. The message is
  arguably stronger evidence than a bisect, but it is not the same evidence.
* **Only Desktop 2.157.828.0 (Store package) was tested.** An MSI install, or a future build, could
  differ; the manifest probe in §2 re-answers it in seconds.
* **Whether the archive/ship step has its own lower budget.** 282 remains one anecdote.
* **Whether Desktop fails the same way on a machine with `LongPathsEnabled = 0`.** It cannot be
  *better* there — the registry opt-in is a prerequisite, not a hindrance — but it was not measured.

---

## 8. Proposed follow-up (not done in this pass)

`scripts/preflight.ps1` has **zero** long-path checks. It should report the pair — the host's
`LongPathsEnabled` **and** the longest generated path — because the interaction is the risk and
either number alone is harmless. That file is out of scope here; see the pull request for #235.
