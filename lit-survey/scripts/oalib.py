#!/usr/bin/env python3
"""OpenAlex client and record helpers for lit-survey.

Uses the standard library with request throttling, retries and optional disk
caching. Pass --mailto or set OPENALEX_MAILTO to provide a contact email.
Cached responses have no expiry. Check paper versions and actual fetch dates
before using metadata or citation counts in a report.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

OA_BASE = "https://api.openalex.org"
UA = "lit-survey/1.0 (OpenAlex client; mailto:{mailto})"
MAX_PER_BATCH = 50          # OpenAlex pipe-filter limit for ids.openalex
MIN_INTERVAL = 0.12         # seconds between uncached requests

_last_request = [0.0]

ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?")
ARXIV_OLD_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")


# --------------------------------------------------------------------------- #
# http + cache
# --------------------------------------------------------------------------- #
def _cache_path(cache_dir: Path | None, url: str) -> Path | None:
    if cache_dir is None:
        return None
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    return Path(cache_dir) / f"{key}.json"


def api_get(url: str, *, cache_dir=None, mailto: str | None = None,
            retries: int = 4, timeout: int = 60) -> dict:
    """GET a JSON document from OpenAlex with retries, throttling and cache."""
    cache = _cache_path(Path(cache_dir) if cache_dir else None, url)
    if cache is not None and cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass  # corrupted cache entry: refetch

    headers = {"User-Agent": UA.format(mailto=mailto or "anonymous@example.com"),
               "Accept": "application/json"}
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
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RuntimeError(f"OpenAlex HTTP {exc.code} for {url}: {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001 - network errors vary by platform
            _last_request[0] = time.time()
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"OpenAlex request failed after {retries} attempts: {url} ({last_err})")


def _with_params(url: str, params: dict | None, mailto: str | None) -> str:
    params = dict(params or {})
    if mailto:
        params.setdefault("mailto", mailto)
    if params:
        sep = "&" if "?" in url else "?"
        # quote (not quote_plus): OpenAlex filters use spaces literally, e.g.
        # filter=title.search:speculative%20decoding
        url = f"{url}{sep}{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"
    return url


# --------------------------------------------------------------------------- #
# work queries
# --------------------------------------------------------------------------- #
def normalize_id(value: str) -> str:
    """'https://openalex.org/W123' / 'w123' -> 'W123'."""
    value = (value or "").strip().rstrip("/")
    value = value.rsplit("/", 1)[-1]
    if re.fullmatch(r"[Ww]\d+", value):
        return "W" + value[1:]
    raise ValueError(f"not an OpenAlex work id: {value!r}")


def short_id(url_or_id: str) -> str:
    return (url_or_id or "").rsplit("/", 1)[-1]


def search_works(query: str, *, filters: str = "", sort: str = "cited_by_count:desc",
                 per_page: int = 25, page: int = 1, select: str | None = None,
                 cache_dir=None, mailto: str | None = None) -> dict:
    params = {"search": query, "per-page": per_page, "page": page}
    if filters:
        params["filter"] = filters
    if sort:
        params["sort"] = sort
    if select:
        params["select"] = select
    return api_get(_with_params(f"{OA_BASE}/works", params, mailto),
                   cache_dir=cache_dir, mailto=mailto)


def fetch_works(ids, *, select: str | None = None, cache_dir=None,
                mailto: str | None = None) -> dict:
    """Batch-fetch works by id (<=50 per request). Returns {short_id: work}."""
    ids = [normalize_id(i) for i in ids]
    out: dict[str, dict] = {}
    chunks = [ids[i:i + MAX_PER_BATCH] for i in range(0, len(ids), MAX_PER_BATCH)]
    for chunk in chunks:
        params = {"filter": "ids.openalex:" + "|".join(chunk), "per-page": len(chunk)}
        if select:
            params["select"] = select
        data = api_get(_with_params(f"{OA_BASE}/works", params, mailto),
                       cache_dir=cache_dir, mailto=mailto)
        for work in data.get("results", []):
            out[short_id(work["id"])] = work
    return out


def forward_citations(work_id: str, *, min_cites: int = 100, per_page: int = 100,
                      max_items: int = 200, cache_dir=None, mailto: str | None = None) -> list:
    """Works that cite ``work_id``, highest-cited first, server-side filtered."""
    work_id = normalize_id(work_id)
    filters = f"cites:{work_id}"
    if min_cites > 0:
        filters += f",cited_by_count:>{min_cites}"
    select = ("id,title,publication_year,cited_by_count,type,primary_location,"
              "best_oa_location,open_access,ids,authorships")
    items: list = []
    page = 1
    while len(items) < max_items:
        data = search_works("", filters=filters, sort="cited_by_count:desc",
                            per_page=min(per_page, max_items - len(items)), page=page,
                            select=select, cache_dir=cache_dir, mailto=mailto)
        results = data.get("results", [])
        if not results:
            break
        items.extend(results)
        if len(results) < per_page:
            break
        page += 1
        if page > 10:
            break
    return items[:max_items]


# --------------------------------------------------------------------------- #
# record shaping
# --------------------------------------------------------------------------- #
def reconstruct_abstract(inverted: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild plain text."""
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, spots in inverted.items():
        for spot in spots:
            positions[spot] = word
    return " ".join(positions[i] for i in sorted(positions))


def arxiv_id_of(work: dict) -> str | None:
    for loc in work.get("locations") or []:
        for key in ("landing_page_url", "pdf_url"):
            url = loc.get(key) or ""
            m = ARXIV_ID_RE.search(url) or ARXIV_OLD_RE.search(url)
            if m:
                return m.group(1)
    return None


def venue_of(work: dict) -> str:
    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    return source.get("display_name") or (work.get("host_venue") or {}).get("display_name") or ""


def to_record(work: dict) -> dict:
    """Normalize a raw OpenAlex work into the flat record used across phases."""
    ids = work.get("ids") or {}
    oa = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    authors = [a.get("author", {}).get("display_name", "") for a in work.get("authorships") or []]
    record = {
        "id": short_id(work.get("id", "")),
        "title": re.sub(r"\s+", " ", work.get("title") or work.get("display_name") or "").strip(),
        "year": work.get("publication_year"),
        "cites": work.get("cited_by_count") or 0,
        "type": work.get("type") or "",
        "venue": venue_of(work),
        "doi": (ids.get("doi") or "").replace("https://doi.org/", ""),
        "authors": [a for a in authors if a],
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "is_oa": bool(oa.get("is_oa")),
        "oa_status": oa.get("oa_status") or "",
        "oa_url": oa.get("oa_url") or best.get("pdf_url") or "",
        "pdf_url": best.get("pdf_url") or "",
        "arxiv_id": arxiv_id_of(work),
        "referenced_works": [short_id(r) for r in work.get("referenced_works") or []],
        "is_retracted": bool(work.get("is_retracted")),
    }
    return record


def abstract_excerpt(record: dict, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", record.get("abstract") or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " [...]"


def first_author(record: dict) -> str:
    return (record.get("authors") or ["?"])[0] or "?"


def cite_key(record: dict) -> str:
    """Deterministic BibTeX key: firstauthorlastname + year + openalex id."""
    author = first_author(record)
    surname = re.sub(r"[^A-Za-z]", "", author.split()[-1]) if author != "?" else "anon"
    return f"{surname.lower() or 'anon'}{record.get('year') or 'n'}{record.get('id', '')}"


def to_bibtex(records) -> str:
    entries = []
    for rec in records:
        fields = [
            ("title", (rec.get("title") or "").replace("{", "").replace("}", "")),
            ("author", " and ".join(rec.get("authors") or [])),
            ("year", str(rec.get("year") or "")),
        ]
        if rec.get("venue"):
            fields.append(("journal" if rec.get("type") in ("article", "review") else "booktitle",
                           rec["venue"]))
        if rec.get("doi"):
            fields.append(("doi", rec["doi"]))
        if rec.get("oa_url"):
            fields.append(("url", rec["oa_url"]))
        if rec.get("arxiv_id"):
            fields.append(("eprint", rec["arxiv_id"]))
            fields.append(("archivePrefix", "arXiv"))
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
        entries.append(f"@misc{{{cite_key(rec)},\n{body}\n}}")
    return "\n\n".join(entries) + "\n"


# --------------------------------------------------------------------------- #
# presentation helpers
# --------------------------------------------------------------------------- #
def fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def truncate(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def md_table(headers: list, rows: list) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(lines)


def slug(text: str, limit: int = 48) -> str:
    text = (text or "untitled").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:limit].rstrip("-") or "untitled")


def write_json(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def today() -> str:
    return time.strftime("%Y-%m-%d")


def resolve_mailto(cli_value: str | None) -> str | None:
    return cli_value or os.environ.get("OPENALEX_MAILTO") or None


# --------------------------------------------------------------------------- #
# topicality helpers (shared by find_seeds.py and build_lineage.py)
# --------------------------------------------------------------------------- #
STOPWORDS = {
    "model", "models", "modeling", "method", "methods", "learning", "network",
    "networks", "system", "systems", "data", "analysis", "using", "based",
    "approach", "approaches", "framework", "frameworks", "study", "training",
    "language", "languages", "large", "deep", "neural", "application",
    "applications", "efficient", "efficiency", "scale", "scaling", "survey",
    "towards", "toward", "beyond", "under", "with", "from", "that", "this",
}


def title_roots(texts) -> list[str]:
    """Word roots (first 6 chars) that identify the field, minus stopwords.

    Used to tell a field paper ("Speculative Decoding: ...") from the general
    background it cites (BERT, Adam, Attention).
    """
    roots: set[str] = set()
    for text in texts:
        for word in re.split(r"[^a-z0-9]+", (text or "").lower()):
            if len(word) >= 5 and word not in STOPWORDS and word.rstrip("s") not in STOPWORDS:
                roots.add(word[:6])
    return sorted(roots)


def title_hit(title: str, roots: list[str]) -> bool:
    if not roots:
        return True
    low = (title or "").lower()
    return any(root in low for root in roots)
