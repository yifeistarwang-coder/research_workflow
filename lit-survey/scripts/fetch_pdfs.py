#!/usr/bin/env python3
"""Fetch PDFs and extract page-marked text for selected papers.

Reads ``lineage.json`` and uses available PDF URLs or supplied local files
(``--register-local <id>=<path.pdf>``). Text extraction requires PyMuPDF.

Text format:
    ===== [p.1] =====
    ...page 1 text...
    ===== [p.2] =====

Outputs (inside --workdir/papers)
---------------------------------
    <sanitized-id>.pdf      downloaded or supplied PDF
    <sanitized-id>.txt      extracted text with PDF page numbers
    FETCH_REPORT.md         per-paper download and extraction status

Examples
--------
    python3 fetch_pdfs.py --workdir rag --tier backbone
    python3 fetch_pdfs.py --workdir rag --ids arXiv:2005.11401,arXiv:2004.04906
    python3 fetch_pdfs.py --workdir rag --tier backbone --limit 5 --no-text
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oalib as oa  # noqa: E402

UA = "lit-survey/1.0 (paper fetcher)"
PDF_MAGIC = b"%PDF-"


def sanitize(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", node_id).strip("_")


def guess_pdf_url(node: dict) -> str | None:
    for key in ("pdf_url", "oa_url"):
        url = node.get(key) or ""
        if url.lower().endswith(".pdf") or "arxiv.org/pdf/" in url.lower():
            return url
    if node.get("arxiv_id"):
        return f"https://arxiv.org/pdf/{node['arxiv_id']}"
    url = node.get("pdf_url") or node.get("oa_url")
    return url or None


def download_pdf(url: str, dest: Path, timeout: int = 120) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            head = resp.read(2048)
            if PDF_MAGIC not in head[:1024] and "pdf" not in ctype:
                return False, f"not a PDF (content-type {ctype or '?'})"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fh:
                fh.write(head)
                while chunk := resp.read(1 << 16):
                    fh.write(chunk)
        return True, f"{dest.stat().st_size // 1024} KB"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def extract_text(pdf_path: Path, txt_path: Path) -> tuple[bool, str]:
    try:
        import pymupdf  # noqa: PLC0415
    except ImportError:
        return False, "PyMuPDF not installed (pip install pymupdf)"
    try:
        doc = pymupdf.open(pdf_path)
        if doc.page_count == 0:
            return False, "0 pages"
        parts = []
        for i, page in enumerate(doc, 1):
            parts.append(f"\n===== [p.{i}] =====\n" + page.get_text())
        txt_path.write_text("".join(parts), encoding="utf-8")
        chars = sum(len(p) for p in parts)
        if chars < 500:
            return False, f"only {chars} chars (scanned PDF?)"
        return True, f"{doc.page_count} pages, {chars} chars"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def select_nodes(data: dict, tier: str | None, ids: list[str] | None) -> list[dict]:
    nodes = data.get("nodes") or []
    if ids:
        wanted = {i.strip() for i in ids}
        return [n for n in nodes
                if n["id"] in wanted or n.get("openalex_id") in wanted]
    if tier and tier != "all":
        tiers = set(tier.split(","))
        return [n for n in nodes if n.get("tier") in tiers]
    return nodes


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workdir", required=True)
    p.add_argument("--tier", default="backbone,seed",
                   help="comma-separated tiers to fetch (default backbone,seed)")
    p.add_argument("--ids", default=None,
                   help="explicit node ids (overrides --tier), comma-separated")
    p.add_argument("--limit", type=int, default=12, help="max papers to fetch")
    p.add_argument("--force", action="store_true", help="re-download existing PDFs")
    p.add_argument("--no-text", action="store_true", help="skip text extraction")
    p.add_argument("--register-local", action="append", default=[],
                   metavar="ID=PATH", help="attach a manually downloaded PDF")
    args = p.parse_args(argv)

    workdir = Path(args.workdir)
    lineage_path = workdir / "lineage.json"
    if not lineage_path.exists():
        raise SystemExit(f"{lineage_path} not found; run build_lineage.py first")
    data = oa.read_json(lineage_path)
    papers_dir = workdir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    for spec in args.register_local:
        if "=" not in spec:
            raise SystemExit(f"--register-local expects ID=PATH, got {spec!r}")
        node_id, path = spec.split("=", 1)
        src = Path(path)
        if not src.exists():
            print(f"[register] {path} not found", file=sys.stderr)
            continue
        dest = papers_dir / f"{sanitize(node_id)}.pdf"
        dest.write_bytes(src.read_bytes())
        print(f"[register] {node_id} <- {path}")

    nodes = select_nodes(data, args.tier, args.ids.split(",") if args.ids else None)
    nodes = [n for n in nodes if n.get("title")]
    nodes.sort(key=lambda n: (n.get("tier") != "seed", -(n.get("score") or 0)))
    nodes = nodes[: args.limit]

    rows = []
    for node in nodes:
        stem = sanitize(node["id"])
        pdf_path = papers_dir / f"{stem}.pdf"
        txt_path = papers_dir / f"{stem}.txt"
        status = ""
        if pdf_path.exists() and not args.force:
            status = f"cached ({pdf_path.stat().st_size // 1024} KB)"
        else:
            url = guess_pdf_url(node)
            if not url:
                rows.append([node["id"], node.get("tier"), "no-oa-link", "-", node["title"][:60]])
                continue
            ok, info = download_pdf(url, pdf_path)
            status = ("downloaded " + info) if ok else f"FAILED: {info}"
            if not ok:
                rows.append([node["id"], node.get("tier"), status, url, node["title"][:60]])
                continue
        text_status = "-"
        if not args.no_text and pdf_path.exists():
            if txt_path.exists() and not args.force:
                text_status = "cached"
            else:
                ok, info = extract_text(pdf_path, txt_path)
                text_status = ("text ok " + info) if ok else f"text FAILED: {info}"
        print(f"[fetch] {node['id']}: {status} | {text_status}", file=sys.stderr)
        rows.append([node["id"], node.get("tier"), status, text_status, node["title"][:60]])

    report = ["# PDF fetch report", "",
              f"Workdir: `{workdir}` · nodes requested: {len(nodes)}", "",
              oa.md_table(["id", "tier", "pdf", "text", "title"], rows), ""]
    missing = [r[0] for r in rows if "FAILED" in r[2] or r[2] == "no-oa-link"]
    if missing:
        report.append("**Needs a manual PDF** (paywalled / not open access):")
        report.append("")
        for nid in missing:
            report.append(f"- `{nid}` → download it yourself, then run "
                          f"`--register-local {nid}=/path/to/file.pdf`")
        report.append("")
    (papers_dir / "FETCH_REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\nWrote {papers_dir / 'FETCH_REPORT.md'}")
    if missing:
        print(f"{len(missing)} node(s) need a manual PDF: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
