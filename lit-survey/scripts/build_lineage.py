#!/usr/bin/env python3
"""Collect a reference graph around selected papers.

Start from OpenAlex seed IDs and optional candidates from find_seeds.py. Fetch
S2 reference lists, count reference occurrences and suggest reading tiers.
Expansion follows references to earlier work; search for later papers separately.

Nodes prefer arXiv IDs, then DOIs, then shortened S2 IDs. Check paper identities
before interpreting the graph, since records from different sources can differ.

``--budget`` caps measured papers. Identity lookups, metadata refreshes and
reference pagination can add requests. API quotas and retries affect runtime.

Outputs (inside --workdir)
--------------------------
    lineage.json    collected nodes, references and suggested tiers
    timeline.md     tables ordered by year

Subcommands
-----------
    build           collect references and write outputs
    show            print selected node summaries
    tier            record the complete chosen backbone set

Examples
--------
    python3 build_lineage.py --workdir rag --seeds W4389984066,W3098425262
    python3 build_lineage.py show --workdir rag --ids arXiv:2005.11401
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oalib as oa      # noqa: E402
import s2lib as s2      # noqa: E402

JUNK_TITLE_RE = re.compile(
    r"(consciousness|paradigm shift|revolutioniz|game.?chang|"
    r"unprecedented|breakthrough framework|holistic framework)", re.I)


# --------------------------------------------------------------------------- #
# node model
# --------------------------------------------------------------------------- #
def node_id_for(arxiv_id, doi, s2_id) -> str:
    if arxiv_id:
        return f"arXiv:{arxiv_id}"
    if doi and not doi.lower().startswith("10.48550/arxiv."):
        return f"DOI:{doi.lower()}"
    if doi:
        return f"arXiv:{doi.lower().split('arxiv.', 1)[1]}"
    return f"S2:{(s2_id or 'unknown')[:8]}"


def s2_paper_to_node(p: dict, *, source: str = "discovered") -> dict | None:
    ext = p.get("externalIds") or {}
    arxiv = ext.get("ArXiv")
    doi = ext.get("DOI")
    sid = p.get("paperId") or ""
    title = re.sub(r"\s+", " ", p.get("title") or "").strip()
    # S2 reference lists occasionally contain junk entries (title set to an author
    # list, no ids at all). Such entries cannot be verified or fetched: drop them.
    if not (sid or arxiv or doi) or len(title) < 12:
        return None
    return {
        "id": node_id_for(arxiv, doi, sid),
        "s2_id": sid,
        "openalex_id": None,
        "title": title,
        "year": p.get("year"),
        "cites": p.get("citationCount") or 0,
        "venue": p.get("venue") or "",
        "doi": doi or "",
        "arxiv_id": arxiv or "",
        "authors": [],
        "abstract": p.get("abstract") or "",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv}" if arxiv else "",
        "oa_url": f"https://arxiv.org/abs/{arxiv}" if arxiv else "",
        "generation": None,
        "co_cite": 0,
        "measured": False,
        "source": source,
        "oa_cites": None,
        "score": 0.0,
        "tier": "leaf",
        "tier_source": "auto",
        "suspicious": "",
    }


def oa_record_to_node(rec: dict, *, source: str, tier: str = "leaf") -> dict:
    node = {
        "id": node_id_for(rec.get("arxiv_id"), rec.get("doi"), rec.get("s2_id") or ""),
        "s2_id": rec.get("s2_id") or "",
        "openalex_id": rec.get("id"),
        "title": rec.get("title") or "",
        "year": rec.get("year"),
        "cites": rec.get("cites") or 0,
        "venue": rec.get("venue") or "",
        "doi": rec.get("doi") or "",
        "arxiv_id": rec.get("arxiv_id") or "",
        "authors": rec.get("authors") or [],
        "abstract": rec.get("abstract") or "",
        "pdf_url": rec.get("pdf_url") or "",
        "oa_url": rec.get("oa_url") or "",
        "generation": None,
        "co_cite": 0,
        "measured": False,
        "source": source,
        "oa_cites": rec.get("cites") or 0,
        "score": 0.0,
        "tier": tier,
        "tier_source": "seed-set" if tier == "seed" else "pool",
        "suspicious": "",
    }
    if node["arxiv_id"] and not node["pdf_url"]:
        node["pdf_url"] = f"https://arxiv.org/pdf/{node['arxiv_id']}"
        node["oa_url"] = f"https://arxiv.org/abs/{node['arxiv_id']}"
    return node


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def match_key(node: dict) -> str:
    doi = (node.get("doi") or "").lower()
    if doi and not doi.startswith("10.48550/arxiv."):
        return "doi:" + doi
    if node.get("arxiv_id"):
        return "arxiv:" + node["arxiv_id"]
    return "title:" + norm_title(node.get("title", ""))


def s2_lookup_id(node: dict, *, s2_cache=None, key=None) -> str | None:
    """S2 id for a node: arXiv id first, DOI second, title match as last resort."""
    if node.get("s2_id"):
        return node["s2_id"]
    resolved = None
    if node.get("arxiv_id"):
        resolved = f"arXiv:{node['arxiv_id']}"
    else:
        doi = (node.get("doi") or "").lower()
        if doi and not doi.startswith("10.48550/arxiv."):
            resolved = f"DOI:{doi}"
        elif doi.startswith("10.48550/arxiv."):
            resolved = f"arXiv:{doi.split('arxiv.', 1)[1]}"
    if not resolved and node.get("title"):
        resolved = s2.match_title(node["title"], cache_dir=s2_cache, key=key)
    if resolved:
        node["s2_id"] = resolved
        if node["id"].startswith("S2:unknown"):
            node["id"] = node_id_for(None, None, resolved)
    return resolved


def suspicion(node: dict) -> str:
    reasons = []
    if not node.get("venue") and not node.get("arxiv_id"):
        reasons.append("no-venue")
    if node.get("cites", 0) > 1500 and not node.get("venue") and not node.get("arxiv_id"):
        reasons.append("cites-without-venue")
    if JUNK_TITLE_RE.search(node.get("title") or ""):
        reasons.append("title-cliche")
    return ",".join(reasons)


def verify_cites_s2(nodes: dict[str, dict], *, top: int, s2_cache, key) -> int:
    """Refresh citation counts from S2 for the highest-priority nodes.

    Runs before any measurement: S2 quota is scarce without a key and these
    numbers anchor every table in the report. OpenAlex counts are kept as
    ``oa_cites`` for comparison (they are often fragmented).
    """
    checked = 0
    for node in sorted(nodes.values(), key=lambda n: -(n.get("cites") or 0))[:top]:
        pid = s2_lookup_id(node, s2_cache=s2_cache, key=key)
        if not pid:
            continue
        try:
            paper = s2.paper(pid, cache_dir=s2_cache, key=key)
        except s2.S2Unavailable as exc:
            print(f"[warn] S2 unavailable during citation refresh ({exc})", file=sys.stderr)
            break
        if paper.get("citationCount") is not None:
            if node.get("oa_cites") is None:
                node["oa_cites"] = node.get("cites")
            node["cites"] = paper["citationCount"]
            checked += 1
        if paper.get("venue") and not node.get("venue"):
            node["venue"] = paper["venue"]
    return checked


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_seeds_from_openalex(openalex_ids: list, *, cache, mailto) -> list[dict]:
    select = ("id,title,publication_year,cited_by_count,type,primary_location,"
              "best_oa_location,open_access,ids,authorships,abstract_inverted_index,locations")
    raw = oa.fetch_works(openalex_ids, select=select, cache_dir=cache, mailto=mailto)
    nodes = []
    for wid in openalex_ids:
        rec = raw.get(oa.normalize_id(wid))
        if not rec:
            print(f"[warn] seed {wid} not found in OpenAlex; skipped", file=sys.stderr)
            continue
        nodes.append(oa_record_to_node(oa.to_record(rec), source="seed", tier="seed"))
    return nodes


def resolve_seed_identities(seeds: list[dict], *, s2_cache, key) -> list[dict]:
    """Normalize seed ids via S2 so arXiv id / DOI / title all agree.

    OpenAlex records for old conference papers sometimes carry neither an arXiv
    id nor a DOI; S2 usually knows the paper, and the refreshed identity is what
    makes PDF fetching and further expansion possible.
    """
    for node in seeds:
        pid = s2_lookup_id(node, s2_cache=s2_cache, key=key)
        if not pid:
            continue
        try:
            rec = s2.paper(pid, cache_dir=s2_cache, key=key)
        except s2.S2Unavailable:
            continue
        if not rec.get("title"):
            continue
        ext = rec.get("externalIds") or {}
        node["s2_id"] = pid
        node["arxiv_id"] = ext.get("ArXiv") or node.get("arxiv_id") or ""
        node["doi"] = ext.get("DOI") or node.get("doi") or ""
        if rec.get("citationCount") is not None:
            node["cites"] = rec["citationCount"]
        if rec.get("venue"):
            node["venue"] = rec["venue"]
        if not node.get("year"):
            node["year"] = rec.get("year")
        new_id = node_id_for(node["arxiv_id"], node["doi"], pid)
        if new_id != node["id"]:
            print(f"[seed] {node['id']} -> {new_id}", file=sys.stderr)
            node["id"] = new_id
        if node["arxiv_id"] and not node.get("pdf_url"):
            node["pdf_url"] = f"https://arxiv.org/pdf/{node['arxiv_id']}"
            node["oa_url"] = f"https://arxiv.org/abs/{node['arxiv_id']}"
    return seeds


def load_pool_from_json(path: str) -> list[dict]:
    data = oa.read_json(path)
    seen, nodes = set(), []
    for bucket in ("co_cite_top", "cites_top", "candidates"):
        for rec in data.get(bucket) or []:
            if not rec.get("title"):
                continue
            # S2-only candidates (no OpenAlex id) are valid too: they carry s2_id
            key = rec.get("id") or rec.get("s2_id") or norm_title(rec["title"])
            if key in seen:
                continue
            seen.add(key)
            nodes.append(oa_record_to_node(rec, source="pool"))
    return nodes


# --------------------------------------------------------------------------- #
# measurement: pull S2 reference lists, build graph + co-citation
# --------------------------------------------------------------------------- #
def measure(nodes: dict[str, dict], *, worklist: list[dict], budget: int, gens: int,
            ref_limit: int, s2_cache, key) -> tuple[list, int]:
    edges: set = set()
    measured = 0
    pending = [n for n in worklist if not n.get("measured")]
    batch_size = 0
    for depth in range(1, gens + 1):
        if not pending or measured >= budget:
            break
        pending.sort(key=lambda n: -n["cites"])
        batch = pending[: min(len(pending), budget - measured)]
        batch_size = len(batch)
        newly: list[dict] = []
        for node in batch:
            if measured >= budget:
                break
            pid = s2_lookup_id(node, s2_cache=s2_cache, key=key)
            if not pid:
                node["measured"] = True
                continue
            try:
                refs = s2.references(pid, limit=ref_limit, cache_dir=s2_cache, key=key)
            except s2.S2Unavailable as exc:
                print(f"[warn] S2 unavailable after {measured} measurements ({exc}); "
                      f"stopping measurement early", file=sys.stderr)
                return sorted(edges), measured
            measured += 1
            node["measured"] = True
            for ref in refs:
                if not ref or not ref.get("title"):
                    continue
                ref_node = s2_paper_to_node(ref, source="discovered")
                if ref_node is None:
                    continue
                ref_node["generation"] = depth + 1
                existing = nodes.get(ref_node["id"])
                if existing is None:
                    nodes[ref_node["id"]] = ref_node
                    existing = ref_node
                    newly.append(ref_node)
                existing["co_cite"] = existing.get("co_cite", 0) + 1
                if existing["id"] != node["id"]:
                    edges.add((node["id"], existing["id"]))
            print(f"[measure] {node['id']} -> {len(refs)} refs "
                  f"({measured}/{batch_size + measured - len(batch)})", file=sys.stderr)
        pending = [n for n in newly if not n.get("measured") and n["cites"] >= 5]
        print(f"[measure] depth {depth}: {len(newly)} new nodes, "
              f"{len(pending)} queued for next depth", file=sys.stderr)
    return sorted(edges), measured


# --------------------------------------------------------------------------- #
# scoring / tiers
# --------------------------------------------------------------------------- #
def score_nodes(nodes: dict[str, dict], edges: list, seeds: set,
                field_roots: list[str] | None = None) -> None:
    in_deg = Counter(dst for _, dst in edges)
    # direct links from the seed set are the strongest "belongs to this lineage" hint
    seed_link = Counter(dst for src, dst in edges if src in seeds)
    max_cites = max((n["cites"] for n in nodes.values()), default=1)
    max_co = max((n.get("co_cite", 0) for n in nodes.values()), default=1)
    measured_total = sum(1 for n in nodes.values() if n.get("measured")) or 1
    for nid, node in nodes.items():
        node["in_pool_degree"] = in_deg.get(nid, 0)
        node["seed_link"] = seed_link.get(nid, 0)
        node["suspicious"] = suspicion(node)
        node["topic_hit"] = oa.title_hit(node.get("title") or "", field_roots or [])
        cite_term = math.log1p(node["cites"]) / math.log1p(max(max_cites, node["cites"]))
        pool_term = math.log1p(node.get("co_cite", 0)) / math.log1p(max(max_co, 1))
        link_term = 1.0 if node["seed_link"] else 0.0
        node["score"] = round(0.4 * cite_term + 0.35 * pool_term + 0.25 * link_term, 3)
        # a paper cited by nearly every measured paper but never by a seed is a
        # general-purpose baseline (BERT, Adam, BLEU...), not a field milestone
        share = node.get("co_cite", 0) / measured_total
        node["general_baseline"] = bool(share >= 0.35 and node["seed_link"] == 0
                                         and node["id"] not in seeds)
        if nid in seeds:
            node["tier"], node["tier_source"] = "seed", "seed-set"


def assign_tiers(nodes: dict[str, dict], seeds: set, top_backbone: int) -> None:
    kept = 0
    for node in sorted(nodes.values(), key=lambda n: -n["score"]):
        if node["id"] in seeds or node.get("source") == "seed":
            continue
        if (kept < top_backbone and not node["suspicious"]
                and not node.get("general_baseline")
                and (node.get("co_cite", 0) >= 2 or node.get("seed_link", 0) > 0)
                and (node.get("topic_hit") or node.get("seed_link", 0) >= 2)):
            node["tier"], node["tier_source"] = "backbone", "auto"
            kept += 1
        elif node.get("source") == "pool":
            node["tier"], node["tier_source"] = "pool", "pool"
        else:
            node["tier"], node["tier_source"] = "leaf", "auto"


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #
def write_outputs(workdir: Path, topic: str, seeds: list, nodes: dict,
                  edges: list, measured: int) -> None:
    ordered = sorted(nodes.values(), key=lambda n: (-(n["year"] or 0), -n["score"]))
    oa.write_json(workdir / "lineage.json", {
        "topic": topic,
        "generated_at": oa.today(),
        "source_graph": "semantic-scholar/references",
        "counts_asof": oa.today(),
        "seeds": [s["id"] for s in seeds],
        "measured_nodes": measured,
        "node_count": len(ordered),
        "nodes": ordered,
        "edges": edges,
        "notes": [
            "tier is a reading recommendation; record its rationale separately.",
            "co_cite = reference-list occurrences in the sampled papers.",
            "seed_link = direct references from the seeds to this node.",
            "general_baseline is a heuristic based on sample frequency and seed "
            "links; check relevance to the research question.",
            "cites prefers S2 when available; inspect the source and fetch date "
            "before reporting a count. oa_cites retains a comparison value.",
            "Edges point from citing paper to cited paper (S2 references).",
            "Coverage depends on the sampled reference lists; search for later "
            "papers separately.",
        ],
    })

    lines = [f"# Lineage timeline — {topic or 'untitled'}", "",
             f"Generated {oa.today()} from {measured} measured reference lists "
             f"(Semantic Scholar). co_cite counts how often a paper appears across "
             f"those lists.", "",
             "Tiers: **seed** (starting paper) · **backbone** (suggested close reading) · "
             "**pool** (input candidate) · **leaf** (other collected reference). "
             "Tiers do not record researcher reading progress.", "",
             "## By year", ""]
    buckets: dict[int, list[dict]] = defaultdict(list)
    for node in ordered:
        buckets[node["year"] or 0].append(node)
    for year in sorted(buckets):
        lines.append(f"### {year}")
        lines.append("")
        notable = [n for n in buckets[year]
                   if n.get("tier") != "excluded"
                   and (n.get("tier") != "leaf" or n.get("co_cite", 0) >= 2)]
        notable.sort(key=lambda n: -n["score"])
        omitted = len(buckets[year]) - len(notable)
        rows = []
        for node in notable[:30]:
            flags = []
            if node["suspicious"]:
                flags.append("⚠")
            if node.get("general_baseline"):
                flags.append("baseline")
            if node.get("topic_hit") is False:
                flags.append("off-topic")
            oa_c = oa.fmt_int(node["oa_cites"]) if node.get("oa_cites") else "-"
            rows.append([node["id"], node["tier"], node.get("co_cite", 0),
                         node.get("seed_link", 0), oa.fmt_int(node["cites"]), oa_c,
                         ",".join(flags) or "-", oa.truncate(node["venue"], 16),
                         oa.truncate(node["title"], 62)])
        lines.append(oa.md_table(["id", "tier", "co-cite", "seed-link", "cites",
                                  "oa-cites", "flag", "venue", "title"], rows))
        if omitted > 0:
            lines.append("")
            lines.append(f"_plus {omitted} leaf node(s) with co-cite < 2 (see lineage.json)._")
        lines.append("")

    lines.append("## Top by score (auto suggestion)")
    lines.append("")
    rows = []
    for node in sorted(nodes.values(), key=lambda n: -n["score"])[:35]:
        rows.append([node["id"], node["tier"], node["score"], node["year"] or "?",
                     node.get("co_cite", 0), node.get("seed_link", 0),
                     oa.fmt_int(node["cites"]), oa.truncate(node["title"], 64)])
    lines.append(oa.md_table(["id", "tier", "score", "year", "co-cite", "seed-link",
                              "cites", "title"], rows))
    (workdir / "timeline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def mode_set_tiers(workdir: Path, backbone_ids: str, exclude_ids: str | None) -> int:
    path = workdir / "lineage.json"
    data = oa.read_json(path)
    want_backbone = {x.strip() for x in backbone_ids.split(",") if x.strip()}
    want_excluded = {x.strip() for x in (exclude_ids or "").split(",") if x.strip()}
    seeds = set(data.get("seeds") or [])
    known = {n["id"] for n in data["nodes"]}
    missing = (want_backbone | want_excluded) - known
    if missing:
        raise SystemExit(f"unknown ids: {', '.join(sorted(missing))}")
    for node in data["nodes"]:
        nid = node["id"]
        if nid in want_excluded:
            node["tier"], node["tier_source"] = "excluded", "agent"
        elif nid in want_backbone:
            node["tier"], node["tier_source"] = "backbone", "agent"
        elif nid in seeds:
            node["tier"], node["tier_source"] = "seed", "seed-set"
        elif node.get("tier") in ("backbone", "seed"):
            node["tier"], node["tier_source"] = "leaf", "agent"
    data["tier_decided_at"] = oa.today()
    oa.write_json(path, data)
    print(f"backbone ({len(want_backbone)}): {', '.join(sorted(want_backbone))}")
    if want_excluded:
        print(f"excluded: {', '.join(sorted(want_excluded))}")
    others = [n["id"] for n in data["nodes"] if n["tier"] == "backbone"]
    print(f"lineage.json now has {len(others)} backbone node(s); update timeline.md "
          f"to match. Rebuilding assigns tiers again and replaces these choices.")
    return 0


def mode_show(workdir: Path, ids: list[str], with_abstract: bool, limit: int) -> int:
    data = oa.read_json(workdir / "lineage.json")
    by_id = {n["id"]: n for n in data["nodes"]}
    for wid in ids:
        node = by_id.get(wid) or next((n for n in data["nodes"]
                                       if n.get("openalex_id") == wid
                                       or n["id"].lower() == wid.lower()), None)
        if not node:
            print(f"{wid}: not in lineage.json")
            continue
        print(f"\n### {node['id']} · {node['year']} · {oa.fmt_int(node['cites'])} cites "
              f"(OA {node.get('oa_cites')}) · co-cite {node.get('co_cite')} · "
              f"{node['tier']} · measured={node.get('measured')}")
        print(f"**{node['title']}**")
        print(f"venue: {node['venue'] or '(none)'} | pdf: {node['pdf_url'] or '(none)'} | "
              f"doi: {node['doi'] or '(none)'}")
        if with_abstract:
            print("abstract:", oa.abstract_excerpt(node, limit))
        cited_by = [e[0] for e in data["edges"] if e[1] == node["id"]]
        refs = [e[1] for e in data["edges"] if e[0] == node["id"]]
        if cited_by:
            print("cited by (in graph):", ", ".join(cited_by))
        if refs:
            print("cites (in graph):", ", ".join(refs))
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("build", "show", "tier", "-h", "--help"):
        argv = ["build"] + argv
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode")

    b = sub.add_parser("build", help="measure reference lists; write genealogy files")
    b.add_argument("--workdir", required=True)
    b.add_argument("--seeds", required=True, help="comma-separated OpenAlex ids")
    b.add_argument("--pool-from", default=None,
                   help="seeds.json from find_seeds.py (adds the relevance pool)")
    b.add_argument("--topic", default="")
    b.add_argument("--budget", type=int, default=60,
                   help="max S2 measurements (roughly one request each)")
    b.add_argument("--gens", type=int, default=1,
                   help="backward hops (default 1; 2+ drags in general background)")
    b.add_argument("--ref-limit", type=int, default=200,
                   help="max references pulled per paper")
    b.add_argument("--top-backbone", type=int, default=8)
    b.add_argument("--verify-top", type=int, default=10,
                   help="seed/pool nodes whose citation count is refreshed from S2 "
                        "before anything else (OpenAlex counts are unreliable)")
    b.add_argument("--enrich-openalex", type=int, default=30,
                   help="fill abstracts/OA links for this many top nodes")
    b.add_argument("--cache", default=None)
    b.add_argument("--s2-cache", default=None)
    b.add_argument("--mailto", default=None)
    b.add_argument("--s2-key", default=None)

    s = sub.add_parser("show", help="print node summaries from lineage.json")
    s.add_argument("--workdir", required=True)
    s.add_argument("--ids", nargs="+", required=True)
    s.add_argument("--no-abstract", action="store_true")
    s.add_argument("--abstract-limit", type=int, default=700)

    t = sub.add_parser("tier", help="record the human/agent decision back into "
                                     "lineage.json (verify_survey.py trusts this)")
    t.add_argument("--workdir", required=True)
    t.add_argument("--backbone", required=True,
                   help="comma-separated node ids that ARE the main line")
    t.add_argument("--exclude", default=None,
                   help="comma-separated ids to mark as excluded (junk/off-topic)")

    args = p.parse_args(argv)
    if args.mode == "tier":
        return mode_set_tiers(Path(args.workdir), args.backbone, args.exclude)
    if args.mode == "show":
        return mode_show(Path(args.workdir), args.ids, not args.no_abstract,
                         args.abstract_limit)

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    args.mailto = oa.resolve_mailto(getattr(args, "mailto", None))
    key = s2.resolve_key(args.s2_key)
    if args.cache is None:
        args.cache = str(workdir / ".cache")
    if args.s2_cache is None:
        args.s2_cache = str(workdir / ".s2cache")

    if args.seeds.strip().endswith(".json"):
        pool_path = args.seeds.strip()
        seeds = load_pool_from_json(pool_path)
        for node in seeds:
            node["source"], node["tier"], node["tier_source"] = "seed", "seed", "seed-set"
    else:
        seed_ids = [oa.normalize_id(x) for x in re.split(r"[,\s]+", args.seeds) if x.strip()]
        seeds = load_seeds_from_openalex(seed_ids, cache=args.cache, mailto=args.mailto)
        if not seeds:
            raise SystemExit("no usable seeds")
        seeds = resolve_seed_identities(seeds, s2_cache=args.s2_cache, key=key)

    nodes: dict[str, dict] = {n["id"]: n for n in seeds}
    worklist = list(seeds)
    if args.pool_from:
        pool = [n for n in load_pool_from_json(args.pool_from)]
        added = 0
        for node in pool:
            if node["id"] not in nodes:
                nodes[node["id"]] = node
                worklist.append(node)
                added += 1
        print(f"[pool] {added} pool papers added from {args.pool_from}", file=sys.stderr)

    print(f"[start] {len(worklist)} papers in the worklist, budget {args.budget}",
          file=sys.stderr)
    # Refresh seed/pool citation counts first: S2 quota is tight without a key and
    # these numbers anchor every table the report will show.
    checked = verify_cites_s2(nodes, top=args.verify_top, s2_cache=args.s2_cache, key=key)
    if checked:
        print(f"[s2] refreshed citation counts for {checked} seed/pool nodes", file=sys.stderr)
    edges, measured = measure(nodes, worklist=worklist, budget=args.budget, gens=args.gens,
                              ref_limit=args.ref_limit, s2_cache=args.s2_cache, key=key)
    if measured == 0:
        raise SystemExit("no reference list could be measured (S2 unavailable, or no "
                         "paper had a resolvable S2 id). Retry later or pass --s2-key; "
                         "do NOT fabricate a lineage without citation data.")

    enrich_with_openalex(nodes, cache=args.cache, mailto=args.mailto,
                         limit=args.enrich_openalex)
    field_roots = oa.title_roots([n.get("title") or "" for n in seeds]
                                 + [n.get("title") or "" for n in worklist[:40]
                                    if n.get("source") == "pool"])
    score_nodes(nodes, edges, {n["id"] for n in seeds}, field_roots)
    assign_tiers(nodes, {n["id"] for n in seeds}, args.top_backbone)
    write_outputs(workdir, args.topic, seeds, nodes, edges, measured)

    backbone = [n for n in nodes.values() if n["tier"] == "backbone"]
    print(f"\n[graph] {len(nodes)} nodes, {len(edges)} edges, {measured} measured")
    print(f"Auto-suggested backbone ({len(backbone)}):")
    for node in sorted(backbone, key=lambda n: -(n["year"] or 0)):
        print(f"  {node['id']}  {node['year']}  score={node['score']}  "
              f"co-cite={node['co_cite']}  cites={oa.fmt_int(node['cites'])}  "
              f"{oa.truncate(node['title'], 60)}")
    print(f"\nWrote {workdir / 'lineage.json'} and {workdir / 'timeline.md'}.")
    print("Review node summaries and record the reading priorities with tier. "
          "Prepare guides for the researcher and search separately for later work.")
    return 0


def enrich_with_openalex(nodes: dict[str, dict], *, cache, mailto, limit: int) -> None:
    """Fill abstracts / OA pdf urls / openalex ids for top nodes (best effort)."""
    targets = [n for n in sorted(nodes.values(), key=lambda n: -n["cites"])[:limit]
               if not n.get("openalex_id") and n.get("doi")]
    doi_map = {n["doi"].lower(): n for n in targets}
    if not doi_map:
        return
    dois = list(doi_map)[: 40]
    try:
        data = oa.api_get(oa._with_params(f"{oa.OA_BASE}/works",
                                          {"filter": "doi:" + "|".join(dois),
                                           "per-page": len(dois)}, mailto),
                          cache_dir=cache, mailto=mailto)
    except RuntimeError as exc:
        print(f"[warn] OpenAlex enrich failed: {exc}", file=sys.stderr)
        return
    for work in data.get("results", []):
        rec = oa.to_record(work)
        node = doi_map.get((rec.get("doi") or "").lower())
        if not node:
            continue
        node["openalex_id"] = rec["id"]
        node["oa_cites"] = rec["cites"]
        if not node.get("abstract"):
            node["abstract"] = rec["abstract"]
        if not node.get("pdf_url") and rec.get("pdf_url"):
            node["pdf_url"] = rec["pdf_url"]
        if not node.get("oa_url"):
            node["oa_url"] = rec.get("oa_url") or rec.get("pdf_url") or ""
        if not node.get("venue"):
            node["venue"] = rec["venue"]


if __name__ == "__main__":
    sys.exit(main())
