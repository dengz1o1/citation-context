# Minimal example

A complete, real run for one paper (10.1038/s43016-026-01455-5, *Nature Food* 2026):

| File | What it is |
|---|---|
| `pack.json` | The narrative input pack — the only data the model was allowed to use |
| `narrative.md` | The four-section narrative written from it |
| `lint.json` | The audit result (`ok: true`, 0 errors, 0 warnings) |

Together they show the contract: every `[n]` in `narrative.md` exists in
`pack.json → constraints.allowed_nids`, every number comes from `stats` or `nodes`, and every
cluster with ≥3 members is covered in section 2.

`graph.json` and `report.html` are omitted to keep the repository light; regenerate them with:

```bash
cd skills/citation-context
python scripts/citegraph.py resolve "10.1038/s43016-026-01455-5"
python scripts/citegraph.py graph --run runs/10-1038-s43016-026-01455-5
# … label, score, cluster, pack, layout, render
```

This sample has 0 forward citations and no abstract — a deliberately hard case.
