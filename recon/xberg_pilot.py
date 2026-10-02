"""Xberg pilot: extract a sample of documents with xberg and score the output
against the current pdfplumber sidecars. No pipeline code is touched; outputs
land in data/pilot/xberg/.

Usage: uv run python recon/xberg_pilot.py [--doc ID ...]
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xberg
from core.dnrs import DNR_RE, normalize_dnr

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "db.sqlite3"
DERIVED = BASE / "data" / "derived"
OUT = BASE / "data" / "pilot" / "xberg"

CONFIG = xberg.ExtractionConfig(
    output_format="markdown",
    use_layout_for_markdown=True,
    include_document_structure=True,
    extraction_timeout_secs=600,
)

TOKEN_RE = re.compile(r"[A-Za-zÅÄÖåäö0-9]+")
NUMBER_RE = re.compile(r"\d[\d .,:%-]*\d|\d")
SIDA_RE = re.compile(r"Sida\s+\d+\s*\(\d+\)", re.IGNORECASE)
DATUM_RE = re.compile(r"^\s*(?:Kommun[\w\s]*|.*nämnden)\s+Datum:", re.IGNORECASE | re.MULTILINE)
SECTION_HEADING_RE = re.compile(r"^#{1,6}\s*(.*§\s*\d+|.*)", re.MULTILINE)


def tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(text)}


def numbers(text: str) -> set[str]:
    return {n.strip(" .,-") for n in NUMBER_RE.findall(text) if n.strip(" .,-")}


def sidecar_text(sha: str) -> str:
    path = DERIVED / f"{sha}.extraction.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return "\n".join(p["text"] for p in data["pages"])


def pick_samples(con: sqlite3.Connection) -> list[dict]:
    """One or two docs per interesting category, joined with their sidecar sha."""
    queries = [
        ("kallelse", "d.doc_type='kallelse'"),
        ("protokoll", "d.doc_type='protokoll'"),
        ("motion-stamp", "d.id=292"),  # dnr ONLY in Inkom stamp
        ("interpellation", "d.doc_type='interpellation'"),
        ("utdrag-headers", "d.id=209"),  # 85 pages, header on every page
        ("budget-tables", "d.id=256"),  # Mål och budget, Tabell pages
        ("mkb-compilation", "d.id=9"),  # 331-page compilation
        ("soup-466", "d.id=466"),  # worst table soup
        ("soup-59", "d.id=59"),
        ("ocr-version", "dv.ocr_used=1"),
        ("stamp-63", "d.id=63"),  # dnr only in stamp, stamp from p8 on
    ]
    samples = []
    for label, where in queries:
        row = con.execute(
            f"""SELECT d.id, d.doc_type, d.title, dv.sha256, dv.ocr_used
            FROM core_documentversion dv JOIN core_document d ON d.id=dv.document_id
            WHERE dv.id=d.current_version_id AND {where} LIMIT 1"""
        ).fetchone()
        if row:
            samples.append({"label": label, **dict(row)})
    return samples


def score(label: str, row: sqlite3.Row, md: str, doc, pages_meta, elapsed: float) -> dict:
    ref = sidecar_text(row["sha256"])
    ref_tokens, new_tokens = tokens(ref), tokens(md)
    ref_nums, new_nums = numbers(ref), numbers(md)
    ref_dnrs = {normalize_dnr(d) for d in DNR_RE.findall(ref)}
    new_dnrs = {normalize_dnr(d) for d in DNR_RE.findall(md)}
    headings = [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.*)", md, re.MULTILINE)]
    sec_headings = [h for h in headings if re.search(r"§\s*\d+", h)]
    dnr_headings = [h for h in headings if DNR_RE.search(h)]
    tables = len(doc.tables) if doc.tables else 0
    return {
        "label": label,
        "doc_id": row["id"],
        "doc_type": row["doc_type"],
        "pages_sidecar": len(json.loads((DERIVED / f"{row['sha256']}.extraction.json").read_text(encoding='utf-8'))["pages"]),
        "pages_xberg": pages_meta,
        "elapsed_s": round(elapsed, 1),
        "method": str(doc.extraction_method),
        "quality": doc.quality_score,
        "md_chars": len(md),
        "token_recall": round(len(ref_tokens & new_tokens) / max(1, len(ref_tokens)), 3),
        "number_recall": round(len(ref_nums & new_nums) / max(1, len(ref_nums)), 3),
        "dnr_recall": round(len(ref_dnrs & new_dnrs) / max(1, len(ref_dnrs)), 3),
        "dnrs_ref": len(ref_dnrs),
        "dnrs_xberg": len(new_dnrs),
        "dnrs_lost": sorted(ref_dnrs - new_dnrs)[:5],
        "inkom_stamp": ("Inkom till" in ref, "Inkom till" in md),
        "sida_refs_ref": len(SIDA_RE.findall(ref)),
        "sida_refs_xberg": len(SIDA_RE.findall(md)),
        "datum_headers_ref": len(DATUM_RE.findall(ref)),
        "datum_headers_xberg": len(DATUM_RE.findall(md)),
        "headings": len(headings),
        "section_headings": len(sec_headings),
        "dnr_headings": len(dnr_headings),
        "tables": tables,
    }


def node_summary(doc, limit: int = 3) -> dict:
    d = doc.document
    if d is None:
        return {}
    nodes = list(d.nodes)
    by_type: dict[str, int] = {}
    for n in nodes:
        t = str(getattr(n, "node_type", getattr(n, "content_layer", "?")))
        by_type[t] = by_type.get(t, 0) + 1
    return {"node_count": len(nodes), "node_types": by_type}


async def run(samples: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in samples:
        pdf = (BASE / "data" / "originals" / (
            sqlite3.connect(str(DB)).execute(
                "SELECT stored_path FROM core_documentversion WHERE sha256=?", (s["sha256"],)
            ).fetchone()[0])
        )
        t0 = time.time()
        try:
            result = await xberg.extract(xberg.ExtractInput(kind="uri", uri=str(pdf)), CONFIG)
        except Exception as exc:  # noqa: BLE001
            print(f"{s['label']}: FAILED {type(exc).__name__}: {str(exc)[:200]}")
            rows.append({"label": s["label"], "doc_id": s["id"], "error": str(exc)[:300]})
            continue
        elapsed = time.time() - t0
        doc = result.results[0]
        md = doc.content
        pages_meta = len(doc.pages) if doc.pages else 0

        stem = f"{s['id']:04d}_{s['label']}"
        (OUT / f"{stem}.md").write_text(md, encoding="utf-8")
        summary = node_summary(doc)
        (OUT / f"{stem}.structure.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        if s["label"] == "budget-tables" and doc.tables:
            t = doc.tables[0]
            cells = getattr(t, "cells", None) or getattr(t, "grid", None)
            (OUT / f"{stem}.table0.json").write_text(
                json.dumps(str(cells)[:4000], ensure_ascii=False, indent=1), encoding="utf-8"
            )

        row = score(s["label"], s, md, doc, pages_meta, elapsed)
        row.update(summary)
        rows.append(row)
        print(f"{row['label']}: {row['elapsed_s']}s pages={row['pages_xberg']} "
              f"tok={row['token_recall']} num={row['number_recall']} dnr={row['dnr_recall']} "
              f"({row['dnrs_ref']}->{row['dnrs_xberg']}) sida {row['sida_refs_ref']}->{row['sida_refs_xberg']} "
              f"heads={row['headings']} §heads={row['section_headings']} tables={row['tables']}")

    (OUT / "metrics.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nmetrics written to {OUT / 'metrics.json'}")


def main() -> None:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    samples = pick_samples(con)
    if "--doc" in sys.argv:
        ids = {int(v) for v in sys.argv[sys.argv.index("--doc") + 1:]}
        samples = [s for s in samples if s["id"] in ids] or [
            {"label": f"doc-{i}", **dict(con.execute(
                "SELECT d.id, d.doc_type, d.title, dv.sha256, dv.ocr_used FROM core_documentversion dv "
                "JOIN core_document d ON d.id=dv.document_id WHERE dv.id=d.current_version_id AND d.id=?",
                (i,)).fetchone())}
            for i in ids
        ]
    asyncio.run(run(samples))


if __name__ == "__main__":
    main()
