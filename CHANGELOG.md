# Changelog

## v1.0.0 — 2026-10-28

First public release.

- DOI / arXiv ID / exact-title resolution (arXiv IDs map to DataCite DOIs).
- Backward references, forward citations, and neighbour-to-neighbour citation edges.
- Six weighted signals: text similarity, coupling + co-citation, influence, velocity, recency, venue.
- Co-citation clustering with automatic modularity resolution and singleton text assignment.
- Deterministic force layout, single-file offline HTML report (graph + ranked list + details + narrative).
- `lint`: citation closure, numeric provenance, forbidden phrasing, cluster coverage, direction check.
- Preprint/journal deduplication by normalised title.
- Free mode (single-entity lookups) for keyless, zero-cost runs.
- Offline `selftest`, `doctor`, and `export-dist` with a privacy scan (exit code 4 on leaks).
- Machine-readable output: `--json` for `resolve` / `pack` / `lint`, plus `describe` manifest.
