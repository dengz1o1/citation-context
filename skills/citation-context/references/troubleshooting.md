# Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `429 ... budget is used up` | OpenAlex daily budget exhausted (shared by IP when keyless) | Wait for UTC midnight, set a key, or run `graph --free-mode` |
| `403 key_not_activated` | Key issued but the e-mail link was never clicked | Click the link; the script already falls back to keyless |
| `429` from Semantic Scholar | Anonymous rate limit | Contexts / `isInfluential` will be empty; rerun later or add `semantic_scholar_api_key` |
| `resolve` fails | DOI typo, or title matches several papers | Pass a DOI or arXiv ID; the command lists candidates and exits 3 |
| Backward count lower than expected | `limits.backward` cap | Raise it, or check `stats.backward_truncated` and the pack caveat |
| Pack says "只取被引最高的 N 条" | `limits.forward` cap | Narrative must state the sampling; raise the limit if needed |
| `lint` → `cluster_uncovered` | A cluster of ≥3 papers is never cited | Add one sentence, or merge that line into another |
| `lint` → `number_not_in_pack` | A number was estimated | Take it from `stats` / `nodes`, or drop the figure |
| `lint` → `direction_mismatch` | A citing paper is described as a predecessor in §2 | Move it to §3 or rephrase |
| `render` refuses to run | `graph.json` has no scores | Run `label` then `score` before `render` |
| Clusters look empty / too coarse | Citation density differs by field | Try `cluster --resolution 1.6`, or raise `--max-clusters` |
| Graph is one big hairball | Too many edges at default zoom | Zoom in; filters and the "spokes" toggle reduce clutter |

## Quick diagnosis

```bash
python scripts/citegraph.py doctor      # config path, masked key, cache, runs
python scripts/citegraph.py selftest    # offline sanity check of the four core stages
```
