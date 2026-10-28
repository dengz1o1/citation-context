# Data schema reference

Every JSON artifact carries `schema_version` (currently `1`). Fields may be added; existing fields
will not change meaning.

## graph.json

```jsonc
{
  "meta": {
    "seed_id": "W3066629616",
    "seed_doi": "10.1364/prj.401755",
    "generated_at": "2026-10-28T...",
    "schema_version": 1,
    "form": "standard",             // classic | standard | recent | obscure
    "text_source": "abstract",      // abstract | title-only (seed level)
    "requests": 4,
    "cache_hits": 0,
    "est_cost_usd": 0.0004,
    "rate_limited": false,
    "errors": []                    // URLs are sanitized (api_key removed)
  },
  "seed": { "nid": 0, "title": "...", "year": 2020, "cited_by_count": 55 },
  "nodes": [{
    "nid": 1,
    "role": "backward",           // seed | backward | forward | sibling
    "id": "W...", "doi": "10...", "title": "...", "year": 2016,
    "venue": "...", "venue_type": "journal", "venue_kind": "journal",
    "authors": ["..."], "author_count": 5,
    "cited_by_count": 328,
    "reference_count": 40, "referenced_works": ["W..."],
    "abstract": "...", "text_source": "abstract",   // abstract | title
    "scores": {
      "total": 0.39, "text": 0.62, "text_raw": 0.09,
      "cocite": 0.21, "coupling": 0.18, "cocitation": 0.26, "sibling": 0.0,
      "cocite_kind": "coupling+cocitation",          // forward → coupling+sibling
      "influence": 1.0, "velocity": 3.2, "velocity_p": 0.7,
      "recency": 0.6, "venue": 1.0
    },
    "labels": ["background"],
    "evidence": { "shared_refs": 7, "co_cited_by_forward": 3, "pagerank": 0.1,
                  "s2": { "isInfluential": true, "contexts": ["..."] } }
  }],
  "edges": [
    { "from": 0, "to": 1, "type": "cites", "source": "openalex" },
    { "from": 1, "to": 2, "type": "cites", "source": "openalex-ref" },
    { "from": 1, "to": 2, "type": "coupled", "weight": 0.12 }
  ],
  "stats": {
    "backward": 48, "backward_total": 48, "backward_truncated": false,
    "forward_total": 55, "forward_fetched": 55, "forward_sampled": 55,
    "duplicates_merged": 0, "nodes": 104, "edges": 521,
    "by_label": { "background": 32 }, "by_role": { "backward": 48, "forward": 55 }
  }
}
```

## pack.json

```jsonc
{
  "meta": { "seed_id": "...", "form": "standard", "schema_version": 1,
            "label_source": "rule" },        // rule | agent
  "seed": { "nid": 0, "title": "...", "abstract": "..." },
  "stats": {
    "backward": 48, "forward_total": 55, "co_cited_max": 18,
    "backward_year_span": [1995, 2026],
    "sampling": { "forward_total": 55, "forward_fetched": 55,
                  "forward_sampled": 55, "complete": true, "duplicates": 0 }
  },
  "groups": {
    "foundation": [1], "method": [2], "recent": [3], "hubs": [4],
    "forward": [5], "top_overall": [6],
    "clusters": [{ "id": 0, "name": "回音壁微腔", "size": 15, "packed": 7,
                   "nids": [1, 2, 3] }]
  },
  "nodes": [{ "nid": 1, "title": "...", "year": 2016, "venue": "...",
              "authors": ["..."], "cites": 328, "score": 0.39,
              "label": "background", "role": "backward",
              "text_source": "abstract", "why": "与本文共享 7 篇参考文献",
              "contexts": ["..."], "cluster": 0, "cluster_name": "回音壁微腔" }],
  "caveats": ["..."],
  "constraints": {
    "language": "zh", "citation_style": "[n]",
    "allowed_nids": [1, 2, 3],
    "rules": ["..."], "outline": [{ "section": 1, "title": "...", "use": ["..."] }]
  }
}
```

`allowed_nids` is the closed set of citations the narrative may use.

## clusters.json

```jsonc
{ "schema_version": 1,
  "clusters": [{ "id": 0, "name": "water saving", "size": 7, "packed": 2,
                 "nids": [7, 28], "reps": [[7, "title", 2006]] }],
  "node_cluster": { "7": 0 } }
```

## lint.json

```jsonc
{ "ok": true, "schema_version": 1,
  "sections": 4, "cited": 33, "allowed": 33,
  "clusters_total": 6, "clusters_covered": 6,
  "errors": [ { "type": "unknown_citation", "nid": 999 } ],
  "warnings": [ { "type": "orphan_node", "nids": [16, 22] } ] }
```

Error types: `unknown_citation`, `no_citation_in_section`, `forbidden_phrase`,
`number_not_in_pack`, `cluster_uncovered`. Warnings: `orphan_node`, `cluster_uncovered_small`,
`direction_mismatch`.
