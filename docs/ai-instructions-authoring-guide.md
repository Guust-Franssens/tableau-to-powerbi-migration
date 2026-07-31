# Writing good AI instructions for a migrated semantic model

> **Moved.** This guidance now lives in the repo-local skill
> [`.github/skills/powerbi-ai-readiness/SKILL.md`](../.github/skills/powerbi-ai-readiness/SKILL.md),
> which is the single source of truth. This stub stays so existing links keep working.

**Why it moved:** none of it is Tableau-specific. The mechanism (`cultureInfo <lcid>` →
`linguisticMetadata` → `CustomInstructions`, plus `settings.qnaEnabled` in `definition.pbism`) is pure
Power BI, and the writing advice applies to any BI-to-Power-BI migration — Qlik, Cognos, or a model
built from scratch. Keeping it here *and* in the `pbi-semantic-builder` persona meant two copies that
had already diverged: one listed the "Verified headline numbers" section, the other did not.
`tests/test_skills.py` now fails if the section template is restated outside the skill.

The skill covers:

1. The five file-committable levers — descriptions → enumerated domains → synonyms →
   `CustomInstructions` → `qnaEnabled` — and the ⏸️ list of what is *not* committable today.
2. The storage mechanism, and why the TMDL is edited directly (the Modeling-MCP culture `Update`
   surface cannot reach `CustomInstructions`, and a direct edit avoids an XMLA refresh).
3. The commands: `set_ai_instructions.py` (stamp / lint / `--check [--strict] [--model]`) and
   `check_ai_readiness.py` (description coverage + domain enumeration).
4. **How to write them:** principles, the section template, patterns that work, anti-patterns, sources.
5. Evidence and limits — ✅ survives publish byte-for-byte, ⚠️ "Copilot obeys it" unproven, ❌ gated by
   `qnaEnabled` plus a post-deploy refresh.
6. Migration-produced idioms an agent will mishandle, with a per-source-tool row.
