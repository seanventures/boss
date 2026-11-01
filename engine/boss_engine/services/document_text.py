"""The words in a document a person handed BOSS: one PDF reader, shared.

Drive briefs (``drive_media.read_document``) and Knowledge Graph › Documents uploads
(``knowledge/document_upload.py``) read a PDF through ``pdf_pages`` here, so the two can never
disagree about what a page says. A page's text is what pypdf lifts out, with the spacing
artefacts of painted glyphs removed and Tamil-style vowel signs put back in the order a reader
wrote them (``drive_media._tidied`` / ``in_the_order_a_reader_wrote_it``).

Pages are kept apart (an upload cites "p. 12"), and a PDF with no text on any page — a scan, or
pages exported as images — is refused in words, never returned as an empty document.
"""
from __future__ import annotations

import io


class DocumentTextError(ValueError):
    """The document cannot be read, said in words a person can act on."""


def pdf_pages(payload: bytes, name: str, *, advice: str = "") -> list[str]:
    """The text of every page of a PDF, in page order (an empty string for a page with none).

    Raises DocumentTextError when pypdf is missing, the file is not a readable PDF, or no page
    holds any text. ``advice`` is appended to a refusal: what the caller's person can do instead.
    """
    from .drive_media import _tidied, in_the_order_a_reader_wrote_it

    tail = f" {advice.strip()}" if advice.strip() else ""
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:  # pragma: no cover - a deployment fault, not a user one
        raise DocumentTextError(
            f"'{name}' is a PDF and this install cannot read PDFs: pypdf is not present.{tail}"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(payload))
        raw = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # pypdf raises a family of its own errors for a damaged file
        raise DocumentTextError(
            f"Could not read '{name}' as a PDF: {exc}. If it is encrypted or damaged, open it "
            f"and save it again.{tail}") from exc
    pages = [in_the_order_a_reader_wrote_it(_tidied(page)) if page.strip() else "" for page in raw]
    if not any(page.strip() for page in pages):
        raise DocumentTextError(
            f"'{name}' is a PDF with {len(raw)} page(s) and no text in any of them — the pages "
            f"are images, so the words are part of the picture and there is nothing to lift "
            f"out.{tail}")
    return pages


def pdf_text(payload: bytes, name: str, *, advice: str = "") -> str:
    """Every page's text joined in order, blank pages left out."""
    return "\n\n".join(page for page in pdf_pages(payload, name, advice=advice) if page.strip())
