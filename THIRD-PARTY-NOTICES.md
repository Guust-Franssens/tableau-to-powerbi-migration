# THIRD-PARTY NOTICES

This repository is licensed under [MIT](LICENSE), but that license applies only to original code and documentation that contributors are authorized to license in this repository. It does **not** automatically relicense third-party materials.

Public availability is not the same as public-domain status. Attribution alone does not grant reuse rights. Rights holders retain their rights, and users are responsible for obtaining any permissions required for their use.

## Known third-party material and references

| Topic | Where it appears in this repo | Notice |
|---|---|---|
| Tableau Public dashboards, plus source-reference screenshots | `migrations/*/reference/`, `docs/showcase/assets/`, `docs/showcase/README*.md` | Tableau Public dashboard authors retain rights in their dashboards and visual content. Keep attribution and link back to the source dashboard. Inclusion here does not imply endorsement by source authors. |
| Product/company names, logos, and trademarks (including Microsoft, Microsoft Fabric, Power BI, GitHub, GitHub Copilot, Tableau) | `README.md`, `docs/`, badges/images, examples | Names, logos, and trademarks belong to their respective owners and are used descriptively. This project is unofficial and unaffiliated unless explicitly stated otherwise in writing. |
| Microsoft Fabric / Power BI skills, plugins, MCP servers, and service endpoints (for example `microsoft/skills-for-fabric`, `@microsoft/powerbi-modeling-mcp`, `https://api.fabric.microsoft.com/v1/mcp/powerbi`) | `AGENTS.md`, `.vscode/mcp.json`, `.github/agents/`, docs/scripts references | These are external dependencies/services, not part of this repository's license grant. Their use is subject to their own licenses, terms, policies, and availability. |
| Python/npm/runtime dependencies and generated artifacts/templates | `pyproject.toml`, scripts, generated outputs under `migrations/*/fabric/` and related tooling | Dependencies and generated artifacts may have separate copyright, notice, and license obligations. Review upstream package licenses and notices before redistribution or commercial use. |

## Provenance and source links

For the authoritative list of Tableau Public source URLs used in this repository's migration examples, see [`migrations/README.md`](migrations/README.md).

This repository does not intentionally redistribute source `.twb` / `.twbx` files or extracted source data. Those artifacts are expected to remain local/ignored; committed examples focus on generated Power BI outputs and reference screenshots.

## Content-removal and rights-holder contact

If you are a rights holder and believe content here should be removed or updated, please use a **private** reporting path (do not post sensitive details publicly):

- Open a private report via [GitHub Security Advisories](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/security/advisories/new), or
- Use the private maintainer contact route described in [`SECURITY.md`](SECURITY.md).

Please include the file path(s), a description of the concern, and your requested action so maintainers can review promptly.
