# Copilot instructions, tableau-to-pbi-migration

> This file exists so agent runtimes that auto-load **`.github/copilot-instructions.md`** (VS Code
> Copilot) pick up the same conventions as runtimes that auto-load **`/AGENTS.md`** (Copilot CLI).
> **`/AGENTS.md` at the repo root is the source of truth — read it.** Only the session-start step is
> duplicated here, because it has to fire before anything else.

## Session start, do this first (before any other work)

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1 -Update -CheckUpstream
```

**Only after an actual unsigned/ExecutionPolicy startup refusal**, follow
[preflight cannot start](../docs/operator-runbook.md#preflight-cannot-start).
If recovery is allowed, retry the **exact originating command and arguments**; this session-start
call retains `-Update -CheckUpstream`. Do not run policy diagnostics first.

Nonzero preflight blocks agent/Desktop work; use its repair hints.
Migration-start preflight is **plain**; no tooling upgrades mid-migration.
Setup details live in [runbook §1.1](../docs/operator-runbook.md#11-tooling).

## Everything else

Read [`/AGENTS.md`](../AGENTS.md): front door → one intake → private v2 brief → task-tool
dispatch of `@tableau-migrator` → independent Tableau comparison before done.
Contributor process lives in [CONTRIBUTING.md](../CONTRIBUTING.md); navigate with
[docs/INDEX.md](../docs/INDEX.md).
