---
name: citation-context
description: Map a paper's citation network from a DOI, arXiv ID or exact title — backward references, forward citations, relevance ranking, co-citation clusters, a verifiable narrative and a single-file interactive HTML report. Use when asked "who cites this paper", "what does it build on", "map the citations", "find related work", or whenever a DOI is given for citation analysis.
license: MIT
---

# Citation Context

Turn one paper into a reading map: what it builds on, who built on it, which works are central,
and how those lines relate — delivered as a single, offline, clickable HTML report.

## When to use

- A DOI / arXiv ID / paper title is given and the user wants its citation lineage.
- The user asks "who cites this?", "what is this based on?", "map the related work".
- You need a defensible "prior work → method sources → follow-ups → gaps" narrative for a
  reading group, grant application or review.

## When not to use

- The user only wants to search for papers (use a search tool).
- The user wants to read a paper in depth (use a PDF/reader tool).
- Chinese-language databases (CNKI/Wanfang) — no public API.

## Quick start

```bash
python scripts/citegraph.py doctor                        # config, masked keys, cache
python scripts/citegraph.py resolve "10.1364/prj.401755"  # DOI | arXiv ID | exact title
python scripts/citegraph.py graph   --run runs/<slug>
python scripts/citegraph.py label   --run runs/<slug>
python scripts/citegraph.py score   --run runs/<slug> --explain
python scripts/citegraph.py cluster --run runs/<slug>      # optional but recommended
python scripts/citegraph.py pack    --run runs/<slug>
#   → you (the agent) write runs/<slug>/narrative.md
python scripts/citegraph.py lint    --run runs/<slug>
python scripts/citegraph.py layout  --run runs/<slug>
python scripts/citegraph.py render  --run runs/<slug>
```

## What you get

| Artifact | Meaning |
|---|---|
| `graph.json` | Nodes, edges, six weighted signals, evidence, `schema_version` |
| `clusters.json` | Co-citation clusters (id, name, members) |
| `pack.json` | The only input the narrative may use; `constraints.allowed_nids` is the legal citation set |
| `narrative.md` | Four sections written by the agent |
| `lint.json` | `ok` plus errors/warnings (citations, numbers, cluster coverage, direction) |
| `report.html` | Single file, offline, no external requests |

## Narrative rules (enforced by lint)

1. Every `[n]` must be in `pack.constraints.allowed_nids`.
2. Numbers may only come from `pack.stats` or `pack.nodes`.
3. No "proves / refutes / first / breakthrough" — use "is used for" or drop the claim.
4. §2 must cover every cluster with `size ≥ 3`.
5. No citation markers from quoted contexts; paraphrase instead.
6. If `stats.sampling.complete` is false, state "N total, M sampled".

## Configuration

`venue_weights` (journal 1.0 / conference 0.8 / book 0.7 / repository 0.35 / preprint 0.2 / unknown 0.5),
`weights` (text .42, cocite .28, influence .15, velocity .05, recency .05, venue .05), `text_scale`,
`limits.backward/forward`, `free_mode`. Change them in `config.json` or the user config; rerun `score`
only — no refetch needed.

## Security

Keys live in `~/.config/citation-context/config.json` or `OPENALEX_API_KEY`; the bundled
`config.json` ships empty. `doctor` masks keys, error URLs are sanitized, and `export-dist` refuses
to produce a package containing a key, e-mail address or home path.

## Troubleshooting (short)

- `429 budget` → wait for UTC midnight, add a key, or `graph --free-mode`.
- `lint cluster_uncovered` → a whole line is missing from §2.
- `render` refuses → scores are missing; run `label` then `score`.

## More

- [workflow](references/workflow.md) — all commands, flags, exit codes
- [schema](references/schema.md) — JSON contracts
- [security](references/security.md) — key handling, publishing checklist
- [troubleshooting](references/troubleshooting.md) — full error table

## License

MIT.
