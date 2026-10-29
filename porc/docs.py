"""Content checks on the Bill of Lading PDF before it is attached to a Zeus order.

A submitted Zeus order cannot be deleted, so the file is verified three ways before the browser
is even opened: it must be a real PDF, its text must identify it as a Bill of Lading, and the text
must carry this load's TPro load number (the TPro-generated unsigned BOL prints "Trip #: <load id>"
and the Work Order # in the notes). A scanned/signed copy with no text layer fails the check on
purpose: only the system-generated unsigned BOL passes.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MIN_PDF_BYTES = 2_000


def pdf_text(path: Path, max_pages: int = 3) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages[:max_pages]:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def verify_bol_pdf(path: Path, load_id: int, work_order: str = "") -> tuple[bool, str]:
    """Return (ok, detail). ok=False means: do not create a Zeus order for this load."""
    if not path.exists():
        return False, f"{path.name} is missing"
    size = path.stat().st_size
    if size < MIN_PDF_BYTES:
        return False, f"{path.name} is only {size} bytes"
    with open(path, "rb") as fh:
        head = fh.read(5)
    if head != b"%PDF-":
        return False, f"{path.name} is not a PDF (starts with {head!r})"
    try:
        text = pdf_text(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"{path.name} could not be parsed as a PDF: {exc}"
    flat = re.sub(r"\s+", " ", text)
    if not re.search(r"bill\s*of\s*lading", flat, re.I):
        return False, f"{path.name} has no 'Bill Of Lading' text (scanned/signed copy or wrong document?)"
    if str(load_id) not in flat:
        return False, f"{path.name} does not mention load {load_id}"
    detail = f"{path.name}: {size} bytes, text identifies it as the Bill Of Lading for load {load_id}"
    if work_order and work_order not in flat:
        detail += f" (note: WO# {work_order} not found in its text)"
        log.warning("BOL for load %s does not mention WO# %s", load_id, work_order)
    return True, detail
