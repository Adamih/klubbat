"""PDF text extraction (pdfplumber) with page-level OCR fallback (OCRmyPDF/Tesseract swe).

Runs entirely outside database transactions. Raw per-page text is preserved in a
sidecar JSON; the OCR'd PDF is stored as a derived artifact when OCR is needed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
from django.conf import settings

log = logging.getLogger(__name__)

EXTRACTOR_VERSION = "pdfplumber+ocrmypdf-v1"
MIN_USABLE_CHARS = 30
OCR_SUBPROCESS_TIMEOUT = 1800  # seconds per document


class ExtractionError(Exception):
    pass


@dataclass
class PageText:
    number: int
    method: str  # "text" | "ocr" | "blank" | "ocr-failed"
    text: str


@dataclass
class ExtractionResult:
    pages: list[PageText] = field(default_factory=list)
    ocr_used: bool = False
    ocr_page_numbers: list[int] = field(default_factory=list)
    ocr_error: str = ""  # non-empty when OCR was attempted but unavailable


def _text_is_usable(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < MIN_USABLE_CHARS:
        return False
    # pdfminer emits "(cid:N)" tokens when fonts cannot be mapped: unusable text.
    if stripped.count("(cid:") > max(3, len(stripped) // 500):
        return False
    return True


def _page_has_content(page) -> bool:
    """A page with drawing/image/text objects but no usable text layer is an OCR candidate."""
    return bool(page.chars or page.images or page.rects or page.lines or page.curves)


def _ocr_command_env() -> dict:
    env = os.environ.copy()
    extra_paths = [p for p in (settings.TESSERACT_DIR, settings.GHOSTSCRIPT_DIR) if p]
    if extra_paths:
        env["PATH"] = os.pathsep.join(extra_paths) + os.pathsep + env.get("PATH", "")
    env["TESSDATA_PREFIX"] = settings.TESSDATA_PREFIX
    return env


def run_ocrmypdf(src: Path, dst: Path, page_numbers: list[int]) -> None:
    pages = ",".join(str(n) for n in sorted(page_numbers))
    cmd = [
        sys.executable, "-m", "ocrmypdf",
        "--language", "swe",
        "--pages", pages,
        # Mixed-document mode: among the selected pages, any page that already
        # has a text layer is kept as-is; only image-only pages are OCR'd.
        # (--force-ocr would rasterize sparse text pages and abort on others.)
        "--skip-text",
        "--output-type", "pdf",
        "--optimize", "0",
        "--quiet",
        str(src), str(dst),
    ]
    log.info("OCR pages %s of %s", pages, src.name)
    proc = subprocess.run(
        cmd, env=_ocr_command_env(), capture_output=True, text=True, timeout=OCR_SUBPROCESS_TIMEOUT
    )
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "")[-500:]
        raise ExtractionError(f"ocrmypdf failed (exit {proc.returncode}): {stderr_tail}")


def _pikepdf_repair(src: Path, dst: Path) -> None:
    """Normalize a PDF's structure through pikepdf (bundled with ocrmypdf)."""
    import pikepdf

    with pikepdf.open(src) as pdf:
        pdf.save(dst)


def extract_pdf(pdf_path: Path, derived_dir: Path, sha256: str) -> ExtractionResult:
    """Extract page by page; OCR only pages that look non-blank but have no usable text."""
    result = ExtractionResult()
    with pdfplumber.open(pdf_path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # a single bad page must not abort the document
                log.warning("Text extraction failed for page %s of %s: %r", number, pdf_path.name, exc)
                text = ""
            if _text_is_usable(text):
                result.pages.append(PageText(number, "text", text))
            elif _page_has_content(page):
                result.pages.append(PageText(number, "ocr", ""))  # placeholder, replaced below
                result.ocr_page_numbers.append(number)
            else:
                result.pages.append(PageText(number, "blank", ""))

    if result.ocr_page_numbers:
        ocr_pdf = derived_dir / f"{sha256}-ocr.pdf"
        derived_dir.mkdir(parents=True, exist_ok=True)
        try:
            try:
                run_ocrmypdf(pdf_path, ocr_pdf, result.ocr_page_numbers)
            except ExtractionError:
                # Some tagged/protected PDFs produce invalid OCR output; a
                # structural normalization pass fixes most of them.
                log.warning("OCR failed for %s; repairing input and retrying once", pdf_path.name)
                repaired = derived_dir / f"{sha256}-repaired.pdf"
                try:
                    _pikepdf_repair(pdf_path, repaired)
                    run_ocrmypdf(repaired, ocr_pdf, result.ocr_page_numbers)
                finally:
                    repaired.unlink(missing_ok=True)
            with pdfplumber.open(ocr_pdf) as pdf:
                wanted = {p.number: p for p in result.pages}
                for number, page in enumerate(pdf.pages, start=1):
                    if number not in wanted:
                        continue
                    entry = wanted[number]
                    try:
                        text = page.extract_text() or ""
                    except Exception as exc:
                        log.warning("OCR text extraction failed for page %s of %s: %r", number, pdf_path.name, exc)
                        text = ""
                    if _text_is_usable(text):
                        entry.text = text
                        entry.method = "ocr"
                    else:
                        entry.method = "ocr-failed"
            result.ocr_used = True
            _prune_failed_ocr_pages(result)
        except ExtractionError as exc:
            # Graceful degradation: keep extracted text pages, flag OCR pages.
            log.error("OCR unavailable for %s: %s", pdf_path.name, exc)
            for page in result.pages:
                if page.method == "ocr":
                    page.method = "ocr-failed"
            result.ocr_error = str(exc)[:300]

    _write_sidecar(derived_dir, sha256, result)
    return result


def _prune_failed_ocr_pages(result: ExtractionResult) -> None:
    failed = [p for p in result.pages if p.method == "ocr-failed"]
    if failed:
        log.warning(
            "OCR could not recover text on %d page(s): %s",
            len(failed), ", ".join(str(p.number) for p in failed),
        )


def _write_sidecar(derived_dir: Path, sha256: str, result: ExtractionResult) -> None:
    derived_dir.mkdir(parents=True, exist_ok=True)
    sidecar = {
        "sha256": sha256,
        "extractor_version": EXTRACTOR_VERSION,
        "ocr_used": result.ocr_used,
        "ocr_page_numbers": result.ocr_page_numbers,
        "ocr_error": result.ocr_error,
        "pages": [
            {"number": p.number, "method": p.method, "text": p.text} for p in result.pages
        ],
    }
    path = derived_dir / f"{sha256}.extraction.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)