# Citation Context

**Turn one paper into a citation map you can actually check.**

Give it a DOI or an arXiv ID. It pulls the paper's backward references and forward citations,
scores them, clusters the references into research lines, writes a four-section narrative, and
renders everything into a single offline HTML report — graph, ranked list, and per-paper details.

[中文说明](README.zh.md) · [Skill docs](skills/citation-context/SKILL.md) · [Data schema](skills/citation-context/references/schema.md)

![Report screenshot](examples/screenshots/report.png)

## Why another citation tool

- **Verifiable.** Every sentence in the narrative carries a `[n]` that maps to a real node; `lint`
  rejects unknown citations, made-up numbers, uncovered clusters and reversed citation directions.
- **Offline.** The report is one HTML file with no CDN, no fonts, no network calls. E-mail it,
  archive it, open it on a plane.
- **Agent-native.** The deterministic parts (fetch, dedupe, score, cluster, render) are Python;
  only the narrative is written by the model.
- **Honest about gaps.** Missing abstracts, truncated reference lists, sampling and rate limits are
  recorded in `pack.caveats`, not silently smoothed over.

## Install

**Any Agent Skills client** (Claude Code, Codex, Cursor, Gemini CLI, DSH, …):

```bash
npx skills add dengz1o1/citation-context --skill citation-context
# or copy skills/citation-context/ into your agent's skills directory
```

Requires Python ≥ 3.9 (standard library only) and network access to OpenAlex and Semantic Scholar.

## Quick start

```bash
cd skills/citation-context
python scripts/citegraph.py doctor
python scripts/citegraph.py resolve "10.1364/prj.401755"
python scripts/citegraph.py graph   --run runs/10-1364-prj-401755
python scripts/citegraph.py score   --run runs/10-1364-prj-401755
python scripts/citegraph.py cluster --run runs/10-1364-prj-401755
python scripts/citegraph.py pack    --run runs/10-1364-prj-401755
# write runs/10-1364-prj-401755/narrative.md, then:
python scripts/citegraph.py lint   --run runs/10-1364-prj-401755
python scripts/citegraph.py layout --run runs/10-1364-prj-401755
python scripts/citegraph.py render --run runs/10-1364-prj-401755
```

Or just ask your agent: *"Map the citation context of 10.1364/prj.401755."*

## What you get

| Artifact | Use |
|---|---|
| `report.html` | Interactive graph + ranked list + details + narrative |
| `graph.json` | Citation graph with six weighted signals and evidence |
| `pack.json` | The exact input the narrative was allowed to use |
| `lint.json` | Machine-checkable result of the citation audit |
| `scores.csv`, `graph.graphml` | Spreadsheet / Gephi |

## How it works

```
DOI/arXiv ─ resolve ─ graph ─ score ─ cluster ─ pack ─ narrative ─ lint ─ render ─ report.html
             │         │        │        │        │        │         │
             seed    +neighbour 6 signals  co-citation  allowed  agent   citations,
                     citations            clusters     nids     writes  numbers, clusters
```

- Backward references from OpenAlex `referenced_works`; forward citations from `filter=cites:`.
- Neighbour-to-neighbour citation edges are derived from each node's own reference list — this is
  what turns a star into a network.
- Relevance combines text similarity, bibliographic coupling, co-citation, influence, velocity and venue.
- Clustering uses coupling edges + greedy modularity with an automatic resolution.

## Data sources and cost

OpenAlex is free (CC0) and bills API usage only. A DOI lookup is free; list calls are $0.10 per
1,000. One paper costs roughly **$0.0003–0.0006** and 3–6 requests. Without a key you share a
$0.10/day IP budget; a free key gives you $1/day. `--free-mode` trades speed (2–4 min) for ~zero cost.

Semantic Scholar adds citation contexts and `isInfluential`; it is free but rate-limited.

## Privacy

- Keys live in `~/.config/citation-context/config.json` or `OPENALEX_API_KEY` — never in the skill.
- Request URLs are sanitized before being written to `graph.json`.
- `export-dist` scans the package for keys, e-mails and home paths; exit code 4 means "do not publish".

See [security.md](skills/citation-context/references/security.md).

## Limitations

- Chinese-language databases are not supported (no public API).
- Abstracts are missing for some publishers; text similarity then falls back to titles.
- Very recent papers have few forward citations; the report says so instead of inventing impact.
- Labels are heuristic; the clustering, not the labels, drives the narrative.

## License

MIT.
