"""IR extraction (stage A): xberg renders a PDF into structured markdown.

Runs entirely outside database transactions. The full xberg output is cached in
a sidecar JSON keyed on sha256 and stamped with IR_VERSION, so later-stage bumps
(chunker/parsers) never re-pay extraction. OCR is tesseract with the project's
Swedish traineddata, used automatically for scanned pages only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

import xberg

log = logging.getLogger(__name__)

IR_VERSION = "xberg-md-v1"
EXTRACTION_TIMEOUT = 1800  # seconds per document (scanned compilations are slow)


class ExtractionError(Exception):
    pass


@dataclass
class IrResult:
    markdown: str
    ocr_used: bool
    page_count: int
    quality: float | None  # xberg's own confidence, when reported
    pages: list[dict]  # [{number, text}] for future page-level association


def _ocr_config() -> xberg.OcrConfig:
    return xberg.OcrConfig(
        enabled=True,
        backend="tesseract",
        language=["swe"],
        tessdata_path=settings.TESSDATA_PREFIX,
    )


def _config() -> xberg.ExtractionConfig:
    return xberg.ExtractionConfig(
        output_format="markdown",
        use_layout_for_markdown=True,
        include_document_structure=True,
        ocr=_ocr_config(),
        extraction_timeout_secs=EXTRACTION_TIMEOUT,
    )


def extract_ir(pdf_path: Path, derived_dir: Path, sha256: str) -> IrResult:
    """Extract one PDF into the IR. Sidecar is written atomically on success."""
    try:
        result = asyncio.run(xberg.extract(xberg.ExtractInput(kind="uri", uri=str(pdf_path)), _config()))
    except Exception as exc:  # noqa: BLE001 - converted to a recorded per-version failure
        raise ExtractionError(f"xberg failed: {type(exc).__name__}: {exc}") from exc
    if result.errors:
        raise ExtractionError(f"xberg reported errors: {result.errors[0]!s}"[:300])
    if not result.results:
        raise ExtractionError("xberg produced no result")
    doc = result.results[0]
    markdown = doc.content or ""
    if not markdown.strip():
        raise ExtractionError("no text could be extracted (all pages empty or OCR unavailable)")

    pages = []
    for page in doc.pages or []:
        number = getattr(page, "page_number", None)
        text = getattr(page, "content", "") or ""
        if number:
            pages.append({"number": number, "text": text})

    ir = IrResult(
        markdown=markdown,
        ocr_used=bool(getattr(doc, "extraction_method", "") != "native"),
        page_count=len(pages) or len(doc.pages or []),
        quality=doc.quality_score if isinstance(doc.quality_score, (int, float)) else None,
        pages=pages,
    )
    _write_sidecar(derived_dir, sha256, ir)
    return ir


def _write_sidecar(derived_dir: Path, sha256: str, ir: IrResult) -> None:
    derived_dir.mkdir(parents=True, exist_ok=True)
    sidecar = {
        "sha256": sha256,
        "ir_version": IR_VERSION,
        "markdown": ir.markdown,
        "ocr_used": ir.ocr_used,
        "page_count": ir.page_count,
        "quality": ir.quality,
        "pages": ir.pages,
    }
    path = derived_dir / f"{sha256}.ir.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _cached_ir(version) -> IrResult | None:
    """Reuse the sidecar when its IR version is current.

    Reprocessing (parser bumps) must never re-pay extraction; a sidecar produced
    by a different IR version is stale and the expensive path runs again.
    """
    if not version.sha256:
        return None
    path = settings.DERIVED_DIR / f"{version.sha256}.ir.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("ir_version") != IR_VERSION or not data.get("markdown", "").strip():
        return None
    return IrResult(
        markdown=data["markdown"],
        ocr_used=bool(data.get("ocr_used")),
        page_count=int(data.get("page_count") or 0),
        quality=data.get("quality"),
        pages=list(data.get("pages", [])),
    )
