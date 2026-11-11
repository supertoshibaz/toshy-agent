from __future__ import annotations

import asyncio
import hashlib
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import numpy as np
import yaml

from core.logger import core_logger, log_operation
from core.paths import APP_PATHS


BUILTIN_PROVIDER = "builtin"
REMOTE_PROVIDER = "openai_compatible"
BUILTIN_MODEL_NAME = "harrier-oss-v1-270m"
BUILTIN_VARIANT = "q4-onnx-community"
BUILTIN_DIMENSION = 640
BUILTIN_IDENTITY = f"builtin:{BUILTIN_MODEL_NAME}:{BUILTIN_VARIANT}:640"
BUILTIN_MODEL_FILENAME = "model_q4.onnx"
BUILTIN_MODEL_DATA_FILENAME = "model_q4.onnx_data"
BUILTIN_TOKENIZER_FILENAME = "tokenizer.json"

# MIT-licensed Q4 ONNX Community artifact derived from the official Microsoft
# Harrier model. The weights are stored as ONNX external data next to the graph.
# Current ONNX Community exports expose `sentence_embedding`; the runtime code
# also supports a last-hidden-state fallback so the backend is not tied to one
# exact export shape.

DEFAULT_QUERY_TASK = "Retrieve relevant passages that answer the current request"
MEMORY_QUERY_TASK = "Retrieve relevant long-term memory facts for the current conversation"
DOCUMENT_QUERY_TASK = "Retrieve relevant passages from memory and documentation for the current request"


class EmbeddingError(RuntimeError):
    """Operational/configuration error in the embedding subsystem."""



def _load_root_config() -> Dict[str, Any]:
    try:
        with open(APP_PATHS.config, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception:
        return {}


def get_embedding_config() -> Dict[str, Any]:
    return dict((_load_root_config().get("embedding") or {}))


def embedding_provider_from_config(config: Optional[Dict[str, Any]] = None) -> str:
    cfg = config or {}
    provider = str(cfg.get("provider") or "").strip().lower()
    # Old Toshy configs had only api_url/model_name. Keep them working as the
    # OpenAI-compatible remote backend until the user explicitly selects the
    # bundled model from App -> Embeddings.
    if not provider:
        return REMOTE_PROVIDER
    if provider in {"builtin", "local", "harrier"}:
        return BUILTIN_PROVIDER
    return REMOTE_PROVIDER


def _remote_settings(config: Optional[Dict[str, Any]] = None):
    cfg = config or {}
    remote = cfg.get("remote") or {}
    api_url = str(remote.get("api_url") or cfg.get("api_url") or "http://localhost:8081/v1").rstrip("/")
    model_name = str(remote.get("model_name") or cfg.get("model_name") or "embedding-model").strip() or "embedding-model"
    try:
        dimension = int(remote.get("dimension") or cfg.get("dimension") or 768)
    except (TypeError, ValueError):
        dimension = 768
    return api_url, model_name, dimension


def embedding_identity_from_config(config: Optional[Dict[str, Any]] = None) -> str:
    cfg = config or {}
    provider = embedding_provider_from_config(cfg)
    if provider == BUILTIN_PROVIDER:
        return BUILTIN_IDENTITY

    api_url, model_name, dimension = _remote_settings(cfg)
    # Do not store the raw endpoint in memory metadata: URLs can contain
    # private hostnames or credentials. The fingerprint still makes two
    # OpenAI-compatible endpoints with the same model name incompatible.
    endpoint_id = hashlib.sha256(api_url.encode("utf-8")).hexdigest()[:16]
    return f"openai-compatible:{endpoint_id}:{model_name}:{dimension}"


def embedding_dimension_from_config(config: Optional[Dict[str, Any]] = None) -> int:
    cfg = config or {}
    if embedding_provider_from_config(cfg) == BUILTIN_PROVIDER:
        return BUILTIN_DIMENSION
    return _remote_settings(cfg)[2]


def bundled_model_dir() -> Path:
    return APP_PATHS.managed_elements / "models" / "embeddings" / BUILTIN_MODEL_NAME


def _dir_has_model(directory: Path) -> bool:
    model = directory / BUILTIN_MODEL_FILENAME
    model_data = directory / BUILTIN_MODEL_DATA_FILENAME
    tokenizer = directory / BUILTIN_TOKENIZER_FILENAME
    try:
        # Q4 is an external-data ONNX export: the .onnx file is only the graph
        # while almost all weights live in the adjacent .onnx_data file.
        # Size checks also reject Git-LFS pointer files / incomplete copies.
        return (
            model.is_file()
            and model.stat().st_size > 100_000
            and model_data.is_file()
            and model_data.stat().st_size > 150_000_000
            and tokenizer.is_file()
            and tokenizer.stat().st_size > 5_000_000
        )
    except OSError:
        return False


def resolve_builtin_model_dir() -> Path:
    return bundled_model_dir()


def builtin_model_status() -> Dict[str, Any]:
    directory = resolve_builtin_model_dir()
    model_path = directory / BUILTIN_MODEL_FILENAME
    model_data_path = directory / BUILTIN_MODEL_DATA_FILENAME
    tokenizer_path = directory / BUILTIN_TOKENIZER_FILENAME
    installed = _dir_has_model(directory)
    total_bytes = 0
    for path in (model_path, model_data_path, tokenizer_path):
        try:
            if path.is_file():
                total_bytes += path.stat().st_size
        except OSError:
            pass

    deps = {"onnxruntime": False, "tokenizers": False}
    try:
        import onnxruntime  # noqa: F401

        deps["onnxruntime"] = True
    except Exception:
        pass
    try:
        import tokenizers  # noqa: F401

        deps["tokenizers"] = True
    except Exception:
        pass

    return {
        "installed": installed,
        "directory": str(directory),
        "model_path": str(model_path),
        "model_data_path": str(model_data_path),
        "tokenizer_path": str(tokenizer_path),
        "bytes": total_bytes,
        "dependencies": deps,
        "ready": installed and all(deps.values()),
        "identity": BUILTIN_IDENTITY,
        "dimension": BUILTIN_DIMENSION,
    }


class RemoteEmbeddingBackend:
    def __init__(self, config: Dict[str, Any], fallback_url: str, fallback_model: str, timeout: httpx.Timeout):
        remote = config.get("remote") or {}
        self.api_url = str(remote.get("api_url") or config.get("api_url") or fallback_url).rstrip("/")
        self.model_name = str(remote.get("model_name") or config.get("model_name") or fallback_model)
        self.timeout = timeout
        self.api_key = str(remote.get('api_key') or config.get('api_key') or '').strip()

    async def embed(self, text: str, *, purpose: str = "document", task: Optional[str] = None) -> List[float]:
        # OpenAI-compatible providers generally apply their own query/document
        # conventions, so Toshy sends the user's raw text here.
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_url}/embeddings",
                json={"input": text, "model": self.model_name},
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]


class HarrierOnnxBackend:
    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config)
        self.max_tokens = max(128, min(int(self.config.get("max_tokens", 4096)), 32768))
        self.threads = max(1, int(self.config.get("threads", min(4, os.cpu_count() or 1))))
        self.dimension = BUILTIN_DIMENSION
        self.pooling = "last"
        self._session = None
        self._tokenizer = None
        self._loaded_dir: Optional[Path] = None
        self._load_lock = threading.Lock()

    def _ensure_artifacts(self) -> Path:
        directory = resolve_builtin_model_dir()
        if _dir_has_model(directory):
            return directory
        raise RuntimeError(
            "Built-in Harrier embedding model files are missing. "
            "Place 'model_q4.onnx', 'model_q4.onnx_data' and 'tokenizer.json' in "
            f"'{directory}'."
        )

    def _ensure_loaded(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return
        with self._load_lock:
            if self._session is not None and self._tokenizer is not None:
                return

            directory = self._ensure_artifacts()
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer
            except ImportError as error:
                raise RuntimeError(
                    "Built-in embeddings require Python packages 'onnxruntime' and 'tokenizers'. "
                    "Install/update Toshy requirements."
                ) from error

            options = ort.SessionOptions()
            options.intra_op_num_threads = self.threads
            options.inter_op_num_threads = 1
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                str(getattr(self, "_model_file", directory / BUILTIN_MODEL_FILENAME)),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            tokenizer = Tokenizer.from_file(str(directory / BUILTIN_TOKENIZER_FILENAME))
            tokenizer.enable_truncation(max_length=self.max_tokens)
            self._tokenizer = tokenizer
            self._loaded_dir = directory
            core_logger.info("EMBED_READY", f"Loaded {embedding_identity_from_config(self.config)} from {directory}")

    @staticmethod
    def _query_text(text: str, task: Optional[str]) -> str:
        instruction = (task or DEFAULT_QUERY_TASK).strip()
        return f"Instruct: {instruction}\nQuery: {text}"

    @staticmethod
    def _numpy_dtype(onnx_type: str):
        return np.int32 if "int32" in str(onnx_type).lower() else np.int64

    def _embed_sync(self, text: str, purpose: str, task: Optional[str]) -> List[float]:
        self._ensure_loaded()
        assert self._session is not None
        assert self._tokenizer is not None

        prepared = self._query_text(text, task) if purpose == "query" else text
        encoding = self._tokenizer.encode(prepared, add_special_tokens=True)
        ids = list(encoding.ids)
        if not ids:
            raise RuntimeError("Embedding tokenizer produced an empty token sequence")

        feed: Dict[str, np.ndarray] = {}
        seq_len = len(ids)
        for input_info in self._session.get_inputs():
            name = input_info.name
            dtype = self._numpy_dtype(input_info.type)
            if name == "input_ids":
                feed[name] = np.asarray([ids], dtype=dtype)
            elif name == "attention_mask":
                feed[name] = np.ones((1, seq_len), dtype=dtype)
            elif name == "position_ids":
                feed[name] = np.arange(seq_len, dtype=dtype)[None, :]
            elif name == "token_type_ids":
                feed[name] = np.zeros((1, seq_len), dtype=dtype)
            else:
                raise RuntimeError(f"Unsupported embedding ONNX input: {name}")

        output_infos = self._session.get_outputs()
        output_names = [output.name for output in output_infos]
        raw_outputs = self._session.run(None, feed)
        outputs = dict(zip(output_names, raw_outputs))

        if "sentence_embedding" in outputs:
            vector = np.asarray(outputs["sentence_embedding"], dtype=np.float32).reshape(-1)
        else:
            hidden = outputs.get("last_hidden_state")
            if hidden is None:
                # Be tolerant of equivalent ONNX exports that use another name
                # for the token-level hidden state. Harrier's hidden size is 640.
                for value in raw_outputs:
                    candidate = np.asarray(value)
                    if candidate.ndim == 3 and candidate.shape[-1] == self.dimension:
                        hidden = candidate
                        break
            if hidden is None:
                raise RuntimeError(
                    "ONNX export has no 'sentence_embedding' or compatible token hidden-state output. "
                    f"Available outputs: {', '.join(output_names)}"
                )
            hidden = np.asarray(hidden, dtype=np.float32)
            # We encode one unpadded sequence at a time, therefore the final
            # non-padding token is simply seq_len - 1. This mirrors Microsoft's
            # documented last-token pooling for Harrier.
            vector = (hidden[0].mean(axis=0) if self.pooling == "mean" else hidden[0, 0, :] if self.pooling == "cls" else hidden[0, seq_len - 1, :]).reshape(-1)

        if vector.size != self.dimension:
            raise RuntimeError(
                f"Unexpected embedding dimension: {vector.size}; expected {self.dimension}"
            )
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= 0.0:
            raise RuntimeError("Embedding backend produced an invalid vector")
        vector /= norm
        return vector.tolist()

    async def embed(self, text: str, *, purpose: str = "document", task: Optional[str] = None) -> List[float]:
        return await asyncio.to_thread(self._embed_sync, text, purpose, task)


@dataclass(frozen=True)
class EmbeddingSession:
    """Immutable snapshot of one embedding backend for one logical operation.

    Changing App -> Embeddings after this object is created affects only future
    snapshots. A long indexing job therefore cannot mix vector spaces midway.
    """

    identity: str
    dimension: int
    provider: str
    backend: Any

    @log_operation('memory', 'EMBED')
    async def embed(
        self,
        text: str,
        *,
        purpose: str = "document",
        task: Optional[str] = None,
    ) -> List[float]:
        if purpose not in {"document", "query"}:
            raise ValueError("Embedding purpose must be 'document' or 'query'")
        try:
            vector = await self.backend.embed(text, purpose=purpose, task=task)
        except EmbeddingError:
            raise
        except Exception as error:
            raise EmbeddingError(str(error)) from error
        if len(vector) != self.dimension:
            raise EmbeddingError(
                f"Embedding dimension mismatch for '{self.identity}': "
                f"backend returned {len(vector)}, expected {self.dimension}."
            )
        return vector


class EmbeddingService:
    """Hot-reloadable router between bundled Harrier and remote embeddings."""

    def __init__(
        self,
        *,
        fallback_url: str = "http://localhost:8081/v1",
        fallback_model: str = "embedding-model",
        timeout: Optional[httpx.Timeout] = None,
    ):
        self.fallback_url = fallback_url
        self.fallback_model = fallback_model
        self.timeout = timeout or httpx.Timeout(6000.0, connect=15.0)
        self._backend = None
        self._signature = None

    def _config_signature(self, cfg: Dict[str, Any]):
        provider = embedding_provider_from_config(cfg)
        if provider == BUILTIN_PROVIDER:
            return (
                provider,
                int(cfg.get("max_tokens", 4096)),
                int(cfg.get("threads", min(4, os.cpu_count() or 1))),
            )
        api_url, model_name, dimension = _remote_settings(cfg)
        remote = cfg.get('remote') or {}
        return (provider, api_url, model_name, dimension, remote.get('api_key', cfg.get('api_key', '')))

    def _backend_for_config(self, cfg: Dict[str, Any]):
        signature = self._config_signature(cfg)
        if self._backend is not None and signature == self._signature:
            return self._backend

        if embedding_provider_from_config(cfg) == BUILTIN_PROVIDER:
            backend = HarrierOnnxBackend(cfg)
        else:
            backend = RemoteEmbeddingBackend(cfg, self.fallback_url, self.fallback_model, self.timeout)
        self._backend = backend
        self._signature = signature
        return backend

    def snapshot(self) -> EmbeddingSession:
        """Capture the currently configured embedding space atomically."""
        cfg = get_embedding_config()
        provider = embedding_provider_from_config(cfg)
        backend = self._backend_for_config(cfg)
        return EmbeddingSession(
            identity=embedding_identity_from_config(cfg),
            dimension=embedding_dimension_from_config(cfg),
            provider=provider,
            backend=backend,
        )

    async def embed(self, text: str, *, purpose: str = "document", task: Optional[str] = None) -> List[float]:
        try:
            session = self.snapshot()
            return await session.embed(text, purpose=purpose, task=task)
        except EmbeddingError as error:
            core_logger.error("EMBED_ERROR", f"Embedding error: {error}")
            raise
        except Exception as error:
            wrapped = EmbeddingError(str(error))
            core_logger.error("EMBED_ERROR", f"Embedding error: {wrapped}")
            raise wrapped from error

