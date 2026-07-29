# `datasources/` — your published data source migrations

**This folder starts empty. It is where *your* work goes.**

One folder per Tableau **published data source** (`.tds`/`.tdsx`), producing a Power BI **semantic
model** — no report. Same shape as a workbook migration, minus the `.Report`:

```
datasources/<slug>/
├── source/<name>.tdsx            # gitignored - exported from Tableau Server/Cloud
├── migration-spec.json           # produced by scripts/parse_tableau.py (accepts .tds/.tdsx)
├── published-datasource.json     # declares WHICH published data source this satisfies
└── fabric/
    └── <Name>.SemanticModel/     # TMDL - the ONE model every consuming report binds to
```

## Why data sources are separate from `migrations/`

A published data source is typically consumed by **many** workbooks. If its model lived inside one
consumer's `migrations/<slug>/` folder it would look owned by whichever workbook happened to be
migrated first, and would be deleted or rebuilt with it while other reports still bind to it.
Splitting the trees by *what they produce* — semantic models here, reports in `migrations/` — keeps
the model layer independent of every report that uses it, and mirrors the Fabric convention of
separating semantic models from reports.

## Migrate the model layer FIRST

For a whole Tableau estate, ask Tableau which data sources carry the most leverage:

```
python scripts/tableau_lineage.py --plan            # needs a Tableau PAT, see the script header
python scripts/tableau_lineage.py --plan --download datasources/_downloads
```

Then, per data source:

```
python scripts/parse_tableau.py datasources/<slug>/source/<name>.tdsx \
    -o datasources/<slug>/migration-spec.json
# ... build fabric/<Name>.SemanticModel ...
python scripts/published_datasource_registry.py --register "<site>/<name>" \
    --name "<Name>" --slug <slug>
```

Registering is what lets later workbook migrations **discover** the model and bind to it instead of
rebuilding a copy that will drift.

## Why the marker file exists

A standalone `.tds` carries an **empty** `<repository-location />`, so the dedup key
(`<site>/<name>`) cannot be recovered from the file itself — `published-datasource.json` records it.
