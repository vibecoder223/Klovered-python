"""Page-aware document parsing — Python port of lib/parse.ts.

Returns a sequence of typed blocks with page numbers. PDF uses PyMuPDF
(``fitz``) instead of pdfjs; DOCX uses ``mammoth``; TXT is one page. Scanned
PDFs (no text layer) escalate to Mistral OCR when a key is configured.

The boundary is ``parse_document()`` — a drop-in for the TS ``parseDocument``.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import asdict, dataclass
from statistics import median

import fitz  # PyMuPDF
import httpx
import mammoth

BlockType = str  # "heading" | "paragraph" | "list_item" | "table"


@dataclass
class Block:
    type: BlockType
    text: str
    page: int
    level: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["level"] is None:
            d.pop("level")
        return d


@dataclass
class ParsedDoc:
    blocks: list[Block]
    page_count: int
    raw_text: str

    def to_dict(self) -> dict:
        return {
            "blocks": [b.to_dict() for b in self.blocks],
            "page_count": self.page_count,
            "raw_text": self.raw_text,
        }


# Minimum extracted characters per page below which a PDF is treated as scanned
# (image-only) and escalated to OCR. Mirrors SCANNED_CHARS_PER_PAGE in the TS.
SCANNED_CHARS_PER_PAGE = 80

_LIST_RE = re.compile(r"^\s*(?:[-•●◦*]|\d+[.)]|[a-z][.)])\s+")
_HEADING_MD_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_document(buf: bytes, mime: str | None, filename: str) -> ParsedDoc:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if mime == "application/pdf" or ext == "pdf":
        return _parse_pdf_robust(buf)
    if (
        mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or ext == "docx"
    ):
        return _parse_docx(buf)
    if mime == "text/plain" or ext == "txt":
        return _parse_txt(buf)
    raise ValueError(f"Unsupported file type for parsing: {mime or ext}")


def _has_ocr() -> bool:
    return bool(os.getenv("MISTRAL_API_KEY") or os.getenv("LLM_API_KEY"))


def _parse_pdf_robust(buf: bytes) -> ParsedDoc:
    text_parsed: ParsedDoc | None = None
    text_err: str | None = None
    try:
        text_parsed = _parse_pdf(buf)
    except Exception as e:  # noqa: BLE001 — graceful degradation, never hard-fail
        text_err = str(e)

    chars = len(re.sub(r"\s", "", text_parsed.raw_text)) if text_parsed else 0
    pages = (text_parsed.page_count if text_parsed else 0) or 1
    looks_scanned = (text_parsed is None) or chars < SCANNED_CHARS_PER_PAGE * pages

    if not looks_scanned and text_parsed:
        return text_parsed

    if _has_ocr():
        try:
            ocr = _ocr_pdf(buf)
            if len(re.sub(r"\s", "", ocr.raw_text)) > 0:
                return ocr
        except Exception as e:  # noqa: BLE001
            text_err = f"OCR fallback failed: {e}"

    if text_parsed and chars > 0:
        return text_parsed

    if text_err:
        raise ValueError(
            f"Could not read this PDF ({text_err}). If it is a scanned document, "
            "set MISTRAL_API_KEY to enable OCR."
        )
    raise ValueError(
        "This PDF appears to be scanned (no text layer) and OCR is not configured. "
        "Set MISTRAL_API_KEY to enable OCR."
    )


def _ocr_pdf(buf: bytes) -> ParsedDoc:
    key = os.getenv("MISTRAL_API_KEY") or os.getenv("LLM_API_KEY")
    model = os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-latest")
    data_url = f"data:application/pdf;base64,{base64.b64encode(buf).decode()}"
    with httpx.Client(timeout=120.0) as client:
        res = client.post(
            "https://api.mistral.ai/v1/ocr",
            headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
            json={"model": model, "document": {"type": "document_url", "document_url": data_url}},
        )
    if res.status_code >= 400:
        raise RuntimeError(f"Mistral OCR {res.status_code}: {res.text[:200]}")
    pages = res.json().get("pages", []) or []

    blocks: list[Block] = []
    raw_parts: list[str] = []
    for i, pg in enumerate(pages):
        page_no = (pg.get("index", i) or 0) + 1
        md = pg.get("markdown", "") or ""
        for line in re.split(r"\n+", md):
            text = re.sub(r"\s+", " ", line).strip()
            if not text:
                continue
            raw_parts.append(text)
            h = _HEADING_MD_RE.match(text)
            if h:
                blocks.append(Block("heading", h.group(2), page_no, level=len(h.group(1))))
            elif _LIST_RE.match(text):
                blocks.append(Block("list_item", _LIST_RE.sub("", text), page_no))
            else:
                blocks.append(Block("paragraph", text, page_no))

    return ParsedDoc(blocks=blocks, page_count=len(pages) or 1, raw_text="\n".join(raw_parts))


def _parse_pdf(buf: bytes) -> ParsedDoc:
    blocks: list[Block] = []
    raw_parts: list[str] = []

    with fitz.open(stream=buf, filetype="pdf") as pdf:
        page_count = pdf.page_count
        for p in range(page_count):
            page = pdf[p]
            data = page.get_text("dict")
            # Build visual lines with their max span height (font size proxy).
            lines: list[dict] = []
            for block in data.get("blocks", []):
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    text = " ".join(s.get("text", "") for s in spans)
                    height = max((s.get("size", 10) for s in spans), default=10)
                    if text.strip():
                        lines.append({"height": height, "text": text})

            heights = sorted(ln["height"] for ln in lines)
            median_h = median(heights) if heights else 10.0

            buffer = ""

            def flush() -> None:
                nonlocal buffer
                t = re.sub(r"\s+", " ", buffer).strip()
                if t:
                    blocks.append(Block("paragraph", t, p + 1))
                buffer = ""

            for line in lines:
                text = re.sub(r"\s+", " ", line["text"]).strip()
                if not text:
                    continue
                raw_parts.append(text)

                is_heading = (
                    line["height"] >= median_h * 1.15
                    and len(text) <= 140
                    and not re.search(r"[.;]$", text)
                )
                if is_heading:
                    flush()
                    level = max(1, min(6, 7 - round(line["height"] / median_h)))
                    blocks.append(Block("heading", text, p + 1, level=level))
                    continue

                if _LIST_RE.match(text):
                    flush()
                    blocks.append(Block("list_item", _LIST_RE.sub("", text), p + 1))
                    continue

                buffer = f"{buffer} {text}" if buffer else text
                if re.search(r"[.!?]\s*$", text) and len(buffer) > 60:
                    flush()
            flush()

    return ParsedDoc(blocks=blocks, page_count=page_count, raw_text="\n".join(raw_parts))


_DOCX_RE = re.compile(r"<(h([1-6])|p|li)[^>]*>([\s\S]*?)</\1>", re.IGNORECASE)


def _parse_docx(buf: bytes) -> ParsedDoc:
    import io

    result = mammoth.convert_to_html(io.BytesIO(buf))
    html = result.value
    blocks: list[Block] = []
    raw_parts: list[str] = []

    for m in _DOCX_RE.finditer(html):
        tag = m.group(1).lower()
        inner = re.sub(r"<[^>]+>", "", m.group(3))
        inner = (
            inner.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        inner = re.sub(r"\s+", " ", inner).strip()
        if not inner:
            continue
        raw_parts.append(inner)
        if tag.startswith("h"):
            blocks.append(Block("heading", inner, 1, level=int(m.group(2))))
        elif tag == "li":
            blocks.append(Block("list_item", inner, 1))
        else:
            blocks.append(Block("paragraph", inner, 1))

    return ParsedDoc(blocks=blocks, page_count=1, raw_text="\n".join(raw_parts))


def _parse_txt(buf: bytes) -> ParsedDoc:
    text = buf.decode("utf-8", errors="replace")
    blocks = [
        Block("paragraph", para.strip(), 1)
        for para in re.split(r"\n\s*\n+", text)
        if para.strip()
    ]
    return ParsedDoc(blocks=blocks, page_count=1, raw_text=text)
