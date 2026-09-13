#!/usr/bin/env python3
"""Check the structure of survey files that use the bundled script layout.

Compares notes and reports with ``lineage.json`` using filenames, selected ID
patterns, citation counts, section keywords and page markers. Also checks for
related-work entries, a gap keyword and selected placeholder words.

These are format checks. They do not verify source content, the meaning of a
citation, a researcher's reading progress or the completeness of a survey.
The current checks expect numeric citation counts and page markers even when
those are unavailable; see references/script-usage.md for workflow limits.

Lines report PASS / FAIL / WARN / SKIP, followed by OVERALL PASS or OVERALL FAIL.
Exit code is 0 when none of the checks fail.

Usage
-----
    python3 verify_survey.py --workdir rag

Options
-------
    --strict-ids      recognized unknown IDs cause FAIL (default)
    --allow-external  permit IDs outside lineage.json
    --no-related-work skip the related-work digest requirement
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oalib as oa  # noqa: E402

ID_PATTERNS = [
    re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})", re.I),
    re.compile(r"DOI:\s*(10\.\d{4,9}/[^\s|]+)", re.I),
    re.compile(r"\b(W\d{6,})\b"),
    re.compile(r"openalex\.org/(W\d{6,})"),
]
PLACEHOLDER_RE = re.compile(r"\b(TODO|TBD|FIXME|XXX|待补|待定|填写|placeholder)\b", re.I)
PAGE_RE = re.compile(r"\[p\.?\s*\d+|第\s*\d+\s*页|p\.\s*\d+")
SECTION_HINTS = {
    "problem": ("问题", "动机", "背景", "解决什么", "problem", "motivation"),
    "mechanism": ("机制", "方法", "做法", "核心", "mechanism", "method", "approach"),
    "evidence": ("结果", "证据", "实验", "数据", "result", "evidence", "experiment"),
    "relation": ("与前作", "与前一个", "关系", "脉络", "相对", "比较", "relation", "lineage",
                 "contrast"),
    "open": ("遗留", "局限", "未解决", "开放问题", "open question", "limitation"),
}


def sanitize(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", node_id).strip("_")


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed = False
        self.warned = False

    def check(self, ok: bool | None, name: str, detail: str = "") -> None:
        if ok is None:
            tag = "SKIP"
        elif ok:
            tag = "PASS"
        else:
            tag = "FAIL"
            self.failed = True
        self.lines.append(f"{tag}  {name}" + (f"  — {detail}" if detail else ""))

    def warn(self, name: str, detail: str = "") -> None:
        self.warned = True
        self.lines.append(f"WARN  {name}" + (f"  — {detail}" if detail else ""))

    def render(self) -> str:
        verdict = "OVERALL FAIL" if self.failed else "OVERALL PASS"
        if not self.failed and self.warned:
            verdict = "OVERALL PASS (with warnings)"
        return "\n".join(self.lines + ["", verdict]) + "\n"


def collect_md_files(workdir: Path) -> dict[str, list[Path]]:
    def mds(folder: Path) -> list[Path]:
        return sorted(folder.glob("*.md")) if folder.is_dir() else []

    return {
        "survey": mds(workdir),
        "backbone": mds(workdir / "backbone"),
        "digests": mds(workdir / "digests"),
    }


def extract_arxiv_ids(text: str) -> set[str]:
    found: set[str] = set()
    for m in ID_PATTERNS[0].finditer(text):
        found.add(m.group(1))
    return found


def extract_dois(text: str) -> set[str]:
    found: set[str] = set()
    for m in ID_PATTERNS[1].finditer(text):
        found.add(m.group(1).rstrip(").,;。）"))
    return found


def extract_openalex_ids(text: str) -> set[str]:
    found: set[str] = set()
    for pat in ID_PATTERNS[2:]:
        for m in pat.finditer(text):
            found.add(m.group(1))
    return found


def known_id_sets(nodes: list[dict]) -> tuple[set, set, dict]:
    arxiv, dois, wol = set(), set(), {}
    for node in nodes:
        if node.get("arxiv_id"):
            arxiv.add(node["arxiv_id"].lower())
        if node.get("doi"):
            dois.add(node["doi"].lower())
        if node.get("openalex_id"):
            wol[node["openalex_id"]] = node
            wol[node["openalex_id"].lower()] = node
    return arxiv, dois, wol


def section_present(text: str, hints: tuple) -> bool:
    head = text.lower()
    for line in text.splitlines():
        if line.strip().startswith("#") and any(h.lower() in line.lower() for h in hints):
            return True
    return any(h.lower() in head for h in hints[:2])


def check_citation_numbers(note_text: str, node: dict, rep: Report, label: str) -> None:
    """If the note states a citation count, it must match the lineage snapshot."""
    counted = re.findall(r"([\d,]{2,})\s*(?:次)?\s*(?:引用|citations?|cites)", note_text)
    if not counted:
        rep.check(False, f"{label}: citation count stated",
                  "no 'N citations' figure found; readers need the scale of the paper")
        return
    expected = {node.get("cites"), node.get("oa_cites")}
    expected = {int(e) for e in expected if isinstance(e, int)}
    bad = []
    for raw in counted:
        try:
            value = int(raw.replace(",", "").replace(" ", ""))
        except ValueError:
            continue
        if expected and all(abs(value - e) > max(2, 0.02 * e) for e in expected):
            bad.append(raw)
    rep.check(not bad, f"{label}: citation count matches lineage snapshot",
              f"mismatch {bad} vs lineage {sorted(expected)}" if bad else "")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workdir", required=True)
    p.add_argument("--strict-ids", action="store_true", default=True)
    p.add_argument("--allow-external", dest="strict_ids", action="store_false",
                   help="unknown ids are WARN, not FAIL")
    p.add_argument("--no-related-work", dest="require_related", action="store_false",
                   default=True)
    args = p.parse_args(argv)

    workdir = Path(args.workdir)
    rep = Report()
    lineage_path = workdir / "lineage.json"
    if not lineage_path.exists():
        rep.check(False, "lineage.json present", f"missing {lineage_path}")
        print(rep.render())
        return 1
    data = oa.read_json(lineage_path)
    nodes = data.get("nodes") or []
    seeds, edges = data.get("seeds") or [], data.get("edges") or []
    rep.check(bool(nodes), "lineage.json schema", f"{len(nodes)} nodes, {len(edges)} edges, "
              f"{len(seeds)} seeds")
    by_id = {n["id"]: n for n in nodes}
    known_arxiv, known_doi, known_w = known_id_sets(nodes)

    files = collect_md_files(workdir)
    rep.check((workdir / "timeline.md").exists(), "timeline.md present")
    rep.check(bool(files["backbone"]), "backbone/ notes exist",
              f"{len(files['backbone'])} note(s)")

    backbone_nodes = [n for n in nodes if n.get("tier") == "backbone"]
    backbone_names = {f.stem for f in files["backbone"]}
    missing = [n["id"] for n in backbone_nodes
               if sanitize(n["id"]) not in backbone_names
               and not any(sanitize(n["id"]) in name for name in backbone_names)]
    rep.check(not missing, "backbone coverage",
              f"no note for tier=backbone: {', '.join(missing[:6])}" if missing else
              f"{len(backbone_nodes)} backbone node(s) covered")

    note_by_node: dict[str, str] = {}
    for path in files["backbone"]:
        text = path.read_text(encoding="utf-8")
        label = f"note {path.name}"
        node = by_id.get(path.stem) or next(
            (n for n in nodes if sanitize(n["id"]) == path.stem
             or path.stem.endswith(sanitize(n["id"]))), None)
        if node is None:
            note_by_node[path.stem] = text
            rep.warn(f"{label}: identity", "filename does not match any lineage node id")
        else:
            note_by_node[node["id"]] = text
            check_citation_numbers(text, node, rep, f"{label} ({node['id']})")
        rep.check(PAGE_RE.search(text) is not None, f"{label}: page-level citations",
                  "" if PAGE_RE.search(text) else "no [p.N] / 第 N 页 markers found")
        for key, hints in SECTION_HINTS.items():
            rep.check(section_present(text, hints), f"{label}: covers {key}")
        rep.check(PLACEHOLDER_RE.search(text) is None, f"{label}: no placeholders")

    survey_files = [f for f in files["survey"]
                    if f.name in ("survey.md", "SURVEY.md", "report.md", "README.md")]
    survey_text = survey_files[0].read_text(encoding="utf-8") if survey_files else ""
    rep.check(bool(survey_files), "survey.md present")
    if survey_files:
        rep.check("mermaid" in survey_text.lower() or "主线" in survey_text,
                  "survey.md: main-line diagram or narrative")
        rep.check(any(h in survey_text for h in ("gap", "机会", "空白", "未解决", "open")),
                  "survey.md: gap / opportunity section")
        covered = sum(1 for n in backbone_nodes if n["id"] in survey_text)
        rep.check(covered == len(backbone_nodes),
                  "survey.md: mentions every backbone node",
                  f"{covered}/{len(backbone_nodes)}")
        rep.check(PLACEHOLDER_RE.search(survey_text) is None, "survey.md: no placeholders")

    if args.require_related:
        digest_files = files["digests"] + [f for f in files["survey"]
                                           if "related" in f.name.lower()]
        rep.check(bool(digest_files), "related-work digest exists")
        for path in digest_files:
            text = path.read_text(encoding="utf-8")
            entries = re.findall(r"^\s*(?:[-*]|\d+\.|###+)\s+(.*)$", text, re.M)
            weak = [e for e in entries
                    if any(t in e for t in ("arXiv:", "DOI:", "W" + "0"))
                    and not any(k in e for k in ("差异", "相比", "区别", "不同", "vs", "→", "—"))]
            rep.check(not weak, f"{path.name}: every entry states the delta",
                      f"{len(weak)} entry(ies) lack a 'how it differs' clause"
                      if weak else f"{len(entries)} entries")
            rep.check(PLACEHOLDER_RE.search(text) is None, f"{path.name}: no placeholders")

    # Check recognized identifiers across the generated Markdown files.
    all_files = [f for fs in files.values() for f in fs] + \
                ([workdir / "timeline.md"] if (workdir / "timeline.md").exists() else [])
    unknown: list[str] = []
    for path in all_files:
        text = path.read_text(encoding="utf-8")
        for arxiv in extract_arxiv_ids(text):
            if arxiv.lower() not in known_arxiv:
                unknown.append(f"{path.name}: arXiv:{arxiv}")
        for doi in extract_dois(text):
            if doi.lower() not in known_doi:
                unknown.append(f"{path.name}: DOI:{doi}")
        for wid in extract_openalex_ids(text):
            if wid not in known_w and wid.lower() not in known_w:
                unknown.append(f"{path.name}: {wid}")
    if unknown:
        detail = "; ".join(unknown[:8]) + (f" (+{len(unknown) - 8} more)" if len(unknown) > 8 else "")
        rep.check(None, "no unknown ids in prose", detail)
        if args.strict_ids:
            rep.lines[-1] = "FAIL  " + rep.lines[-1][6:]
            rep.failed = True
    else:
        rep.check(True, "no unknown ids in prose",
                  "every reference resolves to a lineage node")

    bib = workdir / "refs.bib"
    if bib.exists():
        text = bib.read_text(encoding="utf-8")
        rep.check(text.count("@") >= 1 and text.count("{") == text.count("}"),
                  "refs.bib well-formed", f"{text.count('@')} entries")

    print(rep.render())
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
