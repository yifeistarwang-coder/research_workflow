#!/usr/bin/env python3
"""Semantic Scholar client for paper metadata and reference lists.

Requests use a local cache, a fixed interval and retries for temporary failures.
An API key may be supplied through --s2-key or S2_API_KEY. Cache entries do not
expire; callers should retain the actual fetch date when reporting counts.

Lookups support paper IDs, arXiv IDs, DOIs and title matching. Callers decide how
to handle unavailable records and incomplete reference lists.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oalib as oa  # noqa: E402

S2_BASE = "https://api.semanticscholar.org/graph/v1"
MIN_INTERVAL = 1.15          # seconds between uncached requests
_last_request = [0.0]


class S2Unavailable(RuntimeError):
    """Raised when S2 keeps returning 429/5xx so callers can degrade."""


def resolve_key(cli_value: str | None) -> str | None:
    return cli_value or os.environ.get("S2_API_KEY") or None


def paper_id_for(record: dict) -> str | None:
    """Build an S2 paper id from an OpenAlex record (arXiv preferred)."""
    arxiv = record.get("arxiv_id")
    if arxiv:
        return f"arXiv:{arxiv}"
    doi = (record.get("doi") or "").strip().lower()
    if not doi:
        return None
    if doi.startswith("10.48550/arxiv."):
        return f"arXiv:{doi.split('arxiv.', 1)[1]}"
    return f"DOI:{doi}"


def _get(url: str, *, cache_dir=None, key: str | None = None, retries: int = 5,
         timeout: int = 60) -> dict:
    cache = None
    if cache_dir:
        import hashlib
        cache = Path(cache_dir) / f"s2_{hashlib.sha1(url.encode()).hexdigest()[:20]}.json"
        if cache.exists():
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

    headers = {"User-Agent": "lit-survey/1.0"}
    if key:
        headers["x-api-key"] = key
    last_err: Exception | None = None
    for attempt in range(retries):
        wait = MIN_INTERVAL - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _last_request[0] = time.time()
                data = json.loads(resp.read().decode("utf-8"))
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        except urllib.error.HTTPError as exc:
            _last_request[0] = time.time()
            last_err = exc
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(min(3.0 * (attempt + 1), 20))
                continue
            if exc.code == 404:
                return {}          # paper not in S2: not an error, just no data
            raise RuntimeError(f"S2 HTTP {exc.code} for {url}: {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001
            _last_request[0] = time.time()
            last_err = exc
            time.sleep(2.0 * (attempt + 1))
    raise S2Unavailable(f"S2 unavailable after {retries} attempts ({last_err})")


def paper(paper_id: str, *, cache_dir=None, key=None) -> dict:
    fields = "title,year,venue,citationCount,referenceCount,externalIds,abstract"
    url = (f"{S2_BASE}/paper/{urllib.parse.quote(paper_id, safe=':')}"
           f"?fields={fields}")
    return _get(url, cache_dir=cache_dir, key=key)


def references(paper_id: str, *, limit: int = 1000, cache_dir=None, key=None) -> list:
    """Papers that ``paper_id`` cites. Returns [] when S2 has no record."""
    fields = "title,year,citationCount,venue,externalIds,referenceCount"
    out: list = []
    offset = 0
    while len(out) < limit:
        chunk = min(100, limit - len(out))
        url = (f"{S2_BASE}/paper/{urllib.parse.quote(paper_id, safe=':')}/references"
               f"?fields={fields}&limit={chunk}&offset={offset}")
        data = _get(url, cache_dir=cache_dir, key=key)
        rows = data.get("data") or []
        if not rows:
            break
        out.extend(row.get("citedPaper") or {} for row in rows)
        offset = data.get("next") or (offset + len(rows))
        if not data.get("next"):
            break
    return out[:limit]


def citations(paper_id: str, *, limit: int = 500, cache_dir=None, key=None) -> list:
    """Papers that cite ``paper_id``, in the order returned by the API."""
    fields = "title,year,citationCount,venue,externalIds,referenceCount"
    out: list = []
    offset = 0
    while len(out) < limit:
        chunk = min(100, limit - len(out))
        url = (f"{S2_BASE}/paper/{urllib.parse.quote(paper_id, safe=':')}/citations"
               f"?fields={fields}&limit={chunk}&offset={offset}")
        data = _get(url, cache_dir=cache_dir, key=key)
        rows = data.get("data") or []
        if not rows:
            break
        out.extend(row.get("citingPaper") or {} for row in rows)
        offset = data.get("next") or (offset + len(rows))
        if not data.get("next"):
            break
    return out[:limit]


def probed_paper(paper_id: str, *, cache_dir=None, key=None) -> dict | None:
    """Return {} fields or None when S2 has no record for this id."""
    try:
        rec = paper(paper_id, cache_dir=cache_dir, key=key)
    except S2Unavailable:
        raise
    settle = rec.get("title")
    return rec if settle else None


def match_title(title: str, *, cache_dir=None, key=None) -> str | None:
    """Resolve a title to an S2 paperId via the /search/match endpoint.

    Used only when a record has neither an arXiv id nor a usable DOI.
    Returns a paperId (usable as an S2 id) or None.
    """
    url = (f"{S2_BASE}/paper/search/match?query={urllib.parse.quote(title)}"
           f"&fields=title,year,citationCount,externalIds")
    try:
        data = _get(url, cache_dir=cache_dir, key=key)
    except S2Unavailable:
        return None
    rows = data.get("data") or []
    if not rows:
        return None
    return rows[0].get("paperId") or None


def s2_citation_count(record: dict, *, cache_dir=None, key=None) -> int | None:
    """Reliable citation count via S2; None when unavailable/unmatched."""
    paper_id = paper_id_for(record)
    if not paper_id:
        return None
    try:
        rec = paper(paper_id, cache_dir=cache_dir, key=key)
    except S2Unavailable:
        return None
    return rec.get("citationCount")


def to_record(s2_paper: dict) -> dict:
    """Shape an S2 paper object like an oalib record (best effort)."""
    ext = s2_paper.get("externalIds") or {}
    doi = ext.get("DOI") or ""
    return {
        "id": "",                       # S2-only node has no OpenAlex id
        "title": (s2_paper.get("title") or "").strip(),
        "year": s2_paper.get("year"),
        "cites": s2_paper.get("citationCount") or 0,
        "type": "",
        "venue": s2_paper.get("venue") or "",
        "doi": doi,
        "authors": [],
        "abstract": s2_paper.get("abstract") or "",
        "is_oa": False,
        "oa_status": "",
        "oa_url": "",
        "pdf_url": "",
        "arxiv_id": ext.get("ArXiv"),
        "referenced_works": [],
        "is_retracted": False,
        "s2_id": s2_paper.get("paperId") or "",
    }


if __name__ == "__main__":
    # Tiny self-check: python3 s2lib.py "arXiv:2005.11401"
    pid = sys.argv[1] if len(sys.argv) > 1 else "arXiv:2005.11401"
    rec = probed = None
    try:
        probed = paper(pid)
    except S2Unavailable as exc:
        print(f"S2 unavailable: {exc}")
        raise SystemExit(2)
    print(json.dumps({k: probed.get(k) for k in
                      ("title", "year", "citationCount", "referenceCount")},
                     ensure_ascii=False, indent=2))
    refs = references(pid, limit=5)
    print("top references:", [(r.get("year"), r.get("citationCount"), (r.get("title") or "")[:50])
                              for r in refs])
