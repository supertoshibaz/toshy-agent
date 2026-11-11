from core.logger import core_logger, log_operation
from core.background import run_data_task
import os
import yaml
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Dict, Any, Optional, Tuple

from core.context import get_execution_context
from core.paths import APP_PATHS
from core.sqlite_storage import SQLiteStore
from core.embeddings import EmbeddingError, embedding_identity_from_config


class MemoryManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        permanent_path: Optional[str] = None,
        client: Optional[Any] = None
    ):
        if self._initialized:
            if client and not self.client:
                self.client = client
            return

        self.permanent_path = str(Path(permanent_path or APP_PATHS.root / "core" / "permanent_memory.sqlite").resolve())
        if Path(self.permanent_path).suffix.lower() == ".json":
            self.permanent_path = str(Path(self.permanent_path).with_suffix(".sqlite"))
        self.client = client

        # Knowledge bases stay separate from temporary memory. Each SQLite file is
        # mounted as an independent database with its own globally unique ID.
        # { database_id: {id, name, manifest, owner, module_name, path, documents} }
        self.knowledge_bases: Dict[str, Dict[str, Any]] = {}
        self.module_knowledge_base_ids: Dict[str, List[str]] = {}

        self._create_parent_dirs(self.permanent_path)
        self._create_parent_dirs(self.temp_path)
        self._init_permanent_memory()
        self._init_temp_memory_if_not_exists()
        self._initialized = True

    @property
    def temp_path(self) -> str:
        return str(get_execution_context().storage_dir / "session.sqlite")

    @property
    def temp_store(self) -> SQLiteStore:
        return SQLiteStore(self.temp_path)

    @property
    def active_mode(self) -> str:
        return get_execution_context().temp_memory_mode

    @property
    def store_both_formats(self) -> bool:
        return bool(get_execution_context().store_temp_memory_both_formats)

    @property
    def limit_ranking(self) -> bool:
        return get_execution_context().limit_ranking

    def _get_active_embedding_model(self) -> str:
        """Считывает имя активной модели эмбеддингов из config.yaml."""
        try:
            with open(APP_PATHS.config, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                return embedding_identity_from_config(cfg.get("embedding", {}) or {})
        except Exception:
            return "unknown"

    def _create_parent_dirs(self, path: str):
        parent_dir = os.path.dirname(path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)


    # =========================================================================
    # ЧТЕНИЕ / ЗАПИСЬ ПОСТОЯННОЙ ПАМЯТИ (SQLite)
    # =========================================================================

    def _read_permanent_db(self) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        return SQLiteStore(self.permanent_path, 'permanent').read_permanent()


    def _init_permanent_memory(self):
        SQLiteStore(self.permanent_path, 'permanent').initialize()

    def _init_temp_memory_if_not_exists(self):
        self.temp_store.initialize()

    # =========================================================================
    # ОТДЕЛЬНЫЕ БАЗЫ ЗНАНИЙ МОДУЛЕЙ
    # =========================================================================

    @staticmethod
    def _normalize_kb_owner(module_name: str, owner: Optional[str] = None) -> str:
        clean_owner = str(owner or "").strip().strip(".")
        if clean_owner:
            return clean_owner
        return f"module.{str(module_name or 'unknown').strip() or 'unknown'}"

    @staticmethod
    def _normalize_kb_descriptor(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, (str, os.PathLike)):
            return {"path": str(raw)}
        if isinstance(raw, dict):
            return dict(raw)
        return {}

    @log_operation('memory', 'MEMORY')
    def mount_external_kb(
        self,
        module_name: str,
        file_paths: Iterable[Any],
        *,
        owner: Optional[str] = None,
    ) -> None:
        """Mount every SQLite file as its own named knowledge base.

        Modules may return a list of paths or descriptor dictionaries with optional ``name`` / ``manifest`` overrides.
        The database itself may contain a reserved ``__toshy_kb__`` metadata
        object. All other top-level mapping entries are treated as documents.
        """
        self.unmount_external_kb(module_name)
        owner_id = self._normalize_kb_owner(module_name, owner)
        mounted_ids: List[str] = []

        for raw in list(file_paths or []):
            descriptor = self._normalize_kb_descriptor(raw)
            raw_path = str(descriptor.get("path") or "").strip()
            if not raw_path:
                continue
            p = Path(raw_path).expanduser().resolve()
            if not p.exists() or not p.is_file():
                core_logger.event('memory', 'MEMORY_EVENT', f"[Knowledge Base Warning] Knowledge base file not found: {raw_path}", level='WARN')
                continue

            try:
                payload = SQLiteStore(p, 'knowledge').read_knowledge()
                if not isinstance(payload, dict):
                    raise ValueError("knowledge-base database root must be an object")

                metadata = payload.get("__toshy_kb__")
                metadata = dict(metadata) if isinstance(metadata, dict) else {}

                # Forward-compatible wrapped form is accepted too, while the
                # current compiler writes the legacy-friendly flat form.
                wrapped_docs = payload.get("documents")
                if isinstance(wrapped_docs, dict) and set(payload).issubset({"__toshy_kb__", "documents"}):
                    documents = dict(wrapped_docs)
                else:
                    documents = {
                        str(doc_id): doc
                        for doc_id, doc in payload.items()
                        if doc_id != "__toshy_kb__" and isinstance(doc, dict)
                    }

                local_id = str(
                    descriptor.get("id")
                    or metadata.get("id")
                    or p.stem
                ).strip()
                if "::" in local_id:
                    database_id = local_id
                else:
                    database_id = f"{owner_id}::{local_id or p.stem}"

                # A copied database must not silently replace another database
                # from the same active module. Keep the first and make the
                # collision explicit instead.
                if database_id in self.knowledge_bases:
                    core_logger.event('memory', 'MEMORY_EVENT', f"[Knowledge Base Warning] Duplicate ID '{database_id}' in '{p}'. "
                        "Database skipped.", level='WARN')
                    continue

                name = str(
                    descriptor.get("name")
                    or metadata.get("name")
                    or p.stem
                ).strip() or p.stem
                manifest = str(
                    descriptor.get("manifest")
                    if descriptor.get("manifest") is not None
                    else metadata.get("manifest", "")
                ).strip()

                entry = {
                    "id": database_id,
                    "name": name,
                    "manifest": manifest,
                    "owner": owner_id,
                    "module_name": str(module_name),
                    "path": str(p),
                    "documents": documents,
                }
                self.knowledge_bases[database_id] = entry
                mounted_ids.append(database_id)
                core_logger.event('memory', 'MEMORY_EVENT', f"[Knowledge Base] '{name}' mounted as '{database_id}' "
                    f"for module '{module_name}' ({len(documents)} documents).", level='INFO')
            except Exception as error:
                core_logger.event('memory', 'MEMORY_EVENT', f"[Knowledge Base Error] Failed to mount knowledge base '{raw_path}': {error}", level='ERROR')

        self.module_knowledge_base_ids[str(module_name)] = mounted_ids

    @log_operation('memory', 'MEMORY')
    def unmount_external_kb(self, module_name: str) -> None:
        module_key = str(module_name)
        removed = False
        for database_id in self.module_knowledge_base_ids.pop(module_key, []):
            removed = self.knowledge_bases.pop(database_id, None) is not None or removed
        # Defensive cleanup for stores mounted by older in-process code.
        stale = [
            database_id
            for database_id, entry in self.knowledge_bases.items()
            if str(entry.get("module_name") or "") == module_key
        ]
        for database_id in stale:
            removed = self.knowledge_bases.pop(database_id, None) is not None or removed
        if removed:
            core_logger.event('memory', 'MEMORY_EVENT', f"[Knowledge Base] Knowledge bases for module '{module_name}' unmounted.", level='INFO')

    def _resolve_allowed_modules(
        self,
        allowed_modules: Optional[Iterable[str]] = None,
        *,
        use_execution_context: bool = True,
    ) -> Optional[set[str]]:
        if allowed_modules is not None:
            return {str(name) for name in allowed_modules}
        if not use_execution_context:
            return None
        try:
            value = get_execution_context().allowed_modules
        except Exception:
            value = None
        return None if value is None else {str(name) for name in value}

    def list_knowledge_bases(
        self,
        allowed_modules: Optional[Iterable[str]] = None,
        *,
        use_execution_context: bool = True,
    ) -> List[Dict[str, Any]]:
        allowed = self._resolve_allowed_modules(
            allowed_modules, use_execution_context=use_execution_context
        )
        result: List[Dict[str, Any]] = []
        for database_id in sorted(self.knowledge_bases, key=str.casefold):
            entry = self.knowledge_bases[database_id]
            if allowed is not None and str(entry.get("module_name")) not in allowed:
                continue
            formats: set[str] = set()
            for doc in (entry.get("documents") or {}).values():
                formats.update(self._document_formats(doc))
            result.append({
                "id": str(entry.get("id") or database_id),
                "name": str(entry.get("name") or database_id),
                "manifest": str(entry.get("manifest") or ""),
                "owner": str(entry.get("owner") or ""),
                "module": str(entry.get("module_name") or ""),
                "formats": [name for name in ("text", "vector") if name in formats],
            })
        return result

    @staticmethod
    def format_knowledge_base_catalog(entries: Iterable[Dict[str, Any]]) -> str:
        items = list(entries or [])
        if not items:
            return "- No knowledge bases were available at session start."
        lines: List[str] = []
        for entry in items:
            database_id = str(entry.get("id") or "").strip()
            name = str(entry.get("name") or database_id).strip()
            manifest = str(entry.get("manifest") or "").strip()
            lines.append(
                f"- ID: {database_id} | Name: {name} | Manifest: {manifest}"
            )
        return "\n".join(lines)

    def _selected_knowledge_bases(
        self,
        database_ids: Iterable[str],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        allowed = self._resolve_allowed_modules()
        selected: List[Dict[str, Any]] = []
        missing: List[str] = []
        seen = set()
        for raw_id in database_ids or []:
            database_id = str(raw_id or "").strip()
            if not database_id or database_id in seen:
                continue
            seen.add(database_id)
            entry = self.knowledge_bases.get(database_id)
            if entry is None or (
                allowed is not None
                and str(entry.get("module_name") or "") not in allowed
            ):
                missing.append(database_id)
                continue
            selected.append(entry)
        return selected, missing

    def knowledge_search_plan(
        self, database_ids: Iterable[str]
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
        selected, missing = self._selected_knowledge_bases(database_ids)
        plan = {"text": [], "vector": []}
        for entry in selected:
            available: set[str] = set()
            for doc in (entry.get("documents") or {}).values():
                available.update(self._document_formats(doc))
            # Knowledge bases are independent from the session temp-memory mode.
            # A dual-format database prefers semantic/vector search; text-only
            # databases use keyword pagination.
            if "vector" in available:
                plan["vector"].append(entry)
            elif "text" in available:
                plan["text"].append(entry)
        return plan, missing

    def validate_knowledge_embedding(
        self, database_ids: Iterable[str], embedding_model: str
    ) -> None:
        selected, missing = self._selected_knowledge_bases(database_ids)
        if missing:
            raise KeyError(", ".join(missing))
        for entry in selected:
            for doc_id, doc in (entry.get("documents") or {}).items():
                if not self._document_supports_mode(doc, "vector"):
                    continue
                doc_model = doc.get("embedding_model") or doc.get("metadata", {}).get("embedding_model")
                if doc_model and embedding_model != "unknown" and doc_model != embedding_model:
                    raise EmbeddingError(
                        self._embedding_mismatch_message(
                            f"База '{entry['id']}', документ '{doc_id}' создан моделью",
                            embedding_model,
                            doc_model,
                        )
                    )

    @log_operation('memory', 'MEMORY')
    def search_knowledge_text(
        self,
        database_ids: Iterable[str],
        keywords: List[str],
        max_pages: int = 4,
    ) -> List[Dict[str, Any]]:
        clean_keywords = [str(k).lower().strip() for k in keywords if str(k).strip()]
        if not clean_keywords:
            return []
        selected, _ = self._selected_knowledge_bases(database_ids)
        results: List[Dict[str, Any]] = []
        for entry in selected:
            for doc_id, doc in (entry.get("documents") or {}).items():
                if not self._document_supports_mode(doc, "text"):
                    continue
                for page in doc.get("pages") or []:
                    text = str(page.get("text") or "")
                    text_lower = text.lower()
                    hits = sum(text_lower.count(keyword) for keyword in clean_keywords)
                    if all(keyword in text_lower for keyword in clean_keywords):
                        hits += 5
                    if hits <= 0:
                        continue
                    results.append({
                        "database_id": entry["id"],
                        "database_name": entry["name"],
                        "doc_id": doc_id,
                        "page_num": page.get("page_num", 1),
                        "source": doc.get("source", "unknown"),
                        "score": hits,
                        "text": text,
                    })
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:max_pages] if self.limit_ranking else results

    @log_operation('memory', 'MEMORY')
    def search_knowledge_vector(
        self,
        database_ids: Iterable[str],
        query_vector: List[float],
        limit: int = 5,
        *,
        embedding_model: Optional[str] = None,
        embedding_dimension: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        selected, _ = self._selected_knowledge_bases(database_ids)
        query_model = embedding_model or self._get_active_embedding_model()
        if embedding_dimension is not None and len(query_vector) != int(embedding_dimension):
            raise EmbeddingError(
                f"Query embedding dimension is {len(query_vector)}, expected {embedding_dimension} "
                f"for '{query_model}'."
            )

        results: List[Dict[str, Any]] = []
        for entry in selected:
            for doc_id, doc in (entry.get("documents") or {}).items():
                if not self._document_supports_mode(doc, "vector"):
                    continue
                doc_model = doc.get("embedding_model") or doc.get("metadata", {}).get("embedding_model")
                if doc_model and query_model != "unknown" and doc_model != query_model:
                    raise EmbeddingError(
                        self._embedding_mismatch_message(
                            f"База '{entry['id']}', документ '{doc_id}' создан моделью",
                            query_model,
                            doc_model,
                        )
                    )
                for chunk in doc.get("chunks") or []:
                    stored_vector = chunk.get("vector")
                    if not isinstance(stored_vector, list):
                        continue
                    if len(stored_vector) != len(query_vector):
                        raise EmbeddingError(
                            f"Несовпадение размерности embedding-векторов в '{entry['id']}:{doc_id}': "
                            f"query={len(query_vector)}, stored={len(stored_vector)}."
                        )
                    results.append({
                        "database_id": entry["id"],
                        "database_name": entry["name"],
                        "doc_id": doc_id,
                        "chunk_id": chunk.get("chunk_id", 1),
                        "source": doc.get("source", "unknown"),
                        "_vector": stored_vector,
                        "text": str(chunk.get("text") or ""),
                    })
        self._score_vectors(query_vector, results)
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:limit]

    @staticmethod
    def _document_formats(doc: Any) -> set[str]:
        """Return the storage representations physically available in a temp-memory document."""
        if not isinstance(doc, dict):
            return set()

        raw_formats = doc.get("formats")
        if isinstance(raw_formats, list):
            formats = {str(item).strip().lower() for item in raw_formats}
            formats &= {"text", "vector"}
            if formats:
                return formats

        mode = str(doc.get("mode") or "").strip().lower()
        if mode == "both":
            return {"text", "vector"}
        if mode in {"text", "vector"}:
            return {mode}

        # Representation flags may be omitted by manually imported knowledge bases.
        formats = set()
        if isinstance(doc.get("pages"), list):
            formats.add("text")
        if isinstance(doc.get("chunks"), list):
            formats.add("vector")
        return formats

    @classmethod
    def _document_supports_mode(cls, doc: Any, mode: str) -> bool:
        return str(mode or "").strip().lower() in cls._document_formats(doc)

    @staticmethod
    def _set_document_formats(doc: Dict[str, Any], formats: set[str]) -> None:
        clean = [name for name in ("text", "vector") if name in formats]
        doc["formats"] = clean
        if len(clean) == 2:
            doc["mode"] = "both"
        elif clean:
            doc["mode"] = clean[0]
        else:
            doc["mode"] = "text"

    def _get_all_temp_docs(self, target_doc_id: Optional[str] = None) -> Dict[str, Any]:
        return {identity: doc for identity, doc in self.temp_store.read_documents(target_doc_id).items()
                if self._document_supports_mode(doc, self.active_mode)}

    # =========================================================================
    # ПРОВЕРКА СОВМЕСТИМОСТИ EMBEDDING-ПРОСТРАНСТВ
    # =========================================================================

    @staticmethod
    def _embedding_mismatch_message(scope: str, active_model: str, stored_model: str) -> str:
        return (
            "Несовпадение embedding-моделей.\n"
            f"Активная модель: {active_model}\n"
            f"{scope}: {stored_model}\n\n"
            "Векторные пространства несовместимы. Выбери модель, которой была создана база, "
            "либо переиндексируй базу текущей моделью."
        )

    def validate_permanent_embedding(self, embedding_model: str) -> None:
        """Fail before inference if permanent memory belongs to another vector space."""
        stored_model, facts = self._read_permanent_db()
        if not facts:
            return
        if stored_model and embedding_model != "unknown" and stored_model != embedding_model:
            raise EmbeddingError(
                self._embedding_mismatch_message(
                    "Постоянная память создана моделью", embedding_model, stored_model
                )
            )

    def validate_temp_embedding(
        self,
        embedding_model: str,
    ) -> None:
        """Fail before inference if any active vector temp/KB document uses another model."""
        target_docs = self._get_all_temp_docs()
        for d_id, doc in target_docs.items():
            if not self._document_supports_mode(doc, "vector") or "chunks" not in doc:
                continue
            doc_model = doc.get("embedding_model") or doc.get("metadata", {}).get("embedding_model")
            if doc_model and embedding_model != "unknown" and doc_model != embedding_model:
                raise EmbeddingError(
                    self._embedding_mismatch_message(
                        f"Документ '{d_id}' создан моделью", embedding_model, doc_model
                    )
                )

    # =========================================================================
    # ПОИСК С ВАЛИДАЦИЕЙ МОДЕЛЕЙ ЭМБЕДДИНГОВ
    # =========================================================================

    @log_operation('memory', 'MEMORY')
    def search_temp_text(
        self,
        keywords: List[str],
        max_pages: int = 3
    ) -> List[Dict[str, Any]]:
        clean_keywords = [k.lower().strip() for k in keywords if k.strip()]
        if not clean_keywords:
            return []

        target_docs = self._get_all_temp_docs()
        if not target_docs:
            return []

        matched_pages = []

        for d_id, doc in target_docs.items():
            if self._document_supports_mode(doc, "text") and "pages" in doc and doc["pages"]:
                for page in doc["pages"]:
                    text_lower = page["text"].lower()
                    hits = sum(text_lower.count(k) for k in clean_keywords)
                    if all(k in text_lower for k in clean_keywords):
                        hits += 5
                    if hits > 0:
                        matched_pages.append({
                            "doc_id": d_id,
                            "page_num": page.get("page_num", 1),
                            "source": doc.get("source", "unknown"),
                            "score": hits,
                            "text": page["text"]
                        })
        if not matched_pages:
            return []

        if self.limit_ranking:
            matched_pages.sort(key=lambda x: x["score"], reverse=True)
            return matched_pages[:max_pages]
        else:
            return matched_pages

    @log_operation('memory', 'MEMORY')
    def search_temp_vector(
        self,
        query_vector: List[float],
        limit: int = 4,
        *,
        embedding_model: Optional[str] = None,
        embedding_dimension: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        target_docs = self._get_all_temp_docs()
        if not target_docs:
            return []

        query_model = embedding_model or self._get_active_embedding_model()
        if embedding_dimension is not None and len(query_vector) != int(embedding_dimension):
            raise EmbeddingError(
                f"Query embedding dimension is {len(query_vector)}, expected {embedding_dimension} "
                f"for '{query_model}'."
            )

        results = []
        for d_id, doc in target_docs.items():
            if not self._document_supports_mode(doc, "vector") or "chunks" not in doc:
                continue

            doc_model = doc.get("embedding_model") or doc.get("metadata", {}).get("embedding_model")
            if doc_model and query_model != "unknown" and doc_model != query_model:
                raise EmbeddingError(
                    self._embedding_mismatch_message(
                        f"Документ '{d_id}' создан моделью", query_model, doc_model
                    )
                )

            for chunk in doc.get("chunks", []):
                if "vector" not in chunk:
                    continue
                stored_vector = chunk["vector"]
                if len(stored_vector) != len(query_vector):
                    raise EmbeddingError(
                        f"Несовпадение размерности embedding-векторов в '{d_id}': "
                        f"query={len(query_vector)}, stored={len(stored_vector)}."
                    )
                results.append({
                    "doc_id": d_id,
                    "chunk_id": chunk.get("chunk_id", 1),
                    "source": doc.get("source", "unknown"),
                    "_vector": stored_vector,
                    "text": chunk["text"]
                })

        self._score_vectors(query_vector, results)
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    @log_operation('memory', 'MEMORY')
    def search_permanent_memory(
        self,
        query_vector: List[float],
        limit: int = 3,
        *,
        embedding_model: Optional[str] = None,
        embedding_dimension: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        stored_model, facts = self._read_permanent_db()
        if not facts:
            return []

        query_model = embedding_model or self._get_active_embedding_model()
        if stored_model and query_model != "unknown" and stored_model != query_model:
            raise EmbeddingError(
                self._embedding_mismatch_message(
                    "Постоянная память создана моделью", query_model, stored_model
                )
            )
        if embedding_dimension is not None and len(query_vector) != int(embedding_dimension):
            raise EmbeddingError(
                f"Query embedding dimension is {len(query_vector)}, expected {embedding_dimension} "
                f"for '{query_model}'."
            )

        results = []
        for record in facts:
            stored_vector = record["vector"]
            if len(stored_vector) != len(query_vector):
                raise EmbeddingError(
                    "Несовпадение размерности embedding-векторов в постоянной памяти: "
                    f"query={len(query_vector)}, stored={len(stored_vector)}."
                )
            results.append({
                "id": record["id"],
                "text": record["text"],
                "_vector": stored_vector,
                "timestamp": record["timestamp"],
            })
        self._score_vectors(query_vector, results)
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def read_temp_data(self, doc_id: str) -> Optional[Dict[str, Any]]:
        all_docs = self._get_all_temp_docs()
        return all_docs.get(doc_id)

    # =========================================================================
    # СОХРАНЕНИЕ В ПАМЯТЬ
    # =========================================================================

    def _paginate_text(self, content: str, min_chars: int = 1000, hard_limit: int = 2500) -> List[str]:
        from core.text_pagination import paginate_text
        return paginate_text(content, min_chars, hard_limit)

    @log_operation('memory', 'MEMORY')
    def init_temp_doc(self, source: str = "unknown") -> str:
        formats = {"text", "vector"} if self.store_both_formats else {self.active_mode}
        doc = {"source": source, "timestamp": datetime.now().isoformat()}
        self._set_document_formats(doc, formats)
        if 'text' in formats:
            doc['pages'] = []
        if 'vector' in formats:
            doc.update(embedding_model=None, chunks=[])
        return self.temp_store.add_document(doc)

    def _drop_vector_representation(self, doc: Dict[str, Any], error: Exception) -> None:
        """Keep the text copy usable when optional dual-format embedding fails."""
        core_logger.event('memory', 'MEMORY_EVENT', f"[Temp Memory] Vector copy skipped; text copy was preserved: {error}", level='INFO')
        doc.pop("chunks", None)
        doc.pop("embedding_model", None)
        formats = self._document_formats(doc)
        formats.discard("vector")
        if "text" not in formats and isinstance(doc.get("pages"), list):
            formats.add("text")
        self._set_document_formats(doc, formats or {"text"})

    async def append_temp_stream_data(self, doc_id: str, content: str, title: str = "", url: str = "", client=None):
        doc = await run_data_task(self.temp_store.read_document_header, doc_id)
        if doc is None:
            return
        pages, chunks = [], []
        model_id, drop_vector = None, False
        if self._document_supports_mode(doc, 'text'):
            annotated = f"### [Раздел: {title}] (URL: {url})\n\n{content}"
            pieces = await run_data_task(self._paginate_text, annotated)
            pages = [{'title': title, 'url': url, 'text': text} for text in pieces]
        if self._document_supports_mode(doc, 'vector'):
            fallback = self._document_supports_mode(doc, 'text') and self.store_both_formats
            try:
                active_client = client or self.client
                if not active_client:
                    raise EmbeddingError('Embedding client is not configured for vector temporary memory')
                session = active_client.embedding_snapshot()
                model_id = session.identity
                if doc.get('_has_chunks') and doc.get('embedding_model') and doc['embedding_model'] != model_id:
                    raise EmbeddingError(f"Нельзя дописать документ '{doc_id}': несовместимые embedding-модели.")
                for start in range(0, len(content), 1800):
                    text = content[start:start+1800].strip()
                    if not text:
                        continue
                    try:
                        vector = await session.embed(text, purpose='document')
                    except EmbeddingError:
                        raise
                    except Exception as error:
                        if fallback:
                            raise
                        core_logger.event('memory', 'MEMORY_EVENT', f'[Vector Stream Error] {error}', level='ERROR')
                        continue
                    chunks.append({'source_url': url, 'title': title, 'text': text, 'vector': vector})
            except Exception as error:
                if not fallback:
                    raise
                self._drop_vector_representation(doc, error)
                drop_vector = True
                chunks = []
        # Re-read and append under a short transaction. Concurrent stream chunks
        # and document ingestion cannot overwrite another operation's changes.
        await run_data_task(self.temp_store.append_document, doc_id, pages=pages, chunks=chunks,
                            embedding_model=model_id, drop_vector=drop_vector)

    @log_operation('memory', 'MEMORY')
    def save_temp_text_data(self, content: str, source: str = "unknown") -> str:
        pages = [{'page_num': i, 'text': text} for i, text in enumerate(self._paginate_text(content), 1)]
        doc = {'source': source, 'pages': pages, 'timestamp': datetime.now().isoformat()}
        self._set_document_formats(doc, {'text'})
        return self.temp_store.add_document(doc)

    @log_operation('memory', 'MEMORY')
    async def save_temp_data(
        self,
        content: str,
        source: str = "unknown",
        client: Optional[Any] = None
    ) -> str:
        active_client = client or self.client
        desired_formats = {self.active_mode}
        if self.store_both_formats:
            desired_formats = {"text", "vector"}

        doc: Dict[str, Any] = {
            "source": source,
            "timestamp": datetime.now().isoformat(),
        }

        if "text" in desired_formats:
            pages = await run_data_task(self._paginate_text, content)
            doc["pages"] = [
                {"page_num": idx + 1, "text": page}
                for idx, page in enumerate(pages)
            ]

        formats_written = {"text"} if "text" in desired_formats else set()
        if "vector" in desired_formats:
            can_fallback_to_text = "text" in desired_formats and self.store_both_formats
            try:
                if not active_client:
                    raise EmbeddingError("Embedding client is not configured for vector temporary memory")
                session = active_client.embedding_snapshot()
                chunks_data = []
                chunk_size = 1800
                for i in range(0, len(content), chunk_size):
                    chunk_text = content[i:i+chunk_size].strip()
                    if not chunk_text:
                        continue
                    try:
                        vector = await session.embed(chunk_text, purpose="document")
                    except EmbeddingError:
                        raise
                    except Exception as error:
                        if can_fallback_to_text:
                            raise
                        core_logger.event('memory', 'MEMORY_EVENT', f"[Vector Embed Error] {error}", level='ERROR')
                        continue
                    chunks_data.append({
                        "chunk_id": len(chunks_data) + 1,
                        "text": chunk_text,
                        "vector": vector
                    })
                doc["embedding_model"] = session.identity
                doc["chunks"] = chunks_data
                formats_written.add("vector")
            except Exception as error:
                if can_fallback_to_text:
                    core_logger.event('memory', 'MEMORY_EVENT', f"[Temp Memory] Vector copy skipped; text copy was preserved: {error}", level='INFO')
                else:
                    raise

        self._set_document_formats(doc, formats_written)
        return await run_data_task(self.temp_store.add_document, doc)

    @log_operation('memory', 'MEMORY')
    def save_permanent_fact(
        self,
        text: str,
        vector: List[float],
        *,
        embedding_model: Optional[str] = None,
        embedding_dimension: Optional[int] = None,
    ) -> str:
        stored_model, facts = self._read_permanent_db()
        vector_model = embedding_model or self._get_active_embedding_model()
        if embedding_dimension is not None and len(vector) != int(embedding_dimension):
            raise EmbeddingError(
                f"Embedding dimension is {len(vector)}, expected {embedding_dimension} "
                f"for '{vector_model}'."
            )
        if facts and stored_model and stored_model != vector_model:
            raise EmbeddingError(
                self._embedding_mismatch_message(
                    "Постоянная память создана моделью", vector_model, stored_model
                )
            )
        if facts and len(facts[0].get("vector", [])) != len(vector):
            raise EmbeddingError(
                "Нельзя добавить факт: размерность embedding-вектора отличается от уже сохранённых фактов."
            )
        return SQLiteStore(self.permanent_path, 'permanent').add_fact(text, vector, vector_model)

    @log_operation('memory', 'MEMORY')
    def delete_permanent_fact(self, fact_id: str) -> bool:
        return SQLiteStore(self.permanent_path, 'permanent').delete_fact(fact_id)

    def get_all_permanent_facts(self) -> List[Dict[str, Any]]:
        _, facts = self._read_permanent_db()
        return [{"id": r["id"], "text": r["text"], "timestamp": r["timestamp"]} for r in facts]

    @staticmethod
    def _score_vectors(query_vector, rows):
        """Bounded native batches; normalize the query once per search."""
        query = np.asarray(query_vector, dtype=np.float64)
        query_norm = np.linalg.norm(query)
        for start in range(0, len(rows), 256):
            batch = rows[start:start + 256]
            vectors = np.asarray([row.pop('_vector') for row in batch], dtype=np.float64)
            norms = np.sqrt(np.einsum('ij,ij->i', vectors, vectors)) * query_norm
            scores = np.divide(np.einsum('ij,j->i', vectors, query), norms,
                               out=np.zeros(len(batch)), where=norms != 0)
            for row, score in zip(batch, scores):
                row['score'] = float(score)

    def _cosine_similarity(self, v1: List[float], v2: List[float]) -> float:
        a, b = np.array(v1), np.array(v2)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))
