"""Knowledge-base extract, chunk, and embed helpers (spec 33).

Used by the Configuration API ingest path and the worker retrieval path so
the same chunk sizes and embedding request shape are shared.
"""

from __future__ import annotations

import csv
import io
import re
from html.parser import HTMLParser
from typing import Any

from shared.models import KnowledgeSourceType

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
MAX_FILE_BYTES = 10 * 1024 * 1024
EMBED_BATCH = 64
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def strip_html(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    return collapse_whitespace(" ".join(parser.parts))


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def guess_source_type(filename: str, content_type: str | None = None) -> KnowledgeSourceType:
    name = filename.lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ctype:
        return KnowledgeSourceType.PDF
    if name.endswith(".docx") or "wordprocessingml" in ctype:
        return KnowledgeSourceType.DOCX
    if name.endswith(".csv") or ctype.startswith("text/csv"):
        return KnowledgeSourceType.CSV
    if name.endswith((".html", ".htm")) or "html" in ctype:
        return KnowledgeSourceType.WEB
    return KnowledgeSourceType.TXT


def extract_text(
    data: bytes,
    source_type: KnowledgeSourceType,
    *,
    encoding: str = "utf-8",
) -> str:
    """Turn a stored file into plain text.

    PDF and DOCX need optional libraries; tests cover TXT/CSV without them.
    """
    if source_type is KnowledgeSourceType.PDF:
        return _extract_pdf(data)
    if source_type is KnowledgeSourceType.DOCX:
        return _extract_docx(data)
    text = data.decode(encoding, errors="replace")
    if source_type is KnowledgeSourceType.CSV:
        return _extract_csv(text)
    if source_type is KnowledgeSourceType.WEB:
        return strip_html(text)
    return collapse_whitespace(text)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required to ingest PDF documents") from exc
    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return collapse_whitespace("\n".join(pages))


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("python-docx is required to ingest DOCX documents") from exc
    document = Document(io.BytesIO(data))
    return collapse_whitespace("\n".join(p.text for p in document.paragraphs if p.text))


def _extract_csv(text: str) -> str:
    reader = csv.DictReader(io.StringIO(text))
    rows: list[str] = []
    for row in reader:
        parts = [f"{key}: {value}" for key, value in row.items() if value]
        if parts:
            rows.append("; ".join(parts))
    if rows:
        return "\n".join(rows)
    return collapse_whitespace(text)


def chunk_text(
    text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Split on paragraph / sentence boundaries, then pad to ``size``.

    Overlap keeps a heading with the paragraph that follows it so retrieval
    does not return a fragment that has lost its subject.
    """
    cleaned = collapse_whitespace(text)
    if not cleaned:
        return []
    if len(cleaned) <= size:
        return [cleaned]

    paragraphs = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n{2,}", cleaned) if p.strip()]
    if not paragraphs:
        paragraphs = [cleaned]

    chunks: list[str] = []
    current = ""
    for part in paragraphs:
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            overlap_text = current[-overlap:] if overlap and len(current) > overlap else current
            current = f"{overlap_text} {part}".strip()
            if len(current) > size:
                chunks.extend(_window(current, size, overlap))
                current = ""
        else:
            chunks.extend(_window(part, size, overlap))
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _window(text: str, size: int, overlap: int) -> list[str]:
    step = max(size - overlap, 1)
    return [text[i : i + size] for i in range(0, len(text), step) if text[i : i + size].strip()]


def embedding_url(base_url: str | None) -> str:
    root = (base_url or "https://api.openai.com/v1").rstrip("/")
    if not root.endswith("/v1") and not root.endswith("/embeddings"):
        root = f"{root}/v1"
    if root.endswith("/embeddings"):
        return root
    return f"{root}/embeddings"


async def embed_texts(
    texts: list[str],
    *,
    api_key: str | None,
    base_url: str | None,
    model: str = DEFAULT_EMBEDDING_MODEL,
) -> list[list[float]]:
    """OpenAI-compatible ``/embeddings``. Empty input returns an empty list."""
    if not texts:
        return []
    if not api_key:
        raise RuntimeError("an embedding API key is required to index or retrieve")

    import httpx

    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            response = await client.post(
                embedding_url(base_url),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "input": batch},
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            ordered = sorted(payload.get("data") or [], key=lambda item: item.get("index", 0))
            if len(ordered) != len(batch):
                raise RuntimeError("the embedding provider returned a different number of vectors")
            vectors.extend(item["embedding"] for item in ordered)
    return vectors


def format_vector(values: list[float]) -> str:
    """pgvector text literal, used so retrieval SQL needs no driver adapter."""
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"
