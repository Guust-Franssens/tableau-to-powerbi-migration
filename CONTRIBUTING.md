# Contributing

Thanks for your interest. This is a proof-of-concept toolkit that migrates Tableau workbooks to
Microsoft Fabric Power BI using GitHub Copilot CLI custom agents plus a deterministic Python parser.
Contributions that make migrations more faithful, more honest about their limitations, or easier to
reproduce are very welcome.

Please read [`SECURITY.md`](SECURITY.md) first — the single most important rule is **never commit
customer data** (source workbooks, extracted data, or reference screenshots are all git-ignored for
this reason).

## Environment setup

The agent/skill/MCP dependencies are described in [`AGENTS.md`](AGENTS.md) and verified by the
preflight script (run it after cloning to see what's missing — it works even before Python is
installed):

```bash
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1
```

Python tooling uses [`uv`](https://github.com/astral-sh/uv) (never bare `pip`/`venv`/`conda`):

```bash
uv venv
uv sync                      # install parser + tooling deps from pyproject.toml
```

## Python conventions (enforced)

After editing any `.py`/`.ipynb`, run the ritual on the changed file(s) before opening a PR:

```bash
ruff format <path>           # format
ruff check <path> --fix      # lint + autofix
pylint <path>                # must meet fail-under = 10 (config in pyproject.toml)
```

- Python 3.11+, PEP 604 unions (`int | None`, not `Optional[int]`), `pathlib`, f-strings, `logging`,
  `argparse`. Every script in `scripts/` starts with a `purpose:` / `usage:` header docstring.
- Ruff replaces black/isort/flake8/pyupgrade — do not add those.

## Tests

The deterministic parser has a `pytest` regression suite:

```bash
pytest -q                    # currently 20 tests
```

If you change `scripts/parse_tableau.py` or `docs/migration-spec.schema.json`, add or update a test in
`tests/`. The parser must always emit a `migration-spec.json` that validates against the schema.

## Anatomy of a migration

Each migration lives under `migrations/<slug>/`:

```
migrations/<slug>/
├── source/              # .twb/.twbx  (git-ignored — may contain customer data)
├── data/                # extracted CSVs  (git-ignored)
├── reference/           # Tableau screenshots + manifest  (git-ignored — see SECURITY.md)
├── migration-spec.json  # the shareable parser contract (structure only)
└── fabric/
    ├── <Name>.SemanticModel/   # TMDL (committed)
    └── <Name>.Report/          # PBIR (committed)
```

Only the `migration-spec.json` and the `fabric/` TMDL/PBIR are committed. To add a new migration, drive
the `tableau-migrator` agent (`/agent tableau-migrator`) end-to-end, or run the stages manually
(`scripts/parse_tableau.py` → `pbi-semantic-builder` → `pbi-report-builder` → `pbi-migration-validator`).

Keep every capability/mapping/number claim backed by evidence (a spec field, a TMDL/PBIR path, a live
`EVALUATE`, or a doc URL), and record anything the pipeline couldn't reproduce in the spec's
`limitations_encountered` array — that honesty is the point of the toolkit.

## Before you open a PR

- Run the Python ritual and `pytest -q`; both must be clean.
- Sanitize machine-specific model paths and confirm the gate passes:
  ```bash
  python scripts/set_data_folder.py --sanitize
  python scripts/set_data_folder.py --check
  ```
- Confirm no source workbook, extracted data, secret, or customer-identifiable screenshot is staged.
- Keep shared tooling customer-agnostic (customer context stays inside `migrations/<slug>/`).

## Commits & branches

- Branch names: `feat/<topic>`, `fix/<topic>`, `chore/<topic>`. Keep PRs focused — one concern each.
- Commits made with Copilot include the trailer:
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```
- Never rewrite pushed history on shared branches without agreement.

By contributing, you agree your contributions are licensed under the repository's [LICENSE](LICENSE).
