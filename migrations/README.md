# `migrations/` — your workbook migrations

**This folder starts empty. It is where *your* work goes.** Our 16 worked examples live in
[`../examples/`](../examples/) so they never mix with a real customer migration.

One folder per Tableau **workbook** (`.twb`/`.twbx`), producing a Power BI **report**:

```
migrations/<slug>/
├── source/<workbook>.twbx        # gitignored - may contain customer data
├── migration-spec.json           # produced by scripts/parse_tableau.py
├── data/                         # gitignored - rows extracted from .hyper
├── reference/                    # Tableau screenshots = fidelity ground truth
└── fabric/
    ├── <Name>.Report/            # PBIR
    └── <Name>.SemanticModel/     # TMDL - unless it binds to a shared model (see below)
```

Start one with the orchestrator agent:

```
/agent tableau-migrator
```

## If the workbook uses a Tableau *published* data source

Don't build its model here. A published data source is normally shared by several workbooks, so it
gets migrated **once** into its own folder under [`../datasources/`](../datasources/), and every
report binds to that single semantic model. Check before building:

```
python scripts/published_datasource_registry.py --spec migrations/<slug>/migration-spec.json
```

Exit `0` means it already exists — bind to it instead of rebuilding:

```jsonc
// <Name>.Report/definition.pbir
{ "datasetReference": { "byPath": {
    "path": "../../../datasources/<ds-slug>/fabric/<Name>.SemanticModel" } } }
```
