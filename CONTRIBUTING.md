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

### Where code lives (and the one exception)

Shared tooling lives in `scripts/`, its tests in `tests/`. **Exception: a skill bundle owns its own
code.** A skill under `.github/skills/<name>/` may ship `scripts/` and `tests/` next to its
`SKILL.md`, because the whole folder is the unit that gets copied into another migration repo (or
promoted to a global skill location) — splitting it across `scripts/` and `tests/` is what made the
"just copy it" claim untrue. Same conventions apply inside the bundle (`purpose:`/`usage:` headers,
10.00/10 pylint), and the same commands, with the extra paths:

```bash
ruff format --check scripts tests .github/skills
ruff check scripts tests .github/skills
pylint scripts                                          # one invocation PER bundle, never combined:
pylint .github/skills/pbip-model-refresh/scripts        # a shim and its bundled script share a
pylint .github/skills/powerbi-ai-readiness/scripts      # module name, so a combined run resolves
                                                        # the import to the shim (false E0611)
```

`scripts/probe_desktop_query.py`, `scripts/refresh_pbip_model.py`, `scripts/set_ai_instructions.py`
and `scripts/check_ai_readiness.py` are **forwarding shims** that `runpy` the bundled copies, so the
existing `python scripts/…` invocations in the personas keep working. Delete them once every caller
points at the skill path.

A bundled script must not assume its depth below the repo root — `parents[N]` resolves to the skill
folder, where every glob silently matches nothing. Walk up for a known migration tree instead (see
`host_root()` in `powerbi-ai-readiness`), and let host-repo fixtures like `examples/` **skip** with a
reason when absent rather than fail.

## Tests

The deterministic parser has a `pytest` regression suite:

```bash
pytest -q                    # currently 188 tests
```

`testpaths` in `pyproject.toml` covers both roots (`tests` **and** `.github/skills`) — pytest skips
dot-directories by default, so a skill's bundled tests would otherwise be collected by nobody.

If you change `scripts/parse_tableau.py` or `docs/migration-spec.schema.json`, add or update a test in
`tests/`. The parser must always emit a `migration-spec.json` that validates against the schema.

## Anatomy of a migration

Each workbook migration lives under `migrations/workbooks/<slug>/` (data-source migrations under `migrations/datasources/<slug>/`; our worked examples under `examples/<slug>/` — all three share this shape):

```
migrations/workbooks/<slug>/
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
- Keep shared tooling customer-agnostic (customer context stays inside `migrations/workbooks/<slug>/` or `migrations/datasources/<slug>/`).

## Commits & branches

- Branch names: `feat/<topic>`, `fix/<topic>`, `chore/<topic>`. Keep PRs focused — one concern each.
- Commits made with Copilot include the trailer:
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```
- Never rewrite pushed history on shared branches without agreement.

By contributing, you agree your contributions are licensed under the repository's [LICENSE](LICENSE).
