# Workflow reference

All commands are subcommands of `scripts/citegraph.py`. Python ≥ 3.9, standard library only.

## Command chain

```bash
python scripts/citegraph.py doctor                       # environment / key / cache check
python scripts/citegraph.py resolve "10.1364/prj.401755" # DOI | arXiv ID | exact title → seed.json
python scripts/citegraph.py graph   --run RUN            # backward + forward + neighbour citations
python scripts/citegraph.py label   --run RUN            # rule-based labels (fallback)
python scripts/citegraph.py score   --run RUN --explain  # six weighted signals
python scripts/citegraph.py cluster --run RUN            # co-citation clustering
python scripts/citegraph.py pack    --run RUN            # narrative input pack
#   → the agent writes RUN/narrative.md
python scripts/citegraph.py lint    --run RUN            # citations, numbers, clusters, direction
python scripts/citegraph.py layout  --run RUN            # deterministic force layout
python scripts/citegraph.py render  --run RUN            # single-file report.html
```

`RUN` is a directory such as `runs/10-1364-prj-401755`.

## Options that matter

| Flag | Command | Effect |
|---|---|---|
| `--free-mode` | `graph` | Use single-entity lookups (free) instead of batched filters; slower, ~zero cost |
| `--back` / `--forward` | config `limits` | How many references / citations to take |
| `--resolution` | `cluster` | Modularity resolution; `0` = auto (default) |
| `--text-min` | `cluster` | Cosine threshold for attaching singleton nodes |
| `--top` / `--max-nodes` | `pack` | Size of the narrative pack |
| `--narrative` | `lint`, `render` | Path to the narrative file |
| `--json` | global | Machine-readable output for `resolve`, `pack`, `lint` |
| `--config` | global | Use a specific config file |

## Artifacts

| File | Written by | Notes |
|---|---|---|
| `seed.json` | resolve | Raw OpenAlex record |
| `graph.json` | graph/label/score | Nodes, edges, scores, evidence; `meta.schema_version` |
| `clusters.json` | cluster | Cluster id, name, members |
| `pack.json` | pack | Narrative input; `constraints.allowed_nids` |
| `narrative.md` | **agent** | Four sections, `[n]` citations |
| `lint.json` | lint | `ok`, errors, warnings, coverage |
| `layout.json` | layout | `{net: {nid: [x,y]}, year: {...}}` |
| `report.html` | render | Offline, single file, no external requests |
| `scores.csv`, `graph.graphml` | export | Spreadsheet / Gephi |

## Agent-authored inputs

- `names.json` — cluster names: `{"0": "光致极化", "1": "集成平台"}` (read by `pack`).
- `labels.json` — relationship labels: `{"37": "方法"}` (overrides rule labels; `pack.meta.label_source` becomes `agent`).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | lint failed / selftest failed |
| 2 | runtime error (budget, config) |
| 3 | title matched multiple papers |
| 4 | `export-dist` found a privacy leak |
