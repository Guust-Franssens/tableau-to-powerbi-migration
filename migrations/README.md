# `migrations/` — your migrations go here

**Both folders start empty.** This repo's 16 worked examples live in [`../examples/`](../examples/),
kept separate so our demo content never mixes with real customer work.

## Which folder?

It depends on what the Tableau source *produces* in Power BI:

| You have | Put it in | It produces |
|---|---|---|
| A workbook (`.twb` / `.twbx`) | [`workbooks/<slug>/`](workbooks/) | a semantic model **+ a report** |
| A published data source (`.tds` / `.tdsx`) | [`datasources/<slug>/`](datasources/) | a semantic model, **no report** |

Not sure whether your workbook uses a published data source? You don't have to guess — parse it and
the spec will tell you:

```
python scripts/parse_tableau.py <workbook>.twbx -o <spec>.json
python scripts/published_datasource_registry.py --spec <spec>.json
```

Tableau **embeds** data sources by default, so most workbooks have none and go straight into
`workbooks/`. A published one shows up as `connection class='sqlproxy'`.

## Do data sources first

When a workbook does use a published data source, migrate the **data source first**, then point the
workbook's report at the model it produced. One published data source is often shared by many
workbooks, so rebuilding it per workbook creates copies that drift apart.

`scripts/tableau_lineage.py --plan` orders a whole Tableau estate by exactly this leverage (which
data sources unblock the most workbooks). The registry then stops the duplicate work: it tracks who
owns each shared model, and reports `ALREADY MIGRATED` with the concrete binding to use.

Full per-folder instructions: [`workbooks/README.md`](workbooks/README.md) ·
[`datasources/README.md`](datasources/README.md)
