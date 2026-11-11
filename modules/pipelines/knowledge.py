"""Programmatic document ingestion, independent of model/tool invocation."""

from __future__ import annotations
import json
import math
import uuid
import re
from pathlib import Path
from datetime import datetime, timezone
from core.sqlite_storage import SQLiteStore

MAX_SOURCE_BYTES = 32 * 1024 * 1024


def temporary_sources(storage):
    """List source metadata without loading document bodies or embeddings."""
    if not storage.exists():
        return []
    with storage.connection() as db:
        return [
            dict(id=identity, source=json.loads(payload).get("source", identity))
            for identity, payload in db.execute(
                "SELECT id,payload FROM documents ORDER BY position"
            )
        ]


def temporary_pages(storage, doc_id):
    if not doc_id or not storage.exists():
        raise ValueError("Выберите документ временной памяти")
    with storage.connection() as db:
        row = db.execute(
            "SELECT payload FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Документ временной памяти не найден в выбранной сессии")
        header = json.loads(row[0])
        pages, total = [], 0
        # A dual-format document is one source, not two copies of the same text.
        for table in ("pages", "chunks"):
            for text, title, url in db.execute(
                f"SELECT json_extract(payload,'$.text'),json_extract(payload,'$.title'),"
                f"COALESCE(json_extract(payload,'$.url'),json_extract(payload,'$.source_url')) "
                f"FROM {table} WHERE document_id=? ORDER BY position",
                (doc_id,),
            ):
                text = str(text or "")
                total += len(text.encode("utf-8"))
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("Документ превышает 32 MiB")
                if text.strip():
                    pages.append(
                        dict(text=text, title=str(title or ""), url=str(url or ""))
                    )
            if pages:
                break
    if not pages:
        raise ValueError("Документ временной памяти пуст")
    return pages, str(header.get("source") or doc_id)


def parse_document(data, suffix, source):
    import io

    if suffix == ".pdf":
        from pypdf import PdfReader

        return [
            {"title": f"Страница {i}", "text": p.extract_text() or "", "url": source}
            for i, p in enumerate(PdfReader(io.BytesIO(data)).pages, 1)
        ]
    if suffix == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs]
        parts.extend(
            " | ".join(c.text for c in row.cells)
            for table in doc.tables
            for row in table.rows
        )
        return [{"title": Path(source).name, "text": "\n".join(parts), "url": source}]
    if b"\x00" in data[:8192] and suffix not in {".txt", ".md", ".csv"}:
        raise ValueError("Формат документа не поддерживается")
    text = data.decode("utf-8-sig", errors="replace")
    if suffix in {".html", ".htm"}:
        from bs4 import BeautifulSoup

        doc = BeautifulSoup(text, "html.parser")
        for tag in doc(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()
        title = doc.title.get_text(" ", strip=True) if doc.title else source
        text = (doc.find("main") or doc.find("article") or doc).get_text(
            "\n", strip=True
        )
    else:
        title = Path(source).name
    if suffix not in {".txt", ".md", ".csv", ".json", ".html", ".htm", ""}:
        raise ValueError("Поддерживаются TXT, Markdown, CSV, JSON, HTML, PDF, DOCX")
    return [{"title": title, "text": text, "url": source}]


def chunks(text, size, overlap):
    if not 100 <= size <= 50000 or not 0 <= overlap < size:
        raise ValueError(
            "Размер чанка: 100–50000, перекрытие должно быть меньше размера"
        )
    start = 0
    while start < len(text):
        piece = text[start : start + size].strip()
        if piece:
            yield piece
        if start + size >= len(text):
            break
        start += size - overlap


async def create_database(
    store,
    module,
    client,
    *,
    name,
    source="",
    source_kind="file",
    raw_text="",
    doc_id="",
    temporary_store=None,
    mode="text",
    manifest="",
    chunk_size=1500,
    overlap=150,
    page_size=1000,
    page_extra_limit=1500,
):
    import asyncio
    from urllib.parse import urlparse

    if not name.strip():
        raise ValueError("Укажите название базы")
    store.read(module)
    if source_kind not in {"file", "text", "temporary"}:
        raise ValueError("Неизвестный тип источника")
    if mode not in {"text", "vector", "both"}:
        raise ValueError("Неизвестный режим базы")
    from core.text_pagination import paginate_text

    chunk_size, overlap = int(chunk_size), int(overlap)
    page_size, page_extra_limit = int(page_size), int(page_extra_limit)
    if mode in {"vector", "both"}:
        list(chunks("", chunk_size, overlap))
    if mode in {"text", "both"} and not (
        100 <= page_size <= 50000 and 0 <= page_extra_limit <= 50000
    ):
        raise ValueError(
            "Размер страницы: 100–50000; запас до переноса: 0–50000 символов"
        )
    if source_kind == "text":
        if not raw_text.strip():
            raise ValueError("Вставьте текст для создания базы")
        if len(raw_text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("Текст превышает 32 MiB")
        source = "Вставленный текст"
        pages = [dict(title=name, text=raw_text, url="")]
    elif source_kind == "temporary":
        if temporary_store is None:
            raise ValueError("Выберите сессию временной памяти")
        pages, original_source = await asyncio.to_thread(
            temporary_pages, temporary_store, doc_id
        )
        source = (
            f"temporary:{temporary_store.path.parent.name}/{doc_id} ({original_source})"
        )
    elif not source.strip():
        raise ValueError("Укажите файл или URL")
    elif source.startswith(("http://", "https://")):
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as http:
            async with http.stream("GET", source) as response:
                response.raise_for_status()
                data = bytearray()
                async for block in response.aiter_bytes():
                    data.extend(block)
                    if len(data) > MAX_SOURCE_BYTES:
                        raise ValueError("Документ превышает 32 MiB")
                mime = response.headers.get("content-type", "").split(";")[0]
                suffix = Path(urlparse(str(response.url)).path).suffix.lower()
                suffix = {
                    "text/html": ".html",
                    "application/pdf": ".pdf",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                    "text/plain": ".txt",
                }.get(mime, suffix)
    else:
        path = Path(source).expanduser().resolve()
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("Документ превышает 32 MiB")
        data = await asyncio.to_thread(path.read_bytes)
        suffix = path.suffix.lower()
    if source_kind == "file":
        pages = await asyncio.to_thread(parse_document, bytes(data), suffix, source)
    pages = [p for p in pages if p["text"].strip()]
    if not pages:
        raise ValueError("В документе нет извлекаемого текста")
    identity = f"custom.{module}::kb_{uuid.uuid4().hex[:12]}"
    session = client.embedding_snapshot() if mode in {"vector", "both"} else None
    doc = {
        "mode": "text" if mode == "text" else "vector",
        "formats": ["text", "vector"] if mode == "both" else [mode],
        "source": source,
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "chunk_size": chunk_size,
            "overlap": overlap,
            "page_size": page_size,
            "page_extra_limit": page_extra_limit,
            "page_hard_limit": page_size + page_extra_limit,
        },
    }
    if mode in {"text", "both"}:
        doc["pages"] = [
            {"page_num": i, "text": piece, "title": page["title"], "url": page["url"]}
            for i, (page, piece) in enumerate(
                (
                    (page, piece)
                    for page in pages
                    for piece in paginate_text(
                        page["text"], page_size, page_size + page_extra_limit
                    )
                ),
                1,
            )
        ]
    if session:
        vectors = []
        for page in pages:
            for piece in chunks(page["text"], chunk_size, overlap):
                vector = await session.embed(piece, purpose="document")
                vectors.append(
                    {
                        "chunk_id": len(vectors) + 1,
                        "text": piece,
                        "vector": vector,
                        "title": page["title"],
                        "source_url": page["url"],
                    }
                )
        doc.update(
            chunks=vectors,
            embedding_model=session.identity,
            embedding_dimension=session.dimension,
        )
        doc["metadata"].update(
            embedding_model=session.identity, dimension=session.dimension
        )
    payload = {
        "__toshy_kb__": {
            "schema_version": 1,
            "id": identity,
            "name": name,
            "manifest": manifest,
            "owner": f"custom.{module}",
        },
        "document": doc,
    }
    filename = "kb_" + uuid.uuid4().hex[:12] + ".sqlite"
    target = store.path(module) / "knowledge" / filename
    await asyncio.to_thread(SQLiteStore(target, "knowledge").write_knowledge, payload)
    return {
        "message": f'База «{name}» создана: страниц {len(doc.get("pages", []))}, чанков {len(doc.get("chunks", []))}.',
        "path": str(target),
        "id": identity,
    }


def import_database(store, module, path):
    path = Path(path)
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("База превышает 32 MiB")
    if path.suffix.lower() == ".sqlite":
        payload = SQLiteStore(path, "knowledge").read_knowledge()
    else:
        # Explicit import remains compatible with previously exported JSON KBs.
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("База должна содержать JSON-объект Toshy")
    docs = {
        k: v
        for k, v in payload.items()
        if k != "__toshy_kb__"
        and isinstance(v, dict)
        and ("pages" in v or "chunks" in v)
    }
    if not docs:
        raise ValueError("В базе нет страниц или векторных чанков")
    for doc in docs.values():
        for page in doc.get("pages", []):
            if not isinstance(page, dict) or not isinstance(page.get("text"), str):
                raise ValueError("Некорректная страница базы")
        lengths = set()
        for chunk in doc.get("chunks", []):
            vector = chunk.get("vector") if isinstance(chunk, dict) else None
            if (
                not isinstance(vector, list)
                or not vector
                or not all(
                    isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and math.isfinite(v)
                    for v in vector
                )
            ):
                raise ValueError("Некорректный embedding")
            lengths.add(len(vector))
        if len(lengths) > 1:
            raise ValueError("Несовместимые размерности embeddings")
    meta = dict(payload.get("__toshy_kb__") or {})
    meta.update(
        id=f"custom.{module}::kb_{uuid.uuid4().hex[:12]}",
        owner=f"custom.{module}",
        name=meta.get("name") or path.stem,
        schema_version=1,
    )
    target = (
        store.path(module) / "knowledge" / ("kb_" + uuid.uuid4().hex[:12] + ".sqlite")
    )
    SQLiteStore(target, "knowledge").write_knowledge({"__toshy_kb__": meta, **docs})
    return {"path": str(target), "id": meta["id"]}
