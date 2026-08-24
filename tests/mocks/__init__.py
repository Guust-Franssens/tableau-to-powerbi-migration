"""Offline mock services for the Tableau -> Power BI/Fabric pipeline.

``fabric``  - an in-process fake of the Fabric REST API (the ``deploy_estate`` seam).
``tableau`` - a loopback HTTP fake of the Tableau REST + Metadata (GraphQL) APIs.
``estate``  - a synthetic Tableau estate built from this repo's REAL workbook fixtures, plus a
              stand-in for the deterministic engine so ``run_estate.py`` can be run offline.

See ``docs/offline-mock-harness.md`` for what is faithfully reproduced, on what evidence, and the
list of behaviours that are ASSUMED rather than measured.
"""
