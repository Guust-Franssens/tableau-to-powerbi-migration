# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public issue. Use
[GitHub Security Advisories](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/security/advisories/new)
for this repository, or contact the maintainer directly. You can expect an initial response within a
few business days.

## What this project is (and the data it touches)

This is an AI-assisted toolkit that migrates **Tableau workbooks** to Microsoft Fabric Power BI. The
inputs — `.twb` / `.twbx` files, their extracted data, and rendered dashboard screenshots — routinely
contain **customer or otherwise sensitive data**. The most important "security" property of this repo
is therefore **not leaking that data into version control**. The rules below are enforced by
`.gitignore` and are non-negotiable when contributing.

### Never commit source data

| Artifact | Why | How it's protected |
|---|---|---|
| Source workbooks (`migrations/*/source/*.twb`, `*.twbx`, `*.hyper`) | Can embed live/extracted customer data | git-ignored |
| Extracted data (`migrations/*/data/`) | Materialized rows from the workbook | git-ignored |
| **Reference screenshots** (`migrations/*/reference/`) | A screenshot **is a picture of the source data** — customer names, metrics, branding | git-ignored (whole bundle: images + `manifest.json` + thumbnails) |

The **shareable** artifact of a migration is its `migration-spec.json` (structure only) and the
generated TMDL/PBIR — never the raw workbook, its data, or a screenshot of it. Curated,
**customer-agnostic** before/after images for the public showcase go under `docs/showcase/` only, and
must not contain identifiable customer content.

### Never commit secrets or credentials

- No PATs, tokens, passwords, or connection strings in the repo. Read them at runtime from environment
  variables or a git-ignored `.env.local` (already ignored).
- Secrets must **never** land in `migration-spec.json` (it is the shareable artifact) — not a Tableau
  Server URL's signed image link, not a PAT name+secret. Only *intrinsic* workbook provenance belongs
  there. See [`docs/reference-capture.md`](docs/reference-capture.md) for the Tableau Server/Cloud
  credential handling design (least-privilege, POC-scoped PAT, revoke after use, hold the session token
  in memory only).

### Don't leak machine-specific local paths

A Power BI semantic model's `expressions.tmdl` carries a `DataFolder` path that is specific to the
machine that built it (e.g. an absolute `C:\Users\...` path). Before publishing or opening a pull
request, run the sanitizer and confirm the check passes:

```bash
python scripts/set_data_folder.py --sanitize    # replace absolute paths with a <REPO_ROOT> placeholder
python scripts/set_data_folder.py --check        # CI gate: exits non-zero if any absolute path remains
```

### Keep shared tooling customer-agnostic

Customer names and context belong only inside a specific `migrations/<name>/` working folder — never in
shared tooling, agent files, script identifiers, or commit messages.

## Supported versions

This is an evolving proof-of-concept toolkit; only the default branch (`master`) is maintained. There
are no long-lived release branches.

## A note on the AI agents

The migration agents (`.github/agents/*.agent.md`) can read local files, run local scripts, and call
the configured Power BI / Fabric MCP servers. Run them against workbooks and Fabric workspaces **you
are authorized to access**, review the changes they propose, and treat their output as a draft to be
validated — the whole point of this toolkit is honest human-validated migration, not blind automation.
