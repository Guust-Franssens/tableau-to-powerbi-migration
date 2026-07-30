---
name: sentinel-probe
description: Test-only skill used to determine whether repository skills are visible to a custom-agent subagent. Contains a unique sentinel token. Not part of the migration pipeline.
---

# Sentinel probe skill

This file exists ONLY to answer one question: can a custom agent running as a subagent
see repository skills under `.github/skills/`?

The sentinel token is: SKILL_SENTINEL_ZEPHYR_74193

If an agent can quote that token, repository skills reach it.
