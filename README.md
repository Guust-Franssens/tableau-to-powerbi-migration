# Tableau → Power BI / Fabric Migration Toolkit

For engineers migrating Tableau workbooks (`.twb` / `.twbx`) to a reviewable local
Power BI project (PBIP): a semantic model and report. Deterministic conversion and
GitHub Copilot agents help build, repair and compare the result with Tableau.

> Unofficial personal/community proof of concept, not an official Microsoft, GitHub
> or Tableau product, nor sponsored, endorsed or maintained by them. Contributors
> act in a personal capacity.

## Get started

**[Set up the toolkit and start a migration](docs/operator-runbook.md#start-here).**
The setup instructions route you to an approved live Tableau project or the
local-workbook guide. Just browsing? No installation is needed.

## Before you run

- **Environment:** Windows with Power BI Desktop, Git, GitHub Copilot CLI,
  Python 3.11+, `uv` and Node.js 20+. The conversion engine is a separately
  installed plugin; setup checks the required tools, plugins and MCP servers.
- **Expect a draft, not guaranteed fidelity.** Conversion success is not sign-off.
  Require independent comparison with Tableau, using real data for agreed numeric
  checks. Fabric deployment is a separate, explicit step.
- **Human stops remain human.** Sign-in, credentials and security/privacy approval
  dialogs need a person; agents must not bypass them.
- **Keep customer material private:** workbooks, rows, screenshots and secrets
  must not enter Git. Verify in-repo output paths are ignored; see
  [Security and privacy](SECURITY.md).

## Learn more

- [Before/after showcase](docs/showcase/README.md): public examples and their sources.
- [Capabilities and limitations](docs/capabilities-and-limitations.md): what needs verification.
- [Documentation map](docs/INDEX.md): operations, architecture and reference.
- [Contributing](CONTRIBUTING.md): development and review.

[MIT license](LICENSE).
