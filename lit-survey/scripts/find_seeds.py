#!/usr/bin/env python3
"""Find candidate foundational papers for a literature survey.

Modes
-----
search
    Search OpenAlex titles, then collect reference lists for a sample of the
    results. Prefer S2 references, with OpenAlex as a fallback. Print candidates
    ranked by reference frequency and a separate list of highly cited papers.
reverse
    Collect references shared by recent reviews using OpenAlex.

Citation counts and reference frequencies help prioritize reading. Review the
paper identities and their relevance before selecting a starting set. The JSON
output contains the ranked candidates, not the full search pool.

Examples
--------
    python3 find_seeds.py search "retrieval augmented generation" --pool 150
    python3 find_seeds.py reverse --topic "long-context language models" --reviews 4
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oalib as oa  # noqa: E402
import s2lib as s2  # noqa: E402

POOL_SELECT = ("id,title,publication_year,cited_by_count,type,primary_location,"
               "best_oa_location,open_access,ids,authorships,locations")
DEFAULT_EXCLUDE_WORDS = ("erratum", "correction", "retraction", "editorial", "comment on")
JUNK_TITLE_RE = re.compile(
    r"(consciousness|paradigm shift|revolutioniz|game.?chang|"
    r"unprecedented|breakthrough framework|holistic framework)", re.I)


def query_tokens(queries: list) -> list[str]:
    """Field roots from the queries (shared stopword list lives in oalib)."""
    return oa.title_roots(queries)


def title_on_topic(title: str, roots: list[str]) -> bool:
    """True when the title hits at least one query root (see oalib.title_hit)."""
    return oa.title_hit(title, roots)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def dedupe(records: list) -> list:
    seen: dict[str, dict] = {}
    for rec in records:
        key = ("doi:" + rec["doi"].lower()) if rec.get("doi") else \
              "title:" + re.sub(r"[^a-z0-9]", "", rec["title"].lower())
        cur = seen.get(key)
        if cur is None or rec.get("cites", 0) > cur.get("cites", 0):
            seen[key] = rec
    return list(seen.values())


def suspicion(rec: dict) -> str:
    """Flag records whose metadata looks unreliable, so the user can veto them.

    Deliberately narrow: a missing venue is extremely common for arXiv records
    and must not raise a flag on its own.
    """
    reasons = []
    if rec.get("is_retracted"):
        reasons.append("retracted")
    if (rec.get("cites", 0) > 1500 and not rec.get("venue")
            and not rec.get("arxiv_id") and not rec.get("doi")):
        reasons.append("cites-without-identity")
    if JUNK_TITLE_RE.search(rec.get("title") or ""):
        reasons.append("title-cliche")
    return ",".join(reasons)


def collect_pool(queries: list, pool_size: int, exclude: tuple, *,
                 cache, mailto, extra_filter: str = "", roots: list | None = None,
                 fulltext: bool = False) -> dict:
    """Build the relevance pool.

    Primary retriever is ``title.search:<query>``: it is the only OpenAlex
    search that reliably lands on the right field for short technical phrases
    (``search=`` full-text happily returns finance/neuroscience papers for
    "speculative decoding"). ``--fulltext`` switches to the noisy `search=``
    behaviour, kept only as an escape hatch for queries that never appear in
    titles.
    """
    pool: dict[str, dict] = {}
    for query in queries:
        kept_before = len(pool)
        for mode in (["fulltext"] if fulltext else ["title", "fulltext"]):
            if mode == "title":
                filters = f"title.search:{query}"
                sort = "cited_by_count:desc"
                query_arg = ""
            else:
                filters = ""
                sort = None
                query_arg = query
                kept_before_fallback = len(pool)
                if not fulltext and len(pool) - kept_before >= 5:
                    break          # title search already worked, no fallback needed
            if extra_filter:
                filters = f"{filters},{extra_filter}" if filters else extra_filter
            fetched, page = 0, 1
            while len(pool) - kept_before < pool_size and fetched < pool_size * 3:
                data = oa.search_works(query_arg, filters=filters, sort=sort,
                                       per_page=min(100, pool_size * 3 - fetched),
                                       page=page, select=POOL_SELECT,
                                       cache_dir=cache, mailto=mailto)
                results = data.get("results", [])
                if not results:
                    break
                for work in results:
                    rec = oa.to_record(work)
                    low = rec["title"].lower()
                    if any(w in low for w in exclude):
                        continue
                    if mode == "fulltext" and roots and not title_on_topic(rec["title"], roots):
                        continue
                    pool.setdefault(rec["id"], rec)
                fetched += len(results)
                page += 1
                if page > 6:
                    break
            tag = "title.search" if mode == "title" else "fulltext"
            got = len(pool) - kept_before
            print(f"[pool] {tag} {query!r}: scanned {fetched}, on-topic kept {got}",
                  file=sys.stderr)
            if mode == "fulltext" and not fulltext:
                got_fallback = len(pool) - kept_before_fallback
                if got_fallback:
                    print(f"[pool] title.search found <5 hits; fulltext fallback added "
                          f"{got_fallback}", file=sys.stderr)
    return pool


def norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def s2_co_cite(pool: dict, *, measure_n: int, ref_limit: int, s2_cache, key):
    """Co-citation over S2 reference lists (the reliable signal).

    Returns (counter over match key, paper objects by match key, measured count).
    OpenAlex reference data is far too sparse to use here.
    """
    freq: Counter = Counter()
    papers: dict[str, dict] = {}
    targets = [r for r in sorted(pool.values(), key=lambda r: -r.get("cites", 0))
               if s2.paper_id_for(r)][:measure_n]
    measured = 0
    for rec in targets:
        pid = s2.paper_id_for(rec)
        try:
            refs = s2.references(pid, limit=ref_limit, cache_dir=s2_cache, key=key)
        except s2.S2Unavailable as exc:
            print(f"[s2] unavailable after {measured} reference lists ({exc}); "
                  f"falling back to the (weaker) OpenAlex co-citation count",
                  file=sys.stderr)
            return None, None, measured
        measured += 1
        for paper in refs:
            if not paper or not paper.get("title"):
                continue
            ext = paper.get("externalIds") or {}
            if not (paper.get("paperId") or ext.get("ArXiv") or ext.get("DOI")):
                continue          # unverifiable junk entry (title-only author lists)
            if len(paper["title"].strip()) < 12:
                continue
            doi = (ext.get("DOI") or "").lower()
            key_ = "doi:" + doi if doi else "t:" + norm_key(paper["title"])
            freq[key_] += 1
            papers.setdefault(key_, paper)
        print(f"[s2] {rec['id']} -> {len(refs)} refs ({measured}/{len(targets)})",
              file=sys.stderr)
    return freq, papers, measured


def s2_paper_to_record(paper: dict) -> dict:
    ext = paper.get("externalIds") or {}
    arxiv = ext.get("ArXiv")
    doi = (ext.get("DOI") or "").lower()
    return {
        "id": "",
        "s2_id": paper.get("paperId") or "",
        "title": paper.get("title") or "",
        "year": paper.get("year"),
        "cites": paper.get("citationCount") or 0,
        "type": "",
        "venue": paper.get("venue") or "",
        "doi": doi,
        "authors": [],
        "abstract": "",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv}" if arxiv else "",
        "oa_url": f"https://arxiv.org/abs/{arxiv}" if arxiv else "",
        "arxiv_id": arxiv or "",
    }


def co_citation_counts(pool_ids: list, *, cache, mailto) -> Counter:
    """Count how many pool members list each reference (in-pool co-citation)."""
    freq: Counter = Counter()
    chunk = 50
    for i in range(0, len(pool_ids), chunk):
        raw = oa.fetch_works(pool_ids[i:i + chunk], select="id,referenced_works",
                             cache_dir=cache, mailto=mailto)
        for work in raw.values():
            for ref in set(oa.short_id(r) for r in (work.get("referenced_works") or [])):
                freq[ref] += 1
    return freq


def print_table(records: list, freq: Counter, pool_size: int, title: str) -> None:
    print(f"\n## {title}")
    if not records:
        print("(no candidates)")
        return
    rows = []
    for i, rec in enumerate(records, 1):
        co = rec.get("co_cite", freq.get(pool_key_of(rec), 0))
        share = f"{100 * co / max(pool_size, 1):.0f}%" if co else "-"
        flag = suspicion(rec)
        display = rec["id"] or f"S2:{(rec.get('s2_id') or '?')[:8]}"
        rows.append([i, display, rec["year"] or "?", oa.fmt_int(rec["cites"]),
                     f"{co} ({share})", oa.truncate(rec["venue"], 22),
                     ("⚠ " if flag else "") + oa.truncate(rec["title"], 60)])
    print(oa.md_table(["#", "openalex", "year", "cites", "co-cite", "venue", "title"], rows))
    flagged = [r["id"] or r.get("s2_id", "?") for r in records if suspicion(r)]
    if flagged:
        print(f"⚠ suspicious records (verify by hand, likely drop): {', '.join(flagged)}")


def pool_key_of(rec: dict) -> str:
    doi = (rec.get("doi") or "").lower()
    return ("doi:" + doi) if doi else "t:" + norm_key(rec.get("title") or "")


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def s2_refresh_counts(records: list, *, top: int, s2_cache, key) -> int:
    """Overwrite OpenAlex citation counts with S2's for the top records.

    OpenAlex routinely splits citations across duplicate records (a foundational
    paper can show 34 citations while S2 shows 3,000), so the counts that drive
    the tables must come from S2 whenever possible.
    """
    checked = 0
    for rec in sorted(records, key=lambda r: -r.get("cites", 0))[:top]:
        pid = s2.paper_id_for(rec)
        if not pid:
            continue
        try:
            paper = s2.paper(pid, cache_dir=s2_cache, key=key)
        except s2.S2Unavailable:
            break
        if paper.get("citationCount") is not None:
            rec["oa_cites"] = rec.get("cites")
            rec["cites"] = paper["citationCount"]
            rec["cites_source"] = "s2"
            checked += 1
    return checked


def mode_search(args) -> dict:
    roots = query_tokens(args.queries)
    pool = collect_pool(args.queries, args.pool, tuple(args.exclude),
                        cache=args.cache, mailto=args.mailto,
                        extra_filter=args.extra_filter or "", roots=roots,
                        fulltext=args.fulltext)
    if len(pool) < 10:
        raise SystemExit(f"pool too small ({len(pool)} papers). Add query variants, "
                         f"raise --pool, or use a known anchor paper instead.")
    s2key = s2.resolve_key(args.s2_key)

    freq: Counter = Counter()
    papers: dict[str, dict] = {}
    measured = 0
    if args.s2_measure > 0:
        result = s2_co_cite(pool, measure_n=args.s2_measure, ref_limit=200,
                            s2_cache=args.s2_cache, key=s2key)
        if result[0] is not None:
            freq, papers, measured = result
    if not freq:
        print("[fallback] using OpenAlex reference lists for co-citation "
              "(sparse and less reliable)", file=sys.stderr)
        freq = co_citation_counts(list(pool), cache=args.cache, mailto=args.mailto)

    pool_by_key = {pool_key_of(rec): rec for rec in pool.values()}
    by_cocite: list[dict] = []
    for key_, count in freq.most_common(args.top * 3):
        paper = papers.get(key_)
        rec = pool_by_key.get(key_)
        if rec is not None:
            rec = dict(rec)
            # the S2 reference entry carries an authoritative citation count:
            # use it, so no extra request is needed to fix fragmented OpenAlex counts
            if paper and paper.get("citationCount") is not None:
                rec["oa_cites"] = rec.get("cites")
                rec["cites"] = paper["citationCount"]
                rec["cites_source"] = "s2"
                if paper.get("externalIds", {}).get("ArXiv") and not rec.get("arxiv_id"):
                    rec["arxiv_id"] = paper["externalIds"]["ArXiv"]
        elif paper:
            rec = s2_paper_to_record(paper)
        else:
            continue
        rec["co_cite"] = count
        by_cocite.append(rec)
    by_cocite = dedupe(by_cocite)[: args.top]
    seen = {pool_key_of(r) for r in by_cocite}

    by_cites = []
    for rec in sorted(pool.values(), key=lambda r: -r.get("cites", 0)):
        if pool_key_of(rec) in seen:
            continue
        rec = dict(rec)
        rec["co_cite"] = freq.get(pool_key_of(rec), 0)
        by_cites.append(rec)
        if len(by_cites) >= args.top:
            break

    # Table B sits at the mercy of unreliable OpenAlex counts, so refresh those few.
    refreshed = s2_refresh_counts(by_cites, top=args.s2_verify,
                                  s2_cache=args.s2_cache, key=s2key)
    if refreshed:
        print(f"[s2] citation counts refreshed for {refreshed} table-B candidates",
              file=sys.stderr)

    print_table(by_cocite, freq, max(len(pool), 1),
                "A. Field foundations (ranked by in-pool co-citation)")
    print_table(by_cites, freq, max(len(pool), 1),
                "B. Highly cited on-topic pool members")
    print(f"\nSignals: co-cite = reference-list occurrences across "
          f"{measured or 'the'} measured papers ({len(pool)} pool papers total); "
          f"cites = S2/OpenAlex citation count. Check identities, count sources "
          f"and relevance before choosing seeds.")

    top_co = freq.most_common(1)[0][1] if freq else 0
    denom = measured or max(len(pool), 1)
    if freq and (top_co < 3 or top_co / denom < 0.05):
        print(f"\n[warn] weak pool signal: the best co-citation score is {top_co} across "
              f"{denom} measured papers. The query probably did not find a coherent "
              f"field; try more specific query phrases, or use a known anchor paper and "
              f"skip this step.", file=sys.stderr)

    return {"mode": "search", "queries": args.queries, "pool_size": len(pool),
            "measured": measured, "co_cite_source": "s2" if measured else "openalex",
            "co_cite_top": by_cocite, "cites_top": by_cites,
            "candidates": dedupe(by_cocite + by_cites)}


def mode_reverse(args) -> dict:
    since = args.review_since
    reviews: list = []
    for query in args.topic:
        data = oa.search_works("", filters=f"type:review,publication_year:>{since - 1},"
                               f"title.search:{query}",
                               sort="cited_by_count:desc",
                               per_page=args.reviews * 2, cache_dir=args.cache,
                               mailto=args.mailto, select="id,title,publication_year,"
                               "cited_by_count,referenced_works")
        reviews.extend(data.get("results", []))
        if len(reviews) >= args.reviews:
            break
    if not reviews:
        print("[reverse] no type:review works matched by title; retrying with "
              "'survey OR review' in the title.", file=sys.stderr)
        for query in args.topic:
            data = oa.search_works(f"{query} survey OR review",
                                   filters=f"publication_year:>{since - 1}",
                                   per_page=args.reviews * 2, cache_dir=args.cache,
                                   mailto=args.mailto,
                                   select="id,title,publication_year,cited_by_count,"
                                          "referenced_works")
            reviews.extend(data.get("results", []))
            if len(reviews) >= args.reviews:
                break
    reviews = sorted(dedupe([oa.to_record(r) for r in reviews]), key=lambda r: -r["cites"])
    reviews = [r for r in reviews if r.get("referenced_works")][: args.reviews]
    if not reviews:
        raise SystemExit("reverse mode: no usable reviews found; broaden --topic or "
                         "lower --review-since")

    freq: Counter = Counter()
    for rev in reviews:
        for ref in set(rev["referenced_works"]):
            freq[ref] += 1
    print(f"[reverse] {len(reviews)} reviews, {len(freq)} distinct references", file=sys.stderr)

    hot = [wid for wid, n in freq.items() if n >= args.min_freq]
    hot.sort(key=lambda wid: -freq[wid])
    raw = oa.fetch_works(hot[:150], cache_dir=args.cache, mailto=args.mailto)
    review_ids = {r["id"] for r in reviews}
    recs = []
    for wid in hot:
        if wid in raw and wid not in review_ids:
            rec = oa.to_record(raw[wid])
            if rec["cites"] >= args.min_cites:
                rec["co_cite"] = freq[wid]
                recs.append(rec)
    recs = recs[: args.max_refs]

    print_table(recs, freq, len(reviews),
                "Seed candidates from review reference lists (co-cite = number of "
                "reviews citing it, out of %d)" % len(reviews))
    print("\nReviews mined: " + "; ".join(f"{r['id']} {oa.truncate(r['title'], 60)}"
                                          for r in reviews))
    return {"mode": "reverse", "topic": args.topic, "reviews_used": reviews,
            "candidates": recs}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("search", help="relevance-pool analysis for a topic")
    s.add_argument("queries", nargs="+", help="one or more query variants")
    s.add_argument("--pool", type=int, default=150,
                   help="on-topic pool size per query (default 150)")
    s.add_argument("--top", type=int, default=12, help="rows per table")
    s.add_argument("--s2-measure", type=int, default=20,
                   help="pool papers whose S2 reference lists are mined for co-citation "
                        "(0 = fall back to the sparse OpenAlex reference data)")
    s.add_argument("--s2-verify", type=int, default=10,
                   help="table-B candidates whose citation count is refreshed from S2 "
                        "(OpenAlex counts are unreliable; 0 = keep OpenAlex counts)")
    s.add_argument("--fulltext", action="store_true",
                   help="use noisy full-text search instead of title.search "
                        "(only when the query never appears in titles)")
    s.add_argument("--extra-filter", help="extra OpenAlex filter, e.g. 'publication_year:>2018'")
    s.add_argument("--exclude", nargs="*", default=list(DEFAULT_EXCLUDE_WORDS),
                   help="drop titles containing these words")

    r = sub.add_parser("reverse", help="mine reference lists of recent reviews")
    r.add_argument("--topic", nargs="+", required=True)
    r.add_argument("--reviews", type=int, default=3, help="how many reviews to mine")
    r.add_argument("--review-since", type=int, default=2020)
    r.add_argument("--min-freq", type=int, default=2, help="cited by N reviews to qualify")
    r.add_argument("--min-cites", type=int, default=100)
    r.add_argument("--max-refs", type=int, default=15)
    r.add_argument("--exclude", nargs="*", default=list(DEFAULT_EXCLUDE_WORDS))

    for sp in (s, r):
        sp.add_argument("--out", default="seeds.json")
        sp.add_argument("--cache", default=None,
                        help="cache directory (default: <out dir>/.cache)")
        sp.add_argument("--mailto", default=None, help="OpenAlex polite-pool email")
    for sp in (s,):
        sp.add_argument("--s2-cache", default=None,
                        help="S2 cache directory (default: <out dir>/.s2cache)")
        sp.add_argument("--s2-key", default=None, help="Semantic Scholar API key")

    args = p.parse_args(argv)
    args.mailto = oa.resolve_mailto(args.mailto)
    if args.cache is None:
        args.cache = str(Path(args.out).resolve().parent / ".cache")
    if getattr(args, "s2_cache", None) is None:
        args.s2_cache = str(Path(args.out).resolve().parent / ".s2cache")

    result = mode_search(args) if args.mode == "search" else mode_reverse(args)
    result["generated_at"] = oa.today()
    result["count"] = len(result["candidates"])
    oa.write_json(args.out, result)

    print(f"\nWrote {args.out} ({result['count']} candidates). Review the starting "
          f"papers and selection reasons, then use build_lineage.py --seeds <ids> "
          f"to collect references.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
