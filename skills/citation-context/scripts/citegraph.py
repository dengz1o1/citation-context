#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
citegraph — 文献关联与表达 skill 的确定性层。

子命令：
    resolve   DOI/arXiv/标题 → seed.json
    graph     seed → graph.json（后向 + 前向 + 平行工作 + 耦合边）
    score     6 信号加权 → 分项得分（回写 graph.json）
    label     规则打标（7 类，回写 graph.json）
    export    scores.csv / graph.graphml

设计约束：
  * 只用标准库，兼容 Python 3.9+（DSH 里由 shell 调用，装依赖是最常见的失败点）。
  * LLM 不参与取数与算分；本文件产出的所有数字都可复算。
  * 请求有缓存、限速、预算上限；失败记录不静默。
"""

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
DEFAULT_CONFIG = os.path.join(SKILL_DIR, "config.json")

OPENALEX = "https://api.openalex.org"
S2 = "https://api.semanticscholar.org/graph/v1"
S2_REC = "https://api.semanticscholar.org/recommendations/v1"

STOPWORDS = set("""a an the of and or in on for to with by from at as is are was were be been being
this that these those we our it its their they them which who whom whose what when
where while into over under between within without using used use can could may might
will would should not no than then also such more most less least very much many few
here there however thus therefore because if but both each other same different new
""".split())

VENUE_PREPRINT = ("arxiv", "biorxiv", "medrxiv", "ssrn", "research square", "preprint", "zenodo")

# 出处权重默认值：会议低于期刊，预印本最低。可在 config.json 的 venue_weights 覆盖。
DEFAULT_VENUE_WEIGHTS = {
    "journal": 1.0, "conference": 0.8, "book": 0.7,
    "repository": 0.35, "preprint": 0.2, "unknown": 0.5,
}


# ---------------------------------------------------------------- 基础工具

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def eprint(*a):
    print(*a, file=sys.stderr)


def load_config(path=None):
    """配置查找顺序：显式 --config > 环境变量 CITEGRAPH_CONFIG > 用户配置 > skill 内置 config.json。

    发布版里内置 config.json 只放默认值（不含 key）；用户的 key 放在自己的用户配置或环境变量里。
    """
    candidates = []
    if path:
        candidates.append(path)
    if os.environ.get("CITEGRAPH_CONFIG"):
        candidates.append(os.environ["CITEGRAPH_CONFIG"])
    candidates.append(os.path.join(os.path.expanduser("~"), ".config", "citation-context", "config.json"))
    candidates.append(DEFAULT_CONFIG)
    chosen = next((p for p in candidates if p and os.path.exists(p)), None)
    cfg = {}
    if chosen:
        with open(chosen, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg["_config_path"] = chosen or "(默认值，未找到配置文件)"
    # 环境变量优先：方便把 key 从文件里挪出去（config.json 可能被分享或提交）
    if os.environ.get("OPENALEX_API_KEY"):
        cfg["openalex_api_key"] = os.environ["OPENALEX_API_KEY"]
    if os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
        cfg["semantic_scholar_api_key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
    if os.environ.get("CITEGRAPH_FREE_MODE") in ("1", "true", "yes"):
        cfg["free_mode"] = True
    cfg.setdefault("venue_weights", dict(DEFAULT_VENUE_WEIGHTS))
    for kind, val in DEFAULT_VENUE_WEIGHTS.items():
        cfg["venue_weights"].setdefault(kind, val)
    return cfg


def slugify(s):
    s = s.lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    s = re.sub(r"[^0-9a-z]+", "-", s).strip("-")
    return s[:80] or "run"


def norm_doi(s):
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    return s.lower()


def short_title(t, n=90):
    t = (t or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def abstract_from_inverted(inv):
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def venue_of(work):
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    return (src.get("display_name") or "").strip()


def venue_type_of(work):
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    return (src.get("type") or "").strip()


def authors_of(work, n=3):
    out = []
    for a in (work.get("authorships") or [])[:n]:
        name = ((a.get("author") or {}).get("display_name") or "").strip()
        if name:
            out.append(name)
    return out


def wid_of(u):
    return (u or "").rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------- HTTP + 缓存

def strip_api_key(url):
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k != "api_key"]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))


def sanitize_url(url):
    """错误信息里不能出现 api_key=明文。"""
    return re.sub(r"(api_key=)[^&\s\"]+", r"\1***", url or "")


class Http:
    def __init__(self, cfg, cache_path=None):
        self.cfg = cfg
        self.cache_path = cache_path or os.environ.get("CITEGRAPH_CACHE") or os.path.join(
            os.getcwd(), ".cache", "citegraph.db")
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        self.db = sqlite3.connect(self.cache_path)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, ts REAL, v TEXT)")
        # 旧版本把 api_key 写进过缓存键；启动时清掉，避免缓存文件里残留明文 key
        self.db.execute("DELETE FROM kv WHERE k LIKE '%api_key=%'")
        self.db.commit()
        self.budget = int(cfg.get("limits", {}).get("max_requests", 80))
        self.used = 0
        self.served = 0
        self.min_interval = float(cfg.get("limits", {}).get("min_interval_s", 0.34))
        self._last = 0.0
        self.errors = []
        self.notes = []
        self.exhausted = False
        self.key_pending = False

    def _cache_get(self, key, ttl_days):
        row = self.db.execute("SELECT ts, v FROM kv WHERE k=?", (key,)).fetchone()
        if not row:
            return None
        ts, v = row
        if (time.time() - ts) > ttl_days * 86400:
            return None
        return json.loads(v)

    def _cache_put(self, key, val):
        self.db.execute("INSERT OR REPLACE INTO kv (k, ts, v) VALUES (?,?,?)",
                        (key, time.time(), json.dumps(val, ensure_ascii=False)))
        self.db.commit()

    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def get(self, url, ttl_days=7, retries=3):
        # 缓存键剥掉 api_key：换 key、或 key 未激活降级时，都不该让缓存失效
        key = "GET " + strip_api_key(url)
        hit = self._cache_get(key, ttl_days)
        if hit is not None:
            self.served += 1
            return hit
        if self.used >= self.budget:
            raise RuntimeError("请求预算用尽（%d 次）。调大 config.limits.max_requests 或改用缓存。" % self.budget)
        last = None
        for attempt in range(retries):
            if self.key_pending:
                url = strip_api_key(url)
            self._throttle()
            self.used += 1
            req = urllib.request.Request(url, headers={
                "User-Agent": "citegraph/0.1 (research; contact: %s)" % (self.cfg.get("email") or "none"),
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    body = r.read().decode("utf-8", "replace")
                val = json.loads(body)
                self._cache_put(key, val)
                return val
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                last = "HTTP %s %s" % (e.code, body[:180])
                if e.code == 403 and "key_not_activated" in body:
                    # key 已签发但邮箱还没点激活链接：降级为无 key 继续，别让整条流程挂掉
                    self.key_pending = True
                    self.notes.append("OpenAlex key 未激活（邮箱未点确认），已自动降级为无 key 模式")
                    continue
                if e.code == 429 and "budget" in body.lower():
                    self.exhausted = True
                    self.errors.append({"url": sanitize_url(url), "error": "OpenAlex 日预算耗尽（按 IP 共享，UTC 午夜重置）"})
                    return None
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
                time.sleep(1.5 * (attempt + 1))
        self.errors.append({"url": sanitize_url(url), "error": last})
        return None


# ---------------------------------------------------------------- 取数

def oa_params(cfg, extra=None):
    p = {}
    if cfg.get("email"):
        p["mailto"] = cfg["email"]
    if cfg.get("openalex_api_key"):
        p["api_key"] = cfg["openalex_api_key"]
    if extra:
        p.update(extra)
    return p


def resolve_doi(http, cfg, doi):
    q = urllib.parse.quote("https://doi.org/" + doi, safe="")
    url = OPENALEX + "/works/" + q + "?" + urllib.parse.urlencode(oa_params(cfg))
    return http.get(url, ttl_days=7)


def fetch_seed_by_title(http, cfg, title):
    params = oa_params(cfg, {"search": title, "per-page": "5",
                             "select": "id,doi,title,publication_year,type"})
    data = http.get(OPENALEX + "/works?" + urllib.parse.urlencode(params), ttl_days=1)
    return (data or {}).get("results") or []


def fetch_works(http, cfg, ids, chunk=50, ttl=7):
    """按 W-id 取元数据；返回 {wid: work}。

    free_mode: 用单实体查询（官方计费为免费）逐条取，而不是 filter 批量（$0.10/千次）。
    代价是请求数从 2 次变成 N 次、耗时约 6 秒/50 篇，但计费接近 0。
    """
    out, ids = {}, [wid_of(i) for i in ids if i]
    if cfg.get("free_mode"):
        sel = ("id,doi,title,publication_year,publication_date,type,primary_location,"
               "authorships,cited_by_count,referenced_works,abstract_inverted_index")
        for wid in ids:
            url = OPENALEX + "/works/" + wid + "?" + urllib.parse.urlencode(oa_params(cfg, {"select": sel}))
            w = http.get(url, ttl_days=ttl)
            if w:
                out[wid_of(w.get("id"))] = w
        return out
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        params = oa_params(cfg, {
            "filter": "openalex_id:" + "|".join(part),
            "per-page": str(len(part)),
            "select": ("id,doi,title,publication_year,publication_date,type,primary_location,"
                       "authorships,cited_by_count,referenced_works,abstract_inverted_index"),
        })
        data = http.get(OPENALEX + "/works?" + urllib.parse.urlencode(params), ttl_days=ttl)
        for w in (data or {}).get("results") or []:
            out[wid_of(w.get("id"))] = w
    return out


def fetch_forward(http, cfg, wid, limit):
    params = oa_params(cfg, {
        "filter": "cites:" + wid,
        "per-page": str(min(200, max(1, limit))),
        "sort": "cited_by_count:desc",
        "select": ("id,doi,title,publication_year,publication_date,type,primary_location,"
                   "authorships,cited_by_count,referenced_works,abstract_inverted_index"),
    })
    data = http.get(OPENALEX + "/works?" + urllib.parse.urlencode(params), ttl_days=1) or {}
    return data.get("results") or [], (data.get("meta") or {}).get("count") or 0


def fetch_s2_citations(http, cfg, doi, limit=100):
    key = cfg.get("semantic_scholar_api_key")
    url = (S2 + "/paper/DOI:" + urllib.parse.quote(doi) + "/citations"
           + "?fields=contexts,intents,isInfluential,externalIds,title,year&limit=" + str(limit))
    if key:
        url += "&x-api-key=" + urllib.parse.quote(key)
    data = http.get(url, ttl_days=1)
    out = {}
    for item in (data or {}).get("data") or []:
        ext = (item.get("citingPaper") or {}).get("externalIds") or {}
        d = norm_doi(ext.get("DOI") or "")
        if not d:
            continue
        out[d] = {
            "isInfluential": bool(item.get("isInfluential")),
            "contexts": [c.strip() for c in (item.get("contexts") or []) if c and c.strip()],
            "intents": item.get("intents") or [],
        }
    return out


# ---------------------------------------------------------------- 构图

def fetch_s2_recs(http, cfg, doi, limit=20):
    """S2 推荐接口。质量参差，必须再过一道文本相似度闸门。"""
    key = cfg.get("semantic_scholar_api_key")
    url = (S2_REC + "/papers/forpaper/DOI:" + urllib.parse.quote(doi)
           + "?limit=%d&fields=title,year,citationCount,venue,abstract,externalIds" % limit)
    if key:
        url += "&x-api-key=" + urllib.parse.quote(key)
    data = http.get(url, ttl_days=3) or {}
    return data.get("recommendedPapers") or []


def build_siblings(http, cfg, seed, doi):
    """平行工作：S2 推荐 + 文本闸门 + 上限。仅作“同期相关”，不是引用证据。"""
    lim = (cfg.get("limits") or {}).get("sibling") or {}
    cap = int(lim.get("max", 8))
    gate = float(lim.get("min_token_overlap", 0.02))
    if not doi:
        return []
    seed_text = (seed.get("title") or "") + " " + abstract_from_inverted(seed.get("abstract_inverted_index"))
    st = set(tokenize(seed_text))
    out, seen = [], set()
    for p in fetch_s2_recs(http, cfg, doi, limit=max(20, cap * 3)):
        d = norm_doi(((p.get("externalIds") or {}).get("DOI") or ""))
        if not d or d in seen:
            continue
        toks = set(tokenize((p.get("title") or "") + " " + (p.get("abstract") or "")))
        union = len(toks | st) or 1
        if (len(toks & st) / float(union)) < gate:
            continue
        seen.add(d)
        out.append(p)
        if len(out) >= cap:
            break
    return out


def node_from_s2(nid, p):
    return {
        "nid": nid,
        "role": "sibling",
        "id": "",
        "doi": norm_doi(((p.get("externalIds") or {}).get("DOI") or "")),
        "title": (p.get("title") or "").strip(),
        "year": p.get("year"),
        "date": "",
        "venue": (p.get("venue") or "").strip(),
        "authors": [],
        "type": "article",
        "cited_by_count": p.get("citationCount") or 0,
        "reference_count": 0,
        "referenced_works": [],
        "abstract": (p.get("abstract") or "")[:1500],
        "scores": {},
        "labels": [],
        "evidence": {"source": "s2-recommendation"},
    }


def node_from_work(nid, role, w, extra=None):
    refs = [wid_of(r) for r in (w.get("referenced_works") or [])]
    abstract = abstract_from_inverted(w.get("abstract_inverted_index"))[:1500]
    n = {
        "nid": nid,
        "role": role,
        "id": wid_of(w.get("id")),
        "doi": norm_doi(w.get("doi") or ""),
        "title": (w.get("title") or "").strip(),
        "year": w.get("publication_year"),
        "date": w.get("publication_date"),
        "venue": venue_of(w),
        "venue_type": venue_type_of(w),
        "authors": authors_of(w),
        "author_count": len(w.get("authorships") or []),
        "type": w.get("type"),
        "cited_by_count": w.get("cited_by_count") or 0,
        "reference_count": len(refs),
        "referenced_works": refs,
        "abstract": abstract,
        "text_source": "abstract" if len(abstract) >= 100 else "title",
        "scores": {},
        "labels": [],
        "evidence": {},
    }
    if extra:
        n.update(extra)
    return n


def _norm_title(t):
    t = re.sub(r"[^a-z0-9]+", " ", (t or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def dedupe_versions(nodes, edges):
    """同一工作的预印本与正式版合并：归一化标题相同即视为重复。

    保留优先级：非 arXiv DOI > 被引更高 > 摘要更长。
    返回 (nodes, edges, merged_count)，nid 会重新编号（seed 仍为 0）。
    """
    groups = {}
    for n in nodes[1:]:
        key = _norm_title(n.get("title"))
        if len(key) < 20:
            continue
        groups.setdefault(key, []).append(n)

    remap, merged = {}, 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda n: (
            0 if (n.get("doi") or "").startswith("10.48550/") else 1,
            n.get("cited_by_count") or 0,
            len(n.get("abstract") or ""),
        ), reverse=True)
        keep = group[0]
        for dup in group[1:]:
            remap[dup["nid"]] = keep["nid"]
            keep.setdefault("merged_from", []).append(
                {"doi": dup.get("doi"), "year": dup.get("year"),
                 "cited": dup.get("cited_by_count")})
            merged += 1
    if not remap:
        return nodes, edges, 0

    kept = [n for n in nodes if n["nid"] not in remap]
    new_index = {n["nid"]: i for i, n in enumerate(kept)}
    for n in kept:
        n["nid"] = new_index[n["nid"]]

    seen_pairs, new_edges = set(), []
    for e in edges:
        a = remap.get(e["from"], e["from"])
        b = remap.get(e["to"], e["to"])
        if a == b:
            continue
        e2 = dict(e)
        e2["from"], e2["to"] = new_index[a], new_index[b]
        k = (e2["from"], e2["to"], e2["type"])
        if k in seen_pairs:
            continue
        seen_pairs.add(k)
        new_edges.append(e2)
    return kept, new_edges, merged


def build_graph(http, cfg, seed, slug):
    wid = wid_of(seed.get("id"))
    refs = seed.get("referenced_works") or []
    lim = cfg.get("limits", {})
    back_ids = [wid_of(r) for r in refs][: int(lim.get("backward", 60))]
    back_total = len(refs)
    back_works = fetch_works(http, cfg, back_ids)

    fwd_works, fwd_total = fetch_forward(http, cfg, wid, int(lim.get("forward", 80)))
    fwd_fetched = len(fwd_works)

    doi = norm_doi(seed.get("doi") or "")
    s2map = fetch_s2_citations(http, cfg, doi) if doi else {}
    sib_lim = lim.get("sibling") or {}
    siblings = build_siblings(http, cfg, seed, doi) if sib_lim.get("enabled") else []

    nodes = [node_from_work(0, "seed", seed)]
    seen = {wid: 0}
    order = []
    for b in back_ids:
        w = back_works.get(b)
        if not w:
            continue
        nid = len(nodes)
        nodes.append(node_from_work(nid, "backward", w))
        seen[b] = nid
        order.append(("backward", b, nid))
    for w in fwd_works:
        k = wid_of(w.get("id"))
        if k in seen:
            continue
        nid = len(nodes)
        nodes.append(node_from_work(nid, "forward", w))
        seen[k] = nid
    have_dois = {n.get("doi") for n in nodes if n.get("doi")}
    for p in siblings:
        d = norm_doi(((p.get("externalIds") or {}).get("DOI") or ""))
        if not d or d in have_dois:
            continue
        nid = len(nodes)
        nodes.append(node_from_s2(nid, p))
        have_dois.add(d)

    for n in nodes:
        hit = s2map.get(n.get("doi") or "")
        n["evidence"]["s2"] = hit or None

    # 共被引：多少篇施引文献同时引用了本节点。
    # 零额外请求——前向节点的 referenced_works 已经在手；比 Jaccard 更直接反映“枢纽”地位。
    fwd_refs = [set(f.get("referenced_works") or []) for f in nodes if f["role"] == "forward"]
    for n in nodes[1:]:
        w_id = n.get("id")
        n["evidence"]["co_cited_by_forward"] = (
            sum(1 for rs in fwd_refs if w_id and w_id in rs) if fwd_refs else 0)

    edges = []
    for n in nodes[1:]:
        if n["role"] == "backward":
            edges.append({"from": 0, "to": n["nid"], "type": "cites", "source": "openalex"})
        elif n["role"] == "forward":
            edges.append({"from": n["nid"], "to": 0, "type": "cites", "source": "openalex"})
        else:
            edges.append({"from": 0, "to": n["nid"], "type": "sibling", "source": "s2-recommendation"})

    # 邻居之间的引用边：零额外请求，来自各自的 referenced_works。
    # 没有这一层，图就是"seed 在中心、所有节点围成放射状"的星形；有了它才有 Connected Papers 那种网状结构。
    for n in nodes[1:]:
        for r in (n.get("referenced_works") or []):
            tgt = seen.get(r)
            if tgt is None or tgt == 0 or tgt == n["nid"]:
                continue
            edges.append({"from": n["nid"], "to": tgt, "type": "cites", "source": "openalex-ref"})

    # 耦合边：共享参考文献比例（补充"没有直接引用但同源"的关系）
    min_c = float(lim.get("coupling_edge_min", 0.10))
    max_e = int(lim.get("coupling_edges_max", 40))
    ref_sets = {n["nid"]: set(n.get("referenced_works") or []) for n in nodes}
    pairs = []
    ids = [n["nid"] for n in nodes[1:]]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ref_sets[ids[i]], ref_sets[ids[j]]
            if not a or not b:
                continue
            inter = len(a & b)
            if inter < 3:
                continue
            jac = inter / float(len(a | b))
            if jac >= min_c:
                pairs.append((jac, ids[i], ids[j]))
    pairs.sort(reverse=True)
    for jac, a, b in pairs[:max_e]:
        edges.append({"from": a, "to": b, "type": "coupled", "weight": round(jac, 3),
                      "source": "openalex"})

    # 预印本与正式版去重：同一标题只保留一条（正式版优先），边重定向到保留节点
    nodes, edges, merged = dedupe_versions(nodes, edges)

    return nodes, edges, {
        "backward": sum(1 for n in nodes if n["role"] == "backward"),
        "backward_total": back_total,
        "backward_truncated": back_total > len(back_ids),
        "forward_total": fwd_total,
        "forward_fetched": fwd_fetched,
        "forward_sampled": sum(1 for n in nodes if n["role"] == "forward"),
        "sibling": sum(1 for n in nodes if n["role"] == "sibling"),
        "duplicates_merged": merged,
        "nodes": len(nodes),
        "edges": len(edges),
    }


def classify_form(seed, stats):
    fwd = stats.get("forward_total") or 0
    back = seed.get("referenced_works_count") or stats.get("backward") or 0
    date = seed.get("publication_date") or ""
    months = 999
    m = re.match(r"(\d{4})-(\d{2})", date or "")
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        now = datetime.now()
        months = (now.year - y) * 12 + (now.month - mo)
    if fwd < 5 and back < 20:
        return "obscure"
    if months < 24 or fwd < 30:
        return "recent"
    if fwd >= 200:
        return "classic"
    return "standard"


# ---------------------------------------------------------------- 打分

def tokenize(text):
    toks = [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 2]
    return [t for t in toks if t not in STOPWORDS]


def idf_weights(docs):
    df = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    n = max(1, len(docs))
    return {t: math.log((n + 1.0) / (c + 1.0)) + 1.0 for t, c in df.items()}


def wjaccard(a, b, w):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = sum(w.get(t, 1.0) for t in sa & sb)
    union = sum(w.get(t, 1.0) for t in sa | sb)
    return inter / union if union else 0.0


def pagerank(nodes, edges, damping=0.85, iters=40):
    n = len(nodes)
    if n == 0:
        return {}
    out = {i: [] for i in range(n)}
    for e in edges:
        if e["type"] == "cites":
            out[e["from"]].append(e["to"])
    pr = {i: 1.0 / n for i in range(n)}
    for _ in range(iters):
        nxt = {i: (1.0 - damping) / n for i in range(n)}
        for i in range(n):
            if not out[i]:
                for j in range(n):
                    nxt[j] += damping * pr[i] / n
            else:
                share = damping * pr[i] / len(out[i])
                for j in out[i]:
                    nxt[j] += share
        pr = nxt
    return pr


def pct_rank(values):
    """返回 {index: 0..1} 的百分位（并列取平均位次）。"""
    idx = list(range(len(values)))
    order = sorted(idx, key=lambda i: values[i])
    out = {}
    n = len(values)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2.0
        for k in range(i, j + 1):
            out[order[k]] = rank / (n - 1) if n > 1 else 1.0
        i = j + 1
    return out


def venue_kind(n):
    """把出处归到 journal / conference / book / repository / preprint / unknown。"""
    t = (n.get("type") or "").lower()
    vt = (n.get("venue_type") or "").lower()
    name = (n.get("venue") or "").lower()
    if t == "preprint" or any(k in name for k in VENUE_PREPRINT):
        return "preprint"
    if vt == "conference" or t == "conference-paper":
        return "conference"
    if vt in ("book", "book series", "ebook platform") or t in ("book", "book-chapter"):
        return "book"
    if vt == "repository":
        return "repository"
    if vt == "journal" or t in ("article", "review", "letter"):
        return "journal"
    return "unknown"


def compute_scores(cfg, nodes, edges, seed, text_only=False):
    w = cfg.get("weights", {})
    scale = float(cfg.get("text_scale", 0.15))
    # 没有摘要时，文本相似度只来自标题，噪声大得多，降权让共被引主导
    w_text = w.get("text", 0.42) * (0.6 if text_only else 1.0)
    now_year = datetime.now().year

    texts = [tokenize((n.get("title") or "") + " " + (n.get("abstract") or "")) for n in nodes]
    idf = idf_weights(texts)
    seed_tokens = texts[0]
    seed_refs = set(nodes[0].get("referenced_works") or [])
    pr = pagerank(nodes, edges)
    pr_max = max(pr.values()) if pr else 1.0

    for i, n in enumerate(nodes):
        raw_text = 1.0 if i == 0 else wjaccard(seed_tokens, texts[i], idf)
        s = {
            "text": round(min(1.0, raw_text / scale), 4),
            "text_raw": round(raw_text, 4),
            "cocite": 0.0,
            "coupling": 0.0,
            "cocitation": 0.0,
            "sibling": 0.0,
            "cocite_kind": "coupling+sibling" if n["role"] == "forward" else "coupling+cocitation",
            "influence": 0.0,
            "velocity": 0.0,
            "recency": 0.0,
            "venue": 0.0,
        }
        refs = set(n.get("referenced_works") or [])
        n_fwd = sum(1 for x in nodes if x["role"] == "forward")
        if i > 0 and seed_refs and refs:
            inter = len(seed_refs & refs)
            ref_jac = inter / float(len(seed_refs | refs))
            n["evidence"]["shared_refs"] = inter
            co = (n["evidence"].get("co_cited_by_forward") or 0) / float(max(1, n_fwd))
            s["coupling"] = round(ref_jac, 4)
            if n["role"] == "forward":
                # 前向节点的"共被引"是另一回事：它们都引本文，彼此之间是同批关系。
                # 这里用"同批里有多少篇也引用了它"来度量它在衍生集合中的中心度。
                sib = (n["evidence"].get("co_cited_by_forward") or 0) / float(max(1, n_fwd - 1))
                s["sibling"] = round(sib, 4)
                s["cocite"] = round(0.6 * ref_jac + 0.4 * sib, 4)
            else:
                s["cocitation"] = round(co, 4)
                s["cocite"] = round(0.6 * ref_jac + 0.4 * co, 4)
        s2 = (n.get("evidence") or {}).get("s2") or {}
        s["influence"] = 1.0 if s2.get("isInfluential") else 0.0
        yrs = n.get("year") or 0
        if yrs:
            age = max(1, now_year - yrs + 1)
            s["velocity"] = round(math.log1p((n.get("cited_by_count") or 0) / float(age)), 4)
            s["recency"] = round(max(0.0, 1.0 - (now_year - yrs) / 15.0), 4)
        kind = venue_kind(n)
        n["venue_kind"] = kind
        s["venue"] = float((cfg.get("venue_weights") or {}).get(kind, 0.5))
        n["evidence"]["pagerank"] = round(pr.get(i, 0) / pr_max if pr_max else 0.0, 4)
        n["scores"] = s

    # 只有 velocity 无量纲，用全局百分位；其余用绝对值，
    # 避免“小组内矮子里拔将军”（实测：3 篇不相关文献靠组内百分位拿到 0.75）。
    idxs = [i for i in range(1, len(nodes))]
    if len(idxs) >= 3:
        ranks = pct_rank([nodes[i]["scores"]["velocity"] for i in idxs])
        for k, i in enumerate(idxs):
            nodes[i]["scores"]["velocity_p"] = round(ranks[k], 4)
    else:
        for i in idxs:
            nodes[i]["scores"]["velocity_p"] = 0.5

    for i, n in enumerate(nodes):
        s = n["scores"]
        total = (
            w_text * s.get("text", 0.0)
            + w.get("cocite", 0.28) * s.get("cocite", 0.0)
            + w.get("influence", 0.15) * s.get("influence", 0.0)
            + w.get("velocity", 0.05) * s.get("velocity_p", 0.0)
            + w.get("recency", 0.05) * s.get("recency", 0.0)
            + w.get("venue", 0.05) * s.get("venue", 0.0)
        )
        s["total"] = round(total if i > 0 else 1.0, 4)
    return nodes


# ---------------------------------------------------------------- 打标

RULES = [
    ("review", re.compile(r"\b(survey|review|overview|perspective|tutorial|progress report)\b", re.I)),
    ("data", re.compile(r"\b(dataset|benchmark|database|corpus|data set)\b", re.I)),
    ("critique", re.compile(r"\b(challenge|limitation|revisit|re-examin|contradict|comment on|reply to|pitfall)\b", re.I)),
    ("method", re.compile(r"\b(method|technique|scheme|algorithm|framework|model|theory|simulation|design)\b", re.I)),
    ("application", re.compile(r"\b(application|imaging|sensing|communication|lidar|spectroscopy|metrology|display|ranging)\b", re.I)),
]

PROPOSE_RX = re.compile(
    r"^\s*(we|here we|in this (work|paper|study))\s+(propose|present|demonstrate|develop|introduce|report|show)",
    re.I)


def assign_labels(nodes):
    """规则层只做高置信度判断，其余按角色给默认值；细分类留给可选 LLM 层。

    实测教训：用摘要全文匹配关键词会把 43% 的节点打成 method（"method" 一词几乎人人都有），
    所以这里只匹配标题，方法类再看摘要开头是否是 "We propose/present/…"。
    """
    for n in nodes:
        if n["nid"] == 0:
            n["labels"] = ["seed"]
            continue
        title = n.get("title") or ""
        head = (n.get("abstract") or "")[:180]
        lab = None
        for name, rx in RULES:
            if rx.search(title):
                lab = name
                break
        if lab is None and PROPOSE_RX.match(head):
            lab = "method"
        role = n["role"]
        s2 = (n.get("evidence") or {}).get("s2") or {}
        if role == "forward":
            if lab in (None, "method", "background"):
                lab = "extension"
            if s2.get("isInfluential") and lab != "critique":
                lab = "extension"
        if lab is None:
            lab = "background"
        n["labels"] = [lab]
    return nodes


# ---------------------------------------------------------------- 输出

def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def export_csv(run_dir, nodes):
    path = os.path.join(run_dir, "scores.csv")
    cols = ["nid", "role", "year", "cites", "total", "text", "cocite", "influence",
            "velocity", "recency", "venue", "label", "title", "doi"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for n in sorted(nodes, key=lambda x: -x["scores"].get("total", 0)):
            s = n["scores"]
            w.writerow([n["nid"], n["role"], n.get("year"), n.get("cited_by_count"),
                        s.get("total"), s.get("text"), s.get("cocite"), s.get("influence"),
                        s.get("velocity"), s.get("recency"), s.get("venue"),
                        (n.get("labels") or [""])[0], n.get("title"), n.get("doi")])
    return path


def export_graphml(run_dir, nodes, edges):
    path = os.path.join(run_dir, "graph.graphml")
    esc = lambda s: (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
             '<key id="label" for="node" attr.name="label" attr.type="string"/>',
             '<key id="role" for="node" attr.name="role" attr.type="string"/>',
             '<key id="year" for="node" attr.name="year" attr.type="int"/>',
             '<key id="score" for="node" attr.name="score" attr.type="double"/>',
             '<graph id="G" edgedefault="directed">']
    for n in nodes:
        lines.append('<node id="n%d"><data key="label">%s</data><data key="role">%s</data>'
                     '<data key="year">%s</data><data key="score">%s</data></node>'
                     % (n["nid"], esc(short_title(n.get("title"), 80)), esc(n["role"]),
                        n.get("year") or 0, n["scores"].get("total", 0)))
    for i, e in enumerate(edges):
        lines.append('<edge id="e%d" source="n%d" target="n%d"><data key="label">%s</data></edge>'
                     % (i, e["from"], e["to"], esc(e["type"])))
    lines.append("</graph></graphml>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def print_explain(nodes, k=12):
    print("\n名次  角色      年份  被引  总分   文本  共被引 影响力 热度  新近  出处   标签")
    print("-" * 100)
    rows = sorted([n for n in nodes if n["nid"] != 0], key=lambda x: -x["scores"].get("total", 0))
    for i, n in enumerate(rows[:k], 1):
        s = n["scores"]
        print("%-4d  %-9s %-5s %-5s %-5.3f  %.2f  %.2f   %.2f   %.2f  %.2f  %.2f  %-9s %s"
              % (i, n["role"], n.get("year") or "-", n.get("cited_by_count") or 0,
                 s.get("total", 0), s.get("text", 0), s.get("cocite", 0),
                 s.get("influence", 0), s.get("velocity_p", 0), s.get("recency", 0),
                 s.get("venue", 0), (n.get("labels") or ["-"])[0],
                 short_title(n.get("title"), 50)))
    print("\n（文本=IDF 加权 Jaccard/0.15 截断，共被引=参考文献 Jaccard，影响力=S2 isInfluential，")
    print("  热度=全局百分位，新近/出处为绝对分）")


# ---------------------------------------------------------------- 子命令

def arxiv_to_doi(raw):
    """识别 arXiv ID（新式/旧式/URL），返回 DataCite DOI；不是 arXiv 就返回 None。"""
    s = raw.strip()
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})",
                  s, re.I)
    if not m:
        m = re.match(r"^(?:arxiv:\s*)?([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})(?:v\d+)?$",
                     s, re.I)
    if not m:
        return None
    return "10.48550/arxiv." + m.group(1).lower()


def cmd_resolve(args, cfg):
    http = Http(cfg)
    raw = args.query.strip()
    doi = norm_doi(raw)
    arx = arxiv_to_doi(raw)
    if arx:
        doi = arx
        print("arXiv ID → DOI: %s" % doi)
    seed = None
    if re.match(r"^10\.\d{4,9}/", doi):
        seed = resolve_doi(http, cfg, doi)
    if not seed:
        cands = fetch_seed_by_title(http, cfg, raw)
        if len(cands) == 1:
            seed = cands[0]
        elif len(cands) > 1:
            print("标题匹配到多篇，请改用 DOI：")
            for c in cands:
                print("  %-40s %s (%s)" % (norm_doi(c.get("doi")), c.get("title"), c.get("publication_year")))
            return 3
    if not seed:
        print("解析失败：", http.errors[-1] if http.errors else "未找到")
        return 1
    out = args.out or os.path.join("runs", slugify(norm_doi(seed.get("doi") or raw)))
    os.makedirs(out, exist_ok=True)
    write_json(os.path.join(out, "seed.json"), seed)
    if cfg.get("_json"):
        print(json.dumps({"schema_version": 1, "run": out, "seed": {
            "id": wid_of(seed.get("id")), "doi": norm_doi(seed.get("doi") or ""),
            "title": seed.get("title"), "year": seed.get("publication_year"),
            "cited_by_count": seed.get("cited_by_count"),
            "reference_count": len(seed.get("referenced_works") or []),
        }}, ensure_ascii=False))
        return 0
    print("seed: %s (%s, %s)" % (short_title(seed.get("title"), 70), seed.get("publication_year"), wid_of(seed.get("id"))))
    print("输出: %s" % out)
    return 0


def cmd_graph(args, cfg):
    if getattr(args, "free_mode", False):
        cfg = dict(cfg)
        cfg["free_mode"] = True
    http = Http(cfg)
    if cfg.get("free_mode"):
        http.min_interval = min(http.min_interval, 0.12)
    seed = read_json(os.path.join(args.run, "seed.json"))
    nodes, edges, stats = build_graph(http, cfg, seed, os.path.basename(args.run))
    form = classify_form(seed, stats)
    graph = {
        "meta": {
            "seed_id": wid_of(seed.get("id")),
            "seed_doi": norm_doi(seed.get("doi") or ""),
            "generated_at": now_iso(),
            "schema_version": 1,
            "form": form,
            "text_source": "abstract" if len(abstract_from_inverted(seed.get("abstract_inverted_index"))) >= 100 else "title-only",
            "sources": ["openalex", "semanticscholar"],
            "requests": http.used,
            "cache_hits": http.served,
            "est_cost_usd": round(http.used * 0.0001, 6),
            "rate_limited": http.exhausted,
            "errors": http.errors,
        },
        "seed": {k: v for k, v in nodes[0].items() if k != "referenced_works"},
        "nodes": nodes,
        "edges": edges,
        "stats": stats,
    }
    graph["stats"]["by_role"] = {}
    for n in nodes:
        graph["stats"]["by_role"][n["role"]] = graph["stats"]["by_role"].get(n["role"], 0) + 1
    write_json(os.path.join(args.run, "graph.json"), graph)
    print("形态: %s | 节点 %d（后向 %d / 前向 %d / 平行 %d）| 边 %d | 前向总数 %d"
          % (form, stats["nodes"], stats["backward"], stats["forward_sampled"],
             stats["sibling"], stats["edges"], stats["forward_total"]))
    print("取数模式: %s | 请求 %d 次（缓存命中 %d）| 约 $%.5f"
          % ("free（单实体查询，计费≈0）" if cfg.get("free_mode") else "batch",
             http.used, http.served, 0 if cfg.get("free_mode") else http.used * 0.0001))
    for note in http.notes:
        print("提示:", note)
    if http.exhausted:
        print("注意: OpenAlex 日预算已耗尽，后向/前向可能不完整；建议配置 openalex_api_key。")
    if http.errors:
        print("警告 %d 条：" % len(http.errors), json.dumps(http.errors[:2], ensure_ascii=False))
    print("输出: %s" % os.path.join(args.run, "graph.json"))
    return 0


def cmd_score(args, cfg):
    graph = read_json(os.path.join(args.run, "graph.json"))
    text_only = (graph.get("meta") or {}).get("text_source") == "title-only"
    nodes = compute_scores(cfg, graph["nodes"], graph["edges"], graph["seed"], text_only=text_only)
    graph["nodes"] = nodes
    graph["meta"]["scored_at"] = now_iso()
    if text_only:
        print("注意: 本文无摘要（OpenAlex/S2/Crossref 均未收录），文本相似度仅基于标题，已降权。")
    write_json(os.path.join(args.run, "graph.json"), graph)
    if args.explain:
        print_explain(nodes)
    print("\n输出: %s" % os.path.join(args.run, "graph.json"))
    return 0


def cmd_label(args, cfg):
    graph = read_json(os.path.join(args.run, "graph.json"))
    nodes = assign_labels(graph["nodes"])
    graph["nodes"] = nodes
    counts = {}
    for n in nodes[1:]:
        k = (n.get("labels") or ["-"])[0]
        counts[k] = counts.get(k, 0) + 1
    graph["stats"]["by_label"] = counts
    write_json(os.path.join(args.run, "graph.json"), graph)
    print("标签分布:", json.dumps(counts, ensure_ascii=False))
    return 0


def cmd_export(args, cfg):
    graph = read_json(os.path.join(args.run, "graph.json"))
    print("CSV     :", export_csv(args.run, graph["nodes"]))
    print("GraphML :", export_graphml(args.run, graph["nodes"], graph["edges"]))
    return 0


# ---------------------------------------------------------------- pack

CITE_MARK_RX = re.compile(r"\[\s*\d+(?:\s*[-–—,，]\s*\d+)*\s*\]")


def sanitize_context(text, limit=300, min_len=40):
    """S2 的 contexts 里带的是施引文献自己的编号（如 [37]），必须删掉，
    否则渲染时会被误当成我们的 [n]。同时做三件事：
    1) NFKC 归一化（ﬁ→fi 这类连字）；2) 去掉标点前的多余空格；3) 丢掉过短的碎片。
    """
    t = unicodedata.normalize("NFKC", text or "")
    t = CITE_MARK_RX.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    if len(t) < min_len:
        return ""
    if not t[:1].isalpha():  # 丢掉 "2(d) using ..." 这类从版面位置截断的碎片
        return ""
    if "[" in t or "]" in t:  # 仍有残余编号（如 [11–15] 的变体）就整条丢弃，宁缺毋滥
        return ""
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def pack_node(n, n_fwd, cluster=None):
    ev = n.get("evidence") or {}
    why = []
    co = ev.get("co_cited_by_forward") or 0
    if co:
        why.append("被 %d/%d 篇施引文献共同引用" % (co, n_fwd))
    if ev.get("shared_refs"):
        why.append("与本文共享 %d 篇参考文献" % ev["shared_refs"])
    if (ev.get("s2") or {}).get("isInfluential"):
        why.append("S2 标注为重要引用")
    if n["role"] == "forward":
        why.append("直接引用本文")
    out = {
        "nid": n["nid"],
        "title": n.get("title"),
        "year": n.get("year"),
        "venue": n.get("venue"),
        "authors": (n.get("authors") or [])[:2],
        "cites": n.get("cited_by_count"),
        "score": n["scores"].get("total"),
        "label": (n.get("labels") or [""])[0],
        "role": n["role"],
        "text_source": n.get("text_source", "abstract"),
        "why": "；".join(why),
        "contexts": [c for c in (sanitize_context(x)
                                 for x in ((ev.get("s2") or {}).get("contexts") or []))
                     if c][:2],
    }
    if cluster:
        out["cluster"] = cluster.get("id")
        out["cluster_name"] = cluster.get("name")
    return out


def load_labels(run_dir):
    """agent 可选写入的关系标签：{ "37": "方法", ... }。缺失时回退规则标签。"""
    p = os.path.join(run_dir, "labels.json")
    if not os.path.exists(p):
        return {}
    try:
        d = read_json(p)
        return {str(k): str(v) for k, v in (d or {}).items()}
    except Exception:
        return {}


def load_clusters(run_dir):
    """读 clusters.json，并用 names.json（agent 起的中文名）覆盖自动名。"""
    p = os.path.join(run_dir, "clusters.json")
    if not os.path.exists(p):
        return None
    d = read_json(p)
    names = {}
    np_ = os.path.join(run_dir, "names.json")
    if os.path.exists(np_):
        names = read_json(np_)
    for c in d.get("clusters", []):
        if str(c["id"]) in names:
            c["name_zh"] = names[str(c["id"])]
            c["name"] = names[str(c["id"])]
    return d


def build_pack(cfg, graph, top=8, max_nodes=40, run_dir=None):
    nodes = graph["nodes"]
    seed = nodes[0]
    others = [n for n in nodes if n["nid"] != 0]
    fwd = [n for n in others if n["role"] == "forward"]
    back = [n for n in others if n["role"] == "backward"]
    n_fwd = len(fwd)

    by_score = lambda n: -n["scores"].get("total", 0)
    hubs = sorted([n for n in back if (n["evidence"].get("co_cited_by_forward") or 0) >= 3],
                  key=by_score)[:top]
    recent = sorted([n for n in back if (n.get("year") or 0) >= 2015], key=by_score)[:top]
    foundation = sorted([n for n in back if (n.get("year") or 9999) <= 2010],
                        key=lambda n: -(n.get("cited_by_count") or 0))[:5]
    fwd_top = sorted(fwd, key=by_score)[:top]
    overall = sorted(others, key=by_score)[:top]

    method = sorted([n for n in back if (n.get("labels") or [""])[0] == "method"],
                    key=by_score)[:5]

    clusters = load_clusters(run_dir) if run_dir else None
    cl_of = {}
    cluster_list = []
    if clusters:
        for c in clusters.get("clusters", []):
            if c.get("name") == "未归类" or c["size"] < 2:
                continue
            keep = [nid for nid in c["nids"] if nid in {x["nid"] for x in back}]
            if keep:
                cluster_list.append({"id": c["id"], "name": c.get("name"), "size": len(keep),
                                     "nids": keep})
                for nid in keep:
                    cl_of[nid] = c

    selected, seen = [], set()
    by_id_score = {n["nid"]: n["scores"].get("total", 0) for n in back}
    by_nid = {n["nid"]: n for n in nodes}
    cluster_reps = []
    for c in cluster_list:
        members = sorted(c["nids"], key=lambda nid: -(by_id_score.get(nid) or 0))[:2]
        cluster_reps.extend(by_nid[nid] for nid in members if nid in by_nid)
    for group in (hubs, method, cluster_reps, recent, foundation, fwd_top, overall):
        for n in group:
            if n["nid"] in seen:
                continue
            seen.add(n["nid"])
            selected.append(n)
    selected = selected[:max_nodes]
    final_ids = {n["nid"] for n in selected}

    groups = {
        "foundation": [n["nid"] for n in foundation if n["nid"] in final_ids],
        "method": [n["nid"] for n in method if n["nid"] in final_ids],
        "recent": [n["nid"] for n in recent if n["nid"] in final_ids],
        "hubs": [n["nid"] for n in hubs if n["nid"] in final_ids],
        "forward": [n["nid"] for n in fwd_top if n["nid"] in final_ids],
        "top_overall": [n["nid"] for n in overall if n["nid"] in final_ids],
        "clusters": [{"id": c["id"], "name": c["name"],
                      "size": c["size"],  # 簇的真实规模
                      "packed": len([nid for nid in c["nids"] if nid in final_ids]),
                      "nids": [nid for nid in c["nids"] if nid in final_ids]}
                     for c in cluster_list],
    }

    packed = [pack_node(n, n_fwd, cl_of.get(n["nid"])) for n in selected]

    # 可选：agent 写入的 labels.json 覆盖规则标签（没写就用规则结果）
    agent_labels = load_labels(run_dir) if run_dir else {}
    if agent_labels:
        for n in packed:
            v = agent_labels.get(str(n["nid"]))
            if v:
                n["label"] = v
                n["label_source"] = "agent"
    label_source = "agent" if agent_labels else "rule"

    stats = dict(graph.get("stats") or {})
    stats["co_cited_max"] = max([(n["evidence"].get("co_cited_by_forward") or 0) for n in back] or [0])
    stats["forward_zero_cited"] = sum(1 for n in fwd if not (n.get("cited_by_count") or 0))
    stats["sampling"] = {
        "forward_total": stats.get("forward_total") or n_fwd,
        "forward_fetched": stats.get("forward_fetched") or n_fwd,
        "forward_sampled": n_fwd,
        "complete": (stats.get("forward_fetched") or n_fwd) >= (stats.get("forward_total") or n_fwd),
        "duplicates": stats.get("duplicates_merged", 0),
    }
    years = [n.get("year") for n in back if n.get("year")]
    stats["backward_year_span"] = [min(years), max(years)] if years else []

    form = (graph.get("meta") or {}).get("form")
    caveats = []
    if n_fwd == 0:
        caveats.append("本文暂无施引文献：共被引不可用，相关度只来自文本相似度与文献耦合（共享参考文献）")
    else:
        if n_fwd < 30:
            caveats.append("本文施引文献仅 %d 条，第 3 段不得写“后续影响/引发转向”，只能写“同期进展或尚未形成下游”" % n_fwd)
        if stats.get("co_cited_max", 0) <= 2:
            caveats.append("共被引最高仅 %d 次（施引文献仅 %d 篇），枢纽判断证据偏弱"
                           % (stats.get("co_cited_max", 0), n_fwd))
        caveats.append("共被引基于 %d 篇施引文献的参考文献统计，样本小，只能作趋势提示" % n_fwd)
    fwd_total = stats.get("forward_total") or n_fwd
    fwd_fetched = stats.get("forward_fetched") or n_fwd
    if form == "standard":
        caveats.append("本文有 %d 条施引：可以写“已经出现的几条跟进”，但不要写“领域转向/奠基性影响”" % fwd_total)
    if fwd_fetched < fwd_total:
        caveats.append("前向共 %d 条，这里只取被引最高的 %d 条；叙述不得把这 %d 条当作全部"
                       % (fwd_total, fwd_fetched, fwd_fetched))
    if stats.get("duplicates_merged"):
        caveats.append("已合并 %d 组重复版本（同标题的预印本/正式版），节点数是合并后的" % stats["duplicates_merged"])
    errs = (graph.get("meta") or {}).get("errors") or []
    if any("semanticscholar" in (e.get("url") or "") for e in errs):
        caveats.append("Semantic Scholar 本次未返回（多为限流）：没有引用语境与 isInfluential 信号，影响力项为 0")
    title_only = [n for n in packed if n.get("text_source") == "title"]
    if title_only:
        caveats.append("%d 个节点无摘要，其文本相似度只基于标题，与其他节点不可直接比较" % len(title_only))
    if stats.get("backward_truncated"):
        caveats.append("原文参考文献共 %d 篇，这里只取前 %d 篇；分簇与叙述只覆盖这 %d 篇"
                       % (stats.get("backward_total"), stats.get("backward"),
                          stats.get("backward")))
    caveats.append("标签由规则自动生成，仅作阅读提示，不是事实断言")
    if (graph.get("meta") or {}).get("text_source") == "title-only":
        caveats.append("本文在 OpenAlex / Semantic Scholar / Crossref 均无摘要，文本相似度仅基于标题，相关度主要由文献耦合/共被引承担")
    ctx_n = sum(len(n["contexts"]) for n in packed)
    if ctx_n:
        caveats.append("本包收录引用语境 %d 条，原文方括号编号已删除，复述时不得补回" % ctx_n)
        caveats.append("语境来自 PDF 抽取，存在下标断裂（如 Si 3 N 4）与断词，引用时必须改写，不得照抄")

    return {
        "meta": {
            "seed_id": (graph.get("meta") or {}).get("seed_id"),
            "seed_doi": (graph.get("meta") or {}).get("seed_doi"),
            "generated_at": now_iso(),
            "form": form,
            "label_source": label_source,
            "pack_version": 1,
            "schema_version": 1,
        },
        "seed": {
            "nid": 0,
            "title": seed.get("title"),
            "year": seed.get("year"),
            "venue": seed.get("venue"),
            "authors": (seed.get("authors") or [])[:3],
            "cited_by_count": seed.get("cited_by_count"),
            "abstract": (seed.get("abstract") or "")[:900],
        },
        "stats": stats,
        "groups": groups,
        "nodes": packed,
        "caveats": caveats,
        "constraints": {
            "language": "zh",
            "citation_style": "[n]",
            "allowed_nids": [n["nid"] for n in selected],
            "rules": [
                "每个 [n] 必须是 allowed_nids 中的编号，不得出现未登记的文献",
                "所有数字（被引、年份、篇数）只能取自 stats 或 nodes，不得估算",
                "不得写“证明了/驳斥了”，除非 contexts 有原文；即使有，也改写成“被用于/被引作”",
                "contexts 中的方括号编号已被移除，复述时不得补回，也不得沿用其编号",
                "找不到证据写“未检索到”，不写“可能/也许”",
                "分组内按 score 降序，不重排",
            ],
            "outline": [
                {"section": 1, "title": "这篇论文在做什么，接的是哪条线", "use": ["seed", "foundation", "recent"]},
                {"section": 2, "title": "方法来源：它站在谁的肩膀上", "use": ["clusters", "foundation", "method", "hubs"]},
                {"section": 3, "title": "后续与同期：验证、扩展与竞争", "use": ["forward", "hubs"]},
                {"section": 4, "title": "仍未解决的空白", "use": ["stats", "caveats"]},
            ],
        },
    }


def cmd_pack(args, cfg):
    graph = read_json(os.path.join(args.run, "graph.json"))
    pack = build_pack(cfg, graph, top=args.top, max_nodes=args.max_nodes, run_dir=args.run)
    path = os.path.join(args.run, "pack.json")
    write_json(path, pack)
    size = os.path.getsize(path) / 1024.0
    g = pack["groups"]
    if cfg.get("_json"):
        print(json.dumps({
            "schema_version": 1, "run": args.run, "pack": path,
            "nodes": len(pack["nodes"]), "size_kb": round(size, 1),
            "allowed_nids": pack["constraints"]["allowed_nids"],
            "clusters": [{"id": c["id"], "name": c["name"], "size": c["size"], "packed": c["packed"]}
                         for c in g.get("clusters", [])],
            "caveats": pack["caveats"], "label_source": pack["meta"].get("label_source"),
        }, ensure_ascii=False))
        return 0
    print("分组: 奠基 %d | 近十年 %d | 枢纽 %d | 前向 %d | 总榜 %d"
          % (len(g["foundation"]), len(g["recent"]), len(g["hubs"]),
             len(g["forward"]), len(g["top_overall"])))
    print("节点: %d 个（上限 %d）| 体积 %.1f KB | 语境 %d 条"
          % (len(pack["nodes"]), args.max_nodes, size,
             sum(len(n["contexts"]) for n in pack["nodes"])))
    print("标签来源: %s%s" % (pack["meta"].get("label_source"),
                             "（labels.json）" if pack["meta"].get("label_source") == "agent" else "（规则兜底）"))
    print("告警: %d 条" % len(pack["caveats"]))
    for c in pack["caveats"]:
        print("  ·", c)
    print("输出:", path)
    return 0


# ---------------------------------------------------------------- 分簇（方案 D）

CLUSTER_STOP = set("""
study studies effect effects using used use based via toward towards new high low
analysis approach method methods system systems review overview progress recent
their its this that from with into for and the of in on by to a an
""".split())


def coupling_edges(nodes, min_shared=2):
    """后向节点两两之间的文献耦合强度（共享参考文献数）。"""
    ids = [n["nid"] for n in nodes if n["role"] == "backward"]
    refs = {n["nid"]: set(n.get("referenced_works") or []) for n in nodes}
    edges = {}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            inter = len(refs[a] & refs[b])
            if inter >= min_shared:
                edges[(a, b)] = float(inter)
    return ids, edges


def greedy_modularity(ids, edges, max_clusters=8, resolution=1.0):
    """朴素贪心模块度合并（n 很小，O(n^3) 足够），返回 {nid: cluster_id}。
    resolution > 1 倾向切得更细（密集引用图需要）。"""
    if not ids:
        return {}
    m = sum(edges.values()) or 1.0
    comm = {nid: nid for nid in ids}
    members = {nid: {nid} for nid in ids}
    degree = {nid: 0.0 for nid in ids}
    for (a, b), w in edges.items():
        degree[a] += w
        degree[b] += w
    strength = dict(degree)

    def pair_weights():
        """当前划分下，各社区对之间的连接权重。每次迭代 O(E)，避免 O(E·C²)。"""
        pw = {}
        for (a, b), w in edges.items():
            ca, cb = comm[a], comm[b]
            if ca == cb:
                continue
            k = (ca, cb) if ca < cb else (cb, ca)
            pw[k] = pw.get(k, 0.0) + w
        return pw

    # 节点数少于 max_clusters 时不该被“下限”卡住；否则小图永远不合并
    floor = max_clusters if len(ids) > max_clusters else 1
    while len(members) > floor:
        best, pair = 0.0, None
        for (c1, c2), w in pair_weights().items():
            dq = w / m - resolution * (strength[c1] * strength[c2]) / (2.0 * m * m)
            if dq > best:
                best, pair = dq, (c1, c2)
        if not pair or best <= 1e-6:
            break
        c1, c2 = pair
        members[c1] |= members[c2]
        del members[c2]
        strength[c1] += strength[c2]
        strength.pop(c2, None)
        for nid in list(comm):
            if comm[nid] == c2:
                comm[nid] = c1
    return comm


def _bigrams(text):
    toks = [t for t in tokenize(text) if t not in CLUSTER_STOP]
    return [toks[i] + " " + toks[i + 1] for i in range(len(toks) - 1)]


def cluster_name(members, nodes_by_id, global_bi):
    """优先用 bigram（water saving / crop yield），没有再用单词。"""
    counts = {}
    for nid in members:
        for b in set(_bigrams(nodes_by_id[nid].get("title") or "")):
            counts[b] = counts.get(b, 0) + 1
    need = max(2, len(members) // 6)
    scored = sorted(((c - 0.6 * global_bi.get(b, 0), b) for b, c in counts.items() if c >= need),
                    reverse=True)
    names = [b for _, b in scored[:2]]
    if names:
        return " / ".join(names)

    counts = {}
    for nid in members:
        for t in set(tokenize(nodes_by_id[nid].get("title") or "")):
            if t in CLUSTER_STOP or t.isdigit() or len(t) < 4:
                continue
            counts[t] = counts.get(t, 0) + 1
    scored = sorted(((c, t) for t, c in counts.items() if c >= need), reverse=True)
    return " / ".join(t for _, t in scored[:3]) if scored else "(未命名簇)"


def _cos(a, b):
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    num = sum(w * b.get(t, 0.0) for t, w in a.items())
    if num <= 0:
        return 0.0
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    return num / (na * nb) if na and nb else 0.0


def cmd_cluster(args, cfg):
    graph = read_json(os.path.join(args.run, "graph.json"))
    nodes = graph["nodes"]
    by_id = {n["nid"]: n for n in nodes}
    ids, edges = coupling_edges(nodes, min_shared=args.min_shared)
    if args.resolution and args.resolution > 0:
        comm = greedy_modularity(ids, edges, max_clusters=args.max_clusters, resolution=args.resolution)
        used_res = args.resolution
    else:
        # 自动分辨率：引用密集的领域（P2）在 1.0 下会并成 27 篇的大团，
        # 逐步加大分辨率，直到最大簇不超过 35% 且至少有 4 个簇。
        comm, used_res, best_share = None, 1.0, 1.0
        for r in (1.0, 1.3, 1.6, 2.0, 2.4, 2.8):
            c = greedy_modularity(ids, edges, max_clusters=args.max_clusters, resolution=r)
            g = {}
            for nid, cid in c.items():
                g.setdefault(cid, []).append(nid)
            sizes = sorted((len(v) for v in g.values()), reverse=True)
            share = sizes[0] / float(len(ids)) if sizes else 1.0
            if comm is None or share < best_share:
                comm, best_share, used_res = c, share, r
            if share <= 0.35 and len([s for s in sizes if s >= 2]) >= 4:
                break

    groups = {}
    for nid, cid in comm.items():
        groups.setdefault(cid, []).append(nid)
    multi = [m for m in groups.values() if len(m) >= 2]
    singles = [m[0] for m in groups.values() if len(m) == 1]

    # 单点簇：按标题/摘要的 IDF 余弦挂到最近的簇；挂不上就进"未归类"
    texts = [tokenize((by_id[nid].get("title") or "") + " " + (by_id[nid].get("abstract") or "")) for nid in ids]
    idf = idf_weights(texts)
    vec = {nid: {t: idf.get(t, 1.0) for t in set(txt)} for nid, txt in zip(ids, texts)}
    cents = []
    for members in multi:
        c = {}
        for nid in members:
            for t, w in vec[nid].items():
                c[t] = c.get(t, 0.0) + w
        cents.append(c)

    assigned = 0
    unassigned = []
    for nid in singles:
        best, bi = 0.0, -1
        for i, c in enumerate(cents):
            s = _cos(vec[nid], c)
            if s > best:
                best, bi = s, i
        if bi >= 0 and best >= args.text_min:
            multi[bi].append(nid)
            assigned += 1
        else:
            unassigned.append(nid)

    global_bi = {}
    for n in nodes:
        for b in set(_bigrams(n.get("title") or "")):
            global_bi[b] = global_bi.get(b, 0) + 1

    clusters = []
    for members in sorted(multi, key=lambda m: -len(m)):
        members.sort(key=lambda nid: -(by_id[nid]["scores"].get("total") or 0))
        clusters.append({
            "id": len(clusters),
            "size": len(members),
            "name": cluster_name(members, by_id, global_bi),
            "assigned_by": "modularity+text",
            "nids": members,
            "reps": [(nid, by_id[nid].get("title"), by_id[nid].get("year")) for nid in members[:3]],
        })
    if unassigned:
        clusters.append({
            "id": len(clusters),
            "size": len(unassigned),
            "name": "未归类",
            "assigned_by": "none",
            "nids": unassigned,
            "reps": [(nid, by_id[nid].get("title"), by_id[nid].get("year")) for nid in unassigned[:3]],
        })

    node_cluster = {}
    for c in clusters:
        for nid in c["nids"]:
            node_cluster[str(nid)] = c["id"]
    out = {"schema_version": 1, "clusters": clusters, "node_cluster": node_cluster}
    write_json(os.path.join(args.run, "clusters.json"), out)

    print("后向 %d 个｜耦合边 %d｜分辨率 %.1f｜模块度簇 %d｜文本归位 %d｜未归类 %d"
          % (len(ids), len(edges), used_res, len(multi), assigned, len(unassigned)))
    for c in clusters:
        print("\n[%d] %-42s %d 篇" % (c["id"], c["name"][:42], c["size"]))
        for nid, title, year in c["reps"]:
            print("     - %s (%s)" % (short_title(title, 64), year))
    print("\n输出:", os.path.join(args.run, "clusters.json"))
    return 0


# ---------------------------------------------------------------- lint

FORBIDDEN = ["证明了", "驳斥", "引发", "领域转向", "首次", "突破", "领先", "显著优于", "颠覆"]


def lint_narrative(pack, text):
    errors, warnings = [], []
    allowed = set(pack["constraints"]["allowed_nids"])
    sections = re.split(r"^## ", text, flags=re.M)[1:]
    cited = set()
    sec_heads, sec_nids = [], []
    for sec in sections:
        head = sec.splitlines()[0].strip() if sec.splitlines() else "(无标题)"
        nids = [int(x) for x in re.findall(r"\[(\d+)\]", sec)]
        sec_heads.append(head)
        sec_nids.append(nids)
        cited.update(nids)
        for n in nids:
            if n not in allowed:
                errors.append({"type": "unknown_citation", "nid": n, "section": head})
        if not nids:
            errors.append({"type": "no_citation_in_section", "section": head})

    for phrase in FORBIDDEN:
        if phrase in text:
            errors.append({"type": "forbidden_phrase", "phrase": phrase})

    # 数字必须能在 pack 里找到出处，杜绝凭印象写"约 50 篇"
    blob = json.dumps(pack, ensure_ascii=False)
    allowed_nums = {int(x) for x in re.findall(r"\d+", blob)}
    for m in re.finditer(r"(\d+)\s*(篇|条|次)", text):
        v = int(m.group(1))
        if v not in allowed_nums:
            errors.append({"type": "number_not_in_pack", "value": v, "unit": m.group(2),
                           "context": text[max(0, m.start() - 24): m.end() + 8]})

    # 分簇覆盖：大簇（≥3 篇）整条没写 → 错误；小簇没写 → 告警
    clusters = (pack.get("groups") or {}).get("clusters") or []
    covered = 0
    for c in clusters:
        nids = c.get("nids") or []
        hit = [nid for nid in nids if nid in cited]
        if hit:
            covered += 1
        elif (c.get("size") or len(nids)) >= 3:
            errors.append({"type": "cluster_uncovered", "cluster": c.get("name"),
                           "size": c.get("size") or len(nids)})
        elif nids:
            warnings.append({"type": "cluster_uncovered_small", "cluster": c.get("name"),
                             "size": c.get("size") or len(nids)})

    # 引用方向：只查“方法来源”段——那里如果把施引文献当成前身，多半是搞反了。
    # （第 3 段引用后向节点是正常的：叙述会提到“这些施引文献反复引用 X”。）
    role_of = {n["nid"]: n.get("role") for n in pack.get("nodes", [])}
    for i, head in enumerate(sec_heads):
        if "方法来源" not in head:
            continue
        bad = [nid for nid in sec_nids[i]
               if role_of.get(nid) == "forward"]
        if bad:
            warnings.append({"type": "direction_mismatch", "section": head[:18], "nids": bad})

    orphans = sorted(allowed - cited)
    if orphans:
        warnings.append({"type": "orphan_node", "nids": orphans})
    return {"ok": not errors, "schema_version": 1, "sections": len(sections), "cited": len(cited),
            "allowed": len(allowed), "clusters_total": len(clusters), "clusters_covered": covered,
            "errors": errors, "warnings": warnings}


def cmd_lint(args, cfg):
    run = args.run
    pack = read_json(os.path.join(run, "pack.json"))
    path = args.narrative or os.path.join(run, "narrative.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    res = lint_narrative(pack, text)
    write_json(os.path.join(run, "lint.json"), res)
    if cfg.get("_json"):
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1
    print("段落 %d | 引用节点 %d/%d | 簇 %d/%d | 错误 %d | 告警 %d"
          % (res["sections"], res["cited"], res["allowed"],
             res.get("clusters_covered", 0), res.get("clusters_total", 0),
             len(res["errors"]), len(res["warnings"])))
    for e in res["errors"][:12]:
        print("  ✗", json.dumps(e, ensure_ascii=False))
    for w in res["warnings"][:5]:
        print("  !", json.dumps(w, ensure_ascii=False))
    print("结果:", "通过" if res["ok"] else "未通过", "|", os.path.join(run, "lint.json"))
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------- 布局与渲染

def force_layout(nodes, edges, size=1000, iters=700, seed=7, y_targets=None):
    """Fruchterman–Reingold，确定性：固定初始环 + 固定随机种子 + seed 钉在中心。
    浏览器不做力学计算，因此同一份输入每次跑出同一张图。
    y_targets: 角色纵向分带（前向在上、后向在下），用软约束而不是硬裁剪，保留局部结构。"""
    rnd = random.Random(seed)
    n = len(nodes)
    ids = [nd["nid"] for nd in nodes]
    pos = {}
    for i, nid in enumerate(ids):
        ang = 2 * math.pi * i / max(1, n)
        r = size * 0.30 * (0.85 + 0.3 * rnd.random())
        pos[nid] = [size / 2 + r * math.cos(ang), size / 2 + r * math.sin(ang)]
    pos[0] = [size / 2, size / 2]
    y_targets = y_targets or {}

    links = [(e["from"], e["to"], 1.0 if e["type"] == "cites" else 0.25)
             for e in edges if e["from"] in pos and e["to"] in pos]
    k = math.sqrt(size * size / max(1, n)) * 0.75
    t = size * 0.12
    for _ in range(iters):
        disp = {nid: [0.0, 0.0] for nid in ids}
        for i in range(n):
            a = ids[i]
            ax, ay = pos[a]
            for j in range(i + 1, n):
                b = ids[j]
                dx, dy = ax - pos[b][0], ay - pos[b][1]
                d2 = dx * dx + dy * dy
                if d2 < 0.01:
                    dx, dy, d2 = rnd.uniform(-1, 1), rnd.uniform(-1, 1), 1.0
                d = math.sqrt(d2)
                f = k * k / d
                ux, uy = dx / d, dy / d
                disp[a][0] += ux * f
                disp[a][1] += uy * f
                disp[b][0] -= ux * f
                disp[b][1] -= uy * f
        for s, tt, wgt in links:
            dx, dy = pos[s][0] - pos[tt][0], pos[s][1] - pos[tt][1]
            d = math.hypot(dx, dy) or 0.01
            f = d * d / k * wgt
            ux, uy = dx / d, dy / d
            disp[s][0] -= ux * f
            disp[s][1] -= uy * f
            disp[tt][0] += ux * f
            disp[tt][1] += uy * f
        for nid in ids:
            dx = pos[nid][0] - size / 2
            disp[nid][0] -= dx * 0.012
            if nid in y_targets:
                disp[nid][1] += (y_targets[nid] - pos[nid][1]) * 0.06
            else:
                dy = pos[nid][1] - size / 2
                disp[nid][1] -= dy * 0.012
        for nid in ids:
            if nid == 0:
                continue
            dx, dy = disp[nid]
            d = math.hypot(dx, dy) or 1.0
            step = min(d, t)
            pos[nid][0] += dx / d * step
            pos[nid][1] += dy / d * step
        t *= 0.985

    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    s = min(860.0 / max(1e-6, (x1 - x0)), 860.0 / max(1e-6, (y1 - y0)))
    for nid in pos:
        pos[nid][0] = 70 + (pos[nid][0] - x0) * s
        pos[nid][1] = 70 + (pos[nid][1] - y0) * s
    dx, dy = 500 - pos[0][0], 500 - pos[0][1]
    for nid in pos:
        pos[nid][0] = min(960, max(40, pos[nid][0] + dx))
        pos[nid][1] = min(960, max(40, pos[nid][1] + dy))
    return {str(nid): [round(p[0], 1), round(p[1], 1)] for nid, p in pos.items()}


def year_layout(nodes):
    """时间轴（极坐标）：seed 在圆心，后向沿下半圆、前向沿上半圆，
    角度按年份从老到新排，半径按综合分轻微收放。比直角坐标的年份轴更省地方，
    因为 1962–2010 那一段本来就很空。"""
    out = {"0": [500.0, 500.0]}
    back = sorted([n for n in nodes if n["role"] == "backward"],
                  key=lambda n: (n.get("year") or 0, -(n.get("scores") or {}).get("total", 0)))
    fwd = sorted([n for n in nodes if n["role"] == "forward"],
                 key=lambda n: (n.get("year") or 0, -(n.get("scores") or {}).get("total", 0)))
    rings = ((back, 168.0, 12.0), (fwd, 192.0, 348.0))
    for group, a0, a1 in rings:
        m = len(group)
        for i, n in enumerate(group):
            frac = i / float(m - 1) if m > 1 else 0.5
            theta = math.radians(a0 + (a1 - a0) * frac)
            total = min(1.0, (n.get("scores") or {}).get("total", 0) / 0.65)
            r = 315 + 95 * (1 - total)
            out[str(n["nid"])] = [round(500 + r * math.cos(theta), 1),
                                  round(500 + r * math.sin(theta), 1)]
    return out


def apply_bands(pos, nodes):
    """把力导向结果的 y 重新映射到角色带：前向在上半区、后向在下半区。
    归一化会把分带压扁，所以必须在布局之后再做一次单调映射。"""
    out = dict(pos)
    for role, lo, hi in (("forward", 110, 430), ("backward", 570, 930)):
        group = [n for n in nodes if n["role"] == role and str(n["nid"]) in pos]
        if not group:
            continue
        group.sort(key=lambda n: pos[str(n["nid"])][1])
        m = len(group)
        for i, n in enumerate(group):
            p = list(pos[str(n["nid"])])
            frac = i / float(m - 1) if m > 1 else 0.5
            p[1] = round(lo + frac * (hi - lo), 1)
            out[str(n["nid"])] = p
    out["0"] = [out.get("0", [500, 500])[0], 500.0]
    return out


def cmd_layout(args, cfg):
    graph = read_json(os.path.join(args.run, "graph.json"))
    nodes = graph["nodes"]
    roles = {n["nid"]: n["role"] for n in nodes}
    targets = {nid: (230 if r == "forward" else 780) for nid, r in roles.items() if nid != 0}
    net = force_layout(nodes, graph["edges"], y_targets=targets)
    net = apply_bands(net, nodes)
    year = year_layout(nodes)
    write_json(os.path.join(args.run, "layout.json"), {"net": net, "year": year})
    print("布局: 网络 %d 点 / 年份 %d 点" % (len(net), len(year)))
    print("输出:", os.path.join(args.run, "layout.json"))
    return 0


def md_inline(s):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[(\d+)\]", r'<a class="cite" data-nid="\1">[\1]</a>', s)
    return s


def narrative_sections(text):
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    out = []
    for p in parts:
        lines = p.splitlines()
        title = lines[0].strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        paras = []
        for block in re.split(r"\n\s*\n", body):
            b = block.strip()
            if not b or b.startswith("---") or b.startswith("*本文由"):
                continue
            paras.append(md_inline(b.replace("\n", " ")))
        out.append({"title": title, "paras": paras})
    return out


def render_node(n, net, year):
    nid = n["nid"]
    s = n.get("scores") or {}
    rel = 1.0 if nid == 0 else min(1.0, max(0.0, 0.6 * s.get("text", 0) + 0.4 * s.get("cocite", 0)))
    ev = n.get("evidence") or {}
    why = []
    if ev.get("co_cited_by_forward"):
        why.append("被 %d 篇施引文献共同引用" % ev["co_cited_by_forward"])
    if ev.get("shared_refs"):
        why.append("与本文共享 %d 篇参考文献" % ev["shared_refs"])
    if (ev.get("s2") or {}).get("isInfluential"):
        why.append("S2 标注为重要引用")
    if n.get("role") == "forward":
        why.append("直接引用本文")
    x, y = net.get(str(nid), [500, 500])
    x2, y2 = year.get(str(nid), [x, y])
    return {
        "nid": nid,
        "title": n.get("title"),
        "year": n.get("year"),
        "venue": n.get("venue"),
        "authors": (n.get("authors") or [])[:3],
        "author_count": n.get("author_count") or len(n.get("authors") or []),
        "cites": n.get("cited_by_count") or 0,
        "doi": n.get("doi"),
        "role": n.get("role"),
        "label": (n.get("labels") or [""])[0],
        "why": "；".join(why),
        "contexts": [sanitize_context(c) for c in ((ev.get("s2") or {}).get("contexts") or [])][:2],
        "abstract": (n.get("abstract") or "")[:420],
        "rel": round(rel, 4),
        "text": round(s.get("text", 0), 4),
        "cocite": round(s.get("cocite", 0), 4),
        "score": round(s.get("total", 0), 4),
        "x": x,
        "y": y,
        "x2": x2,
        "y2": y2,
    }


def cmd_render(args, cfg):
    run = args.run
    graph = read_json(os.path.join(run, "graph.json"))
    lpath = os.path.join(run, "layout.json")
    if not os.path.exists(lpath):
        targets = {n["nid"]: (230 if n["role"] == "forward" else 780)
                   for n in graph["nodes"] if n["nid"] != 0}
        pos = {"net": force_layout(graph["nodes"], graph["edges"], y_targets=targets),
               "year": year_layout(graph["nodes"])}
        write_json(lpath, pos)
    else:
        pos = read_json(lpath)
        if "net" not in pos:  # 兼容旧格式
            pos = {"net": pos, "year": pos}

    npath = args.narrative or os.path.join(run, "narrative.md")
    narrative = ""
    if os.path.exists(npath):
        with open(npath, encoding="utf-8") as f:
            narrative = f.read()

    nodes = [render_node(n, pos["net"], pos["year"]) for n in graph["nodes"]]
    scored = [n for n in graph["nodes"] if n.get("scores") and n["scores"].get("total")]
    if not scored:
        print("错误：graph.json 里没有分数（可能只跑了 graph 没跑 score）。")
        print("请先执行: python citegraph.py label --run RUN && python citegraph.py score --run RUN")
        return 2
    # 相关度用百分位着色：绝对值普遍偏低，直接映射会一片浅绿，看不出层次
    ranks = pct_rank([0.6 * n["text"] + 0.4 * n["cocite"] for n in nodes[1:]])
    for i, n in enumerate(nodes[1:], start=0):
        n["rel"] = round(ranks[i], 4)
    nodes[0]["rel"] = 1.0
    edges = [{"from": e["from"], "to": e["to"], "type": e["type"]}
             for e in graph["edges"] if e["type"] in ("cites", "coupled")]

    secs = narrative_sections(narrative)
    narr_html = "\n".join(
        "<h2>%s</h2>\n%s" % (md_inline(s["title"]), "\n".join("<p>%s</p>" % p for p in s["paras"]))
        for s in secs)

    seed = nodes[0]
    stats = graph.get("stats") or {}
    co_max = max([(n.get("evidence") or {}).get("co_cited_by_forward") or 0 for n in graph["nodes"]] or [0])
    stat_line = "后向 %s · 施引 %s · 共引最高 %s · 生成 %s" % (
        stats.get("backward", "-"), stats.get("forward_total", "-"),
        co_max, (graph.get("meta") or {}).get("generated_at", "")[:10])

    top = sorted(nodes, key=lambda x: -x["score"])[:20]
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (n["nid"], n["year"] or "-", n["cites"], (n["title"] or "")[:90], n["venue"] or "-")
        for n in top)
    fallback = ("<table><thead><tr><th>#</th><th>年</th><th>被引</th><th>标题</th><th>出处</th></tr></thead>"
                "<tbody>%s</tbody></table>" % rows)

    data = {"nodes": nodes, "edges": edges,
            "sections": secs,
            "meta": {"seed": seed["title"], "doi": seed["doi"]}}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    with open(os.path.join(SKILL_DIR, "assets", "report.css"), encoding="utf-8") as f:
        css = f.read()
    with open(os.path.join(SKILL_DIR, "assets", "report.js"), encoding="utf-8") as f:
        js = f.read()
    with open(os.path.join(SKILL_DIR, "assets", "report.tmpl.html"), encoding="utf-8") as f:
        tmpl = f.read()

    html = (tmpl.replace("{{CSS}}", css).replace("{{JS}}", js)
            .replace("{{TITLE}}", seed["title"] or "citation report")
            .replace("{{SEED_TITLE}}", seed["title"] or "")
            .replace("{{STATS}}", stat_line)
            .replace("{{NARRATIVE}}", narr_html)
            .replace("{{FALLBACK}}", fallback)
            .replace("{{DATA}}", payload))

    out = args.out or os.path.join(run, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    ext = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    ext = [u for u in ext if not u.startswith("https://doi.org/")]
    size = os.path.getsize(out) / 1024.0
    print("节点 %d · 边 %d · 段落 %d · 体积 %.1f KB" % (len(nodes), len(edges), len(secs), size))
    print("外部资源: %s" % ("无" if not ext else ", ".join(ext[:3])))
    print("输出:", out)
    return 0


# ---------------------------------------------------------------- 自检与导出

def mask_key(k):
    if not k:
        return "(未设置)"
    if len(k) <= 8:
        return "***"
    return k[:4] + "…" + k[-2:]


def cmd_doctor(args, cfg):
    import platform
    print("python      :", platform.python_version())
    print("skill dir   :", SKILL_DIR)
    print("config used :", cfg.get("_config_path"))
    print("openalex key:", mask_key(cfg.get("openalex_api_key")),
          "(env)" if os.environ.get("OPENALEX_API_KEY") else "(file)")
    print("s2 key      :", mask_key(cfg.get("semantic_scholar_api_key")))
    print("free mode   :", bool(cfg.get("free_mode")))
    cache = os.environ.get("CITEGRAPH_CACHE") or os.path.join(os.getcwd(), ".cache", "citegraph.db")
    print("cache       :", cache, "(exists)" if os.path.exists(cache) else "(empty)")
    runs = os.path.join(os.getcwd(), "runs")
    n_runs = len([d for d in os.listdir(runs)]) if os.path.isdir(runs) else 0
    print("runs        :", n_runs, "个")

    bundled = os.path.join(SKILL_DIR, "config.json")
    if os.path.exists(bundled):
        try:
            b = json.load(open(bundled, encoding="utf-8"))
            if b.get("openalex_api_key"):
                print("⚠ 内置 config.json 里带了 key：如果要分发这个 skill，先把它清空并改用用户配置/环境变量")
            if b.get("email"):
                print("⚠ 内置 config.json 里带了 email：分发前建议清空")
        except Exception:
            pass
    return 0


def describe_manifest():
    return {
        "name": "citation-context",
        "version": "1.0.0",
        "input": ["DOI", "arXiv ID", "exact title"],
        "commands": {
            "resolve": {"args": ["query", "--out"], "writes": ["seed.json"]},
            "graph": {"args": ["--run", "--free-mode"], "reads": ["seed.json"],
                      "writes": ["graph.json"], "network": ["openalex", "semanticscholar"]},
            "label": {"args": ["--run"], "reads": ["graph.json"], "writes": ["graph.json"]},
            "score": {"args": ["--run", "--explain"], "reads": ["graph.json"], "writes": ["graph.json"]},
            "cluster": {"args": ["--run", "--min-shared", "--max-clusters", "--resolution", "--text-min"],
                        "reads": ["graph.json"], "writes": ["clusters.json"]},
            "pack": {"args": ["--run", "--top", "--max-nodes"], "reads": ["graph.json", "clusters.json", "names.json", "labels.json"],
                     "writes": ["pack.json"]},
            "lint": {"args": ["--run", "--narrative"], "reads": ["pack.json", "narrative.md"],
                     "writes": ["lint.json"], "exit": "0 通过 / 1 未通过"},
            "layout": {"args": ["--run"], "reads": ["graph.json"], "writes": ["layout.json"]},
            "render": {"args": ["--run", "--narrative", "--out"], "reads": ["graph.json", "layout.json", "narrative.md"],
                       "writes": ["report.html"], "artifact": "单文件、离线、零外部请求"},
        },
        "agent_inputs": {"names.json": "簇中文名 {id: name}", "labels.json": "关系标签 {nid: label}"},
        "env": ["OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "CITEGRAPH_CONFIG", "CITEGRAPH_CACHE", "CITEGRAPH_FREE_MODE"],
        "python": ">=3.9, 标准库",
    }


def cmd_describe(args, cfg):
    m = describe_manifest()
    if args.write:
        path = os.path.join(SKILL_DIR, "manifest.json")
        write_json(path, m)
        print("written:", path)
    if not args.quiet:
        print(json.dumps(m, ensure_ascii=False, indent=1))
    return 0


def cmd_export_dist(args, cfg):
    import shutil
    out = os.path.abspath(args.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    for name in ("SKILL.md", "SKILL.zh.md", "references", "scripts", "assets"):
        src = os.path.join(SKILL_DIR, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out, name),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif os.path.exists(src):
            shutil.copy2(src, os.path.join(out, name))

    example = {
        "_readme": "复制为 ~/.config/citation-context/config.json（或设 OPENALEX_API_KEY 环境变量）。不要把这个文件连同真实 key 一起提交。",
        "email": "",
        "openalex_api_key": "",
        "semantic_scholar_api_key": "",
        "free_mode": False,
        "venue_weights": cfg.get("venue_weights"),
        "weights": cfg.get("weights"),
        "text_scale": cfg.get("text_scale", 0.15),
        "limits": cfg.get("limits"),
        "cache_ttl_days": cfg.get("cache_ttl_days"),
    }
    write_json(os.path.join(out, "config.example.json"), example)
    write_json(os.path.join(out, "manifest.json"), describe_manifest())

    # 导出后自检：密钥、邮箱、本机路径都不应出现在分发文件里
    PRIVACY_PATTERNS = [
        ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
        ("api_key", re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[A-Za-z0-9]{16,}")),
        ("win_home", re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\\\s]+")),
        ("posix_home", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")),
    ]
    ALLOW_EMAIL = ("example.com", "example.org", "your@email.com")

    key = cfg.get("openalex_api_key") or ""
    s2 = cfg.get("semantic_scholar_api_key") or ""
    leaks = []
    for root, _dirs, files in os.walk(out):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                text = open(fp, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            rel = os.path.relpath(fp, out)
            if (key and key in text) or (s2 and s2 in text):
                leaks.append(rel + " (key)")
            for label, rx in PRIVACY_PATTERNS:
                for m in rx.finditer(text):
                    hit = m.group(0)
                    if label == "email" and any(a in hit for a in ALLOW_EMAIL):
                        continue
                    leaks.append("%s (%s: %s)" % (rel, label, sanitize_url(hit)[:40]))
                    break
    print("导出目录:", out)
    print("文件数:", sum(len(f) for _, _, f in os.walk(out)))
    print("密钥泄漏检查:", "未发现" if not leaks else ("发现！ " + ", ".join(leaks)))
    return 0 if not leaks else 4


def _fixture():
    def mk(nid, role, title, year, cites, refs, venue="Test Journal", vtype="journal"):
        return {"nid": nid, "role": role, "title": title, "year": year, "date": "%d-01-01" % year,
                "venue": venue, "venue_type": vtype, "type": "article",
                "doi": "10.1/test%d" % nid, "authors": ["A B"], "author_count": 1,
                "cited_by_count": cites, "reference_count": len(refs), "referenced_works": refs,
                "abstract": "abstract about " + title, "text_source": "abstract",
                "scores": {}, "labels": [], "evidence": {}}
    nodes = [
        mk(0, "seed", "seed paper about widgets", 2020, 10, ["W1", "W2", "W3", "W4"]),
        mk(1, "backward", "widget method one", 2015, 100, ["W1", "W2"]),
        mk(2, "backward", "widget method two", 2016, 80, ["W2", "W3"]),
        mk(3, "backward", "unrelated gardening study", 2010, 5, ["W9"]),
        mk(4, "forward", "widget application paper", 2021, 7, ["W1", "W2", "W3"]),
        mk(5, "forward", "unrelated application", 2021, 3, ["W8"]),
    ]
    edges = [{"from": 0, "to": 1, "type": "cites"}, {"from": 0, "to": 2, "type": "cites"},
             {"from": 0, "to": 3, "type": "cites"}, {"from": 4, "to": 0, "type": "cites"},
             {"from": 5, "to": 0, "type": "cites"}, {"from": 1, "to": 2, "type": "coupled"}]
    return nodes, edges


def cmd_selftest(args, cfg):
    """离线自检：不联网、不写文件，验证打分/分簇/打包/校验四条主链路。"""
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    cfg = dict(cfg)
    cfg.setdefault("venue_weights", dict(DEFAULT_VENUE_WEIGHTS))
    nodes, edges = _fixture()
    nodes = compute_scores(cfg, nodes, edges, nodes[0])
    by = {n["nid"]: n for n in nodes}

    check("打分范围 0–1", all(0 <= n["scores"]["total"] <= 1 for n in nodes))
    check("相关文献高于无关文献", by[1]["scores"]["total"] > by[3]["scores"]["total"],
          "%.3f vs %.3f" % (by[1]["scores"]["total"], by[3]["scores"]["total"]))
    check("前向用 coupling+sibling", by[4]["scores"].get("cocite_kind") == "coupling+sibling")
    check("后向用 coupling+cocitation", by[1]["scores"].get("cocite_kind") == "coupling+cocitation")
    check("会议权重低于期刊", venue_kind({"type": "conference-paper", "venue_type": "conference"}) == "conference")

    ids, ce = coupling_edges(nodes, min_shared=1)
    check("耦合边识别", ce.get((1, 2)) or ce.get((2, 1)))
    comm = greedy_modularity(ids, ce, max_clusters=8)
    check("分簇把 1/2 归到一起", comm.get(1) == comm.get(2))

    graph = {"nodes": nodes, "edges": edges,
             "stats": {"backward": 3, "forward_total": 2, "forward_sampled": 2,
                       "forward_fetched": 2, "nodes": 6, "edges": 6},
             "meta": {"form": "standard", "text_source": "abstract"}}
    pack = build_pack(cfg, graph)
    check("pack 有 allowed_nids", len(pack["constraints"]["allowed_nids"]) > 0)
    check("pack 带 schema_version", pack["meta"].get("schema_version") == 1)

    bad = lint_narrative(pack, "## 1. 测试\n\n这段话引用了 [999]。\n")
    check("lint 能抓未知编号", any(e["type"] == "unknown_citation" for e in bad["errors"]))
    good = lint_narrative(pack, "## 1. 测试\n\n这段话正确地引用了 [1]。\n")
    check("lint 接受合法编号", good["ok"])

    print("selftest: %d 项" % len(checks))
    for name, ok_, detail in checks:
        print("  %s %s%s" % ("✓" if ok_ else "✗", name, ("  (%s)" % detail) if detail else ""))
    failed = [c for c in checks if not c[1]]
    print("结果:", "PASS" if not failed else "FAIL (%d)" % len(failed))
    return 0 if not failed else 1


def main():
    ap = argparse.ArgumentParser(prog="citegraph")
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true",
                    help="机器可读输出（resolve / pack / lint）；其他命令仍打印人类文本")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve")
    p.add_argument("query")
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("graph")
    p.add_argument("--run", required=True)
    p.add_argument("--free-mode", action="store_true",
                   help="用单实体查询代替批量 filter：计费≈0，但请求数变多（约 6 秒/50 篇）")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("score")
    p.add_argument("--run", required=True)
    p.add_argument("--explain", action="store_true")
    p.set_defaults(fn=cmd_score)

    p = sub.add_parser("label")
    p.add_argument("--run", required=True)
    p.set_defaults(fn=cmd_label)

    p = sub.add_parser("export")
    p.add_argument("--run", required=True)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("pack")
    p.add_argument("--run", required=True)
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--max-nodes", type=int, default=40)
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("cluster")
    p.add_argument("--run", required=True)
    p.add_argument("--min-shared", type=int, default=2)
    p.add_argument("--max-clusters", type=int, default=8)
    p.add_argument("--resolution", type=float, default=0.0,
                   help="模块度分辨率；0 = 自动（推荐），>1 切得更细")
    p.add_argument("--text-min", type=float, default=0.08,
                   help="单点簇按文本相似度归位的阈值，低于此值进“未归类”")
    p.set_defaults(fn=cmd_cluster)

    p = sub.add_parser("lint")
    p.add_argument("--run", required=True)
    p.add_argument("--narrative", default=None)
    p.set_defaults(fn=cmd_lint)

    p = sub.add_parser("layout")
    p.add_argument("--run", required=True)
    p.set_defaults(fn=cmd_layout)

    p = sub.add_parser("render")
    p.add_argument("--run", required=True)
    p.add_argument("--narrative", default=None)
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("doctor")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("describe")
    p.add_argument("--write", action="store_true", help="同时写入 skill 目录下的 manifest.json")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_describe)

    p = sub.add_parser("export-dist")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_export_dist)

    p = sub.add_parser("selftest")
    p.set_defaults(fn=cmd_selftest)

    args = ap.parse_args()
    cfg = load_config(args.config)
    cfg["_json"] = bool(getattr(args, "json", False))
    try:
        return args.fn(args, cfg)
    except RuntimeError as e:
        eprint("错误：", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
