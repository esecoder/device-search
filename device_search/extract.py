"""
extract.py — get TEXT out of a file, whatever kind of file it is.

===============================================================================
⚠️ WHY THIS IS THE RIGHT FIRST MOVE INTO MULTIMODAL, AND WHY IT IS NOT "MULTIMODAL RAG"
===============================================================================
There are three ways to make media searchable, and they are not interchangeable:

    1. EXTRACT TEXT FROM IT  (this file)      image -> OCR, PDF -> text, audio -> ASR
                                              ⚠️ Then it is just NORMAL RAG. One index,
                                              BM25 works, embeddings work, nothing new.
    2. JOINT EMBEDDING       (CLIP, later)    image -> vector, text -> same vector space
                                              ⚠️ Finds images by VISUAL CONTENT with no text
                                              at all — but coarsely, and it cannot answer
                                              questions about what is in the image.
    3. CAPTION THEN EMBED    (a VLM)          image -> description -> index the text
                                              ⚠️ Richest, and the most expensive: one model
                                              call per file. 66k images is hours to days.

⚠️ **THIS IS APPROACH 1, AND IT IS THE HIGHEST VALUE PER UNIT OF EFFORT**, because it reuses
the entire existing pipeline. Once a PDF or a screenshot becomes text, everything already
built — exact match, BM25, embeddings, the query router, the two-tier output — works on it
with no changes.

⚠️ ITS HONEST LIMIT: it only finds media that CONTAINS TEXT. A photograph of a sunset has
nothing to extract, and this file will correctly report that it found nothing. Approach 2 is
what finds that photo, and it is not built here.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# ⚠️ LAZY GLOBALS. Loading the OCR model takes seconds and ~100 MB of RAM; a user who only
# searches text should never pay for it. Both are loaded on first use and cached.
_ocr_engine = None
_ocr_failed = False


def _ocr():
    """Return a RapidOCR engine, or None if it cannot be built.

    ⚠️ RETURNS None RATHER THAN RAISING. OCR is an ENHANCEMENT — if it is unavailable, text
    search must still work. A search tool that refuses to run because an optional model is
    missing is worse than one that searches text only, and reports that it did.
    """
    global _ocr_engine, _ocr_failed
    if _ocr_engine is None and not _ocr_failed:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        except Exception:
            _ocr_failed = True
    return _ocr_engine


def ocr_available() -> tuple[bool, str]:
    """⚠️ REPORTED, NOT ASSUMED. If this is False the CLI must say so, or a user will conclude
    their screenshot does not exist rather than that OCR was never installed."""
    if _ocr() is None:
        return False, "rapidocr-onnxruntime unavailable (pip install rapidocr-onnxruntime)"
    return True, "rapidocr-onnxruntime"


def pdf_available() -> tuple[bool, str]:
    try:
        import pdftext  # noqa: F401
        return True, "pdftext"
    except Exception as e:
        return False, f"pdftext unavailable ({type(e).__name__})"


# =============================================================================
# EXTRACTORS
# =============================================================================
def extract_pdf(path: Path, max_pages: int | None = None) -> str:
    """⚠️ pdftext RETURNS TEXT WITH POSITIONS, which we join page by page.

    ⚠️ PAGE MARKERS ARE INSERTED DELIBERATELY. Without them a hit inside a 400-page PDF reports
    "line 8,231", which is true and useless — the user thinks in pages. The marker lets the
    snippet say which page it came from.
    """
    from pdftext.extraction import plain_text_output
    text = plain_text_output(str(path), sort=False, hyphens=False)
    if max_pages:
        # ⚠️ The page marker is the only place a page boundary is recorded, so a cap is
        # applied on the markers rather than on characters.
        parts = text.split("\f")
        text = "\f".join(parts[:max_pages])
    out = []
    for i, page in enumerate(text.split("\f"), 1):
        if page.strip():
            out.append(f"[page {i}]\n{page}")
    return "\n".join(out)


def extract_image(path: Path, min_conf: float = 0.5) -> str:
    """OCR an image. ⚠️ `min_conf` IS NOT A TUNABLE — IT IS THE DIFFERENCE BETWEEN TEXT AND NOISE.

    ⚠️ OCR ALWAYS RETURNS SOMETHING. On a photograph with no text, a detector with no
    confidence floor invents words out of texture — and those invented words go into the index
    and become searchable. **A hallucinated index entry is worse than a missing one**, because
    a user who searches and gets a match believes the text is really there.

    ⚠️ And the confidence is REPORTED per detection, so the caller can see how much of the text
    was marginal rather than being handed one blended string.
    """
    engine = _ocr()
    if engine is None:
        return ""
    try:
        result, _elapse = engine(str(path))
    except Exception:
        return ""
    if not result:
        return ""
    lines = []
    for box, txt, conf in result:
        # ⚠️ Raise the floor rather than lower it if text is being invented. The default 0.5 is
        # already permissive; real screenshots score 0.85-0.99.
        if conf is not None and conf < min_conf:
            continue
        if txt and txt.strip():
            lines.append(txt.strip())
    return "\n".join(lines)


def extract_text_file(path: Path) -> str:
    """⚠️ READ AS utf-8 WITH REPLACEMENT, NOT STRICTLY. Plenty of real source files are latin-1
    or contain one invalid byte, and refusing the whole file over one byte loses everything."""
    return path.read_text(encoding="utf-8", errors="replace")


# =============================================================================
# THE DISPATCHER
# =============================================================================
# ⚠️ ORDER MATTERS AND IS BY COST: utf-8 read (microseconds) -> PDF (ms) -> OCR (SECONDS).
# The cheap paths must be tried first so a corpus of text files never pays for OCR.
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def extract(path: Path) -> tuple[str, str]:
    """Return (text, method). `method` is empty-ish when nothing was found.

    ⚠️ `method` IS RETURNED, NOT DISCARDED. "found in scan.pdf" and "found in scan.pdf via OCR"
    are different claims about reliability, and a user checking an OCR hit needs to know which
    one they are looking at. It travels into the index and is shown in results.
    """
    ext = path.suffix.lower()
    if ext in PDF_EXTS:
        if not pdf_available()[0]:
            return "", "pdf_unavailable"
        try:
            return extract_pdf(path), "pdftext"
        except Exception as e:
            return "", f"pdf_error:{type(e).__name__}"
    if ext in IMAGE_EXTS:
        if not ocr_available()[0]:
            return "", "ocr_unavailable"
        try:
            return extract_image(path), "ocr"
        except Exception as e:
            return "", f"ocr_error:{type(e).__name__}"
    try:
        return extract_text_file(path), "utf8"
    except (OSError, PermissionError):
        return "", "unreadable"


def supports_media() -> dict:
    """⚠️ A SINGLE PLACE THE CLI CAN ASK 'what can this install actually do?'

    Without this, the answer is discovered one missing file at a time — and the user concludes
    their scan is not on disk rather than that OCR was never installed.
    """
    ok_pdf, why_pdf = pdf_available()
    ok_ocr, why_ocr = ocr_available()
    return {"pdf": (ok_pdf, why_pdf), "ocr": (ok_ocr, why_ocr)}
