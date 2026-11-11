import asyncio
import json
import re
import time
from contextlib import asynccontextmanager, aclosing
from pathlib import Path
from typing import List, Dict, Any, Optional

import httpx
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage

from core.logger import core_logger, log_operation
from core.chat_references import CHAT_ID, model_content, model_assistant_parts, model_message_metadata
from core.embeddings import EmbeddingError, EmbeddingService
from core.attachments import (
    AttachmentError,
    file_to_base64,
    file_to_data_url,
    read_text_attachment,
    resolve_attachment_path,
)


class ContinuationUnsupported(RuntimeError):
    """Provider explicitly rejected an assistant-prefix continuation."""


class InterruptedModelStream(RuntimeError):
    pass


class LlmClient:
    def __init__(
        self,
        llm_url: str = "http://localhost:8080/v1",
        embed_url: str = "http://localhost:8081/v1",
        model_name: str = "local-agent-core",
        embedding_model_name: str = "embedding-model",
    ):
        self.llm_url = llm_url
        self.embed_url = embed_url
        self.model_name = model_name
        self.embedding_model_name = embedding_model_name
        self._timeout_override = None
        self._continuation_capabilities = {}
        self._continuation_server_info = {}
        self.embedding_service = EmbeddingService(
            fallback_url=self.embed_url,
            fallback_model=self.embedding_model_name,
            timeout=httpx.Timeout(6000.0, connect=15.0),
        )

    @property
    def timeout(self):
        if self._timeout_override is not None:return self._timeout_override
        from core.timeout_settings import load_settings
        limits = load_settings()
        return httpx.Timeout(float(limits['response_timeout']), connect=float(limits['connect_timeout']))

    @timeout.setter
    def timeout(self, value):
        self._timeout_override = value

    def model_connection(self, connection_id=None):
        from core.model_connections import snapshot
        return snapshot(self.llm_url, self.model_name, connection_id=connection_id)

    def bind_connection(self, connection=None):
        from core.model_connections import bind_model_connection
        return bind_model_connection(connection or self.model_connection())

    @staticmethod
    def _has_attachments(messages: List[BaseMessage]) -> bool:
        for msg in messages:
            if isinstance(msg, HumanMessage):
                attachments = (getattr(msg, "additional_kwargs", {}) or {}).get("attachments") or []
                if attachments:
                    return True
        return False

    def _human_content_to_openai(self, msg: HumanMessage, session_id: Optional[str]) -> Any:
        kwargs = dict(getattr(msg, "additional_kwargs", {}) or {})
        attachments = list(kwargs.get("attachments") or [])
        if not attachments:
            return msg.content

        parts: List[Dict[str, Any]] = []
        text = str(msg.content or "")
        if text:
            parts.append({"type": "text", "text": text})

        for position, attachment in enumerate(attachments, 1):
            if not isinstance(attachment, dict):
                continue
            filename = str(attachment.get("filename") or "attachment")
            mime_type = str(attachment.get("mime_type") or "application/octet-stream")
            media_type = str(attachment.get("media_type") or "").lower()
            parts.append({'type': 'text', 'text': f'Attachment #{position}: {filename} ({media_type}). The number matches the attachment order in this message.'})
            from modules.system.file_manager.documents import DOCUMENT_SUFFIXES
            document = Path(filename).suffix.lower() in DOCUMENT_SUFFIXES
            if media_type == 'file' or (media_type == 'text' and document and not attachment.get('temp_memory')):
                try:
                    original_path = resolve_attachment_path(attachment, session_id)
                    original_location = f"Original file path: {original_path}; file URL: {original_path.as_uri()}; "
                except (AttachmentError, OSError):
                    original_location = f"Original file is currently unavailable; saved attachment path: {attachment.get('path') or 'unknown'}; "
                parts.append({'type':'text','text':
                    f"Uploaded file: {filename}; size: {attachment.get('size', 0)} bytes; "
                    + f"Document/read tool path: attachment://{attachment.get('id', '')}. "
                    + original_location
                    + f"Project-relative path: {attachment.get('workspace_path') or 'not inside Project Folder'}. "
                    + ('Use inspect_document, read_document, search_document or read_spreadsheet to read selected content. '
                     'Use render_document for page images if you support vision. '
                     'The original file is also available through the terminal mapping. Binary contents are not inlined.'
                     if document else 'Use the terminal mapping to inspect or unpack it. Binary contents are not model input.')})
                continue

            reference = attachment.get('temp_memory')
            if media_type == 'text' and isinstance(reference, dict) and reference.get('document_id'):
                parts.append({'type': 'text', 'text':
                    f"Application attachment notice: the user explicitly loaded '{filename}' into this session's temporary memory. "
                    f"Document ID: {reference['document_id']}. "
                    'The full file is not included in this message. Use temporary-memory search/read tools to retrieve relevant content. '
                    'Temporary-memory searches cover the shared database; the document ID identifies this upload.'})
                continue
            if attachment.get('memory_only'):
                raise AttachmentError('У вложения для памяти отсутствует идентификатор документа: ' + filename)
            path = resolve_attachment_path(attachment, session_id)
            if media_type == "text":
                body = read_text_attachment(path)
                parts.append({
                    "type": "text",
                    "text": (
                        f"\n--- BEGIN ATTACHED TEXT FILE: {filename} ---\n"
                        f"{body}\n"
                        f"--- END ATTACHED TEXT FILE: {filename} ---"
                    ),
                })
                continue

            if media_type == "image":
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": file_to_data_url(path, mime_type)},
                })
                continue

            if media_type == "audio":
                suffix = path.suffix.lower().lstrip(".")
                audio_format = {
                    "mpeg": "mp3",
                    "mpga": "mp3",
                    "wave": "wav",
                    "x-wav": "wav",
                }.get(mime_type.split("/")[-1].lower(), suffix or "wav")
                parts.append({
                    "type": "input_audio",
                    "input_audio": {
                        "data": file_to_base64(path),
                        "format": audio_format,
                    },
                })
                continue

            if media_type == "video":
                # video_url is used by a number of OpenAI-compatible multimodal
                # engines (for example some vLLM-backed deployments). Providers
                # that do not implement it will return a clear capability error.
                parts.append({
                    "type": "video_url",
                    "video_url": {"url": file_to_data_url(path, mime_type)},
                })
                continue

            raise AttachmentError(f"Неизвестный тип вложения '{media_type}' для файла '{filename}'.")

        if not parts:
            return msg.content
        return parts

    def _convert_messages_to_openai(
        self,
        messages: List[BaseMessage],
        *,
        session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        openai_messages = []
        from core.tool_visuals import with_tool_visuals
        messages = with_tool_visuals(messages)
        for msg in messages:
            if isinstance(msg, SystemMessage):
                openai_messages.append({"role": "system", "content": msg.content})
            elif isinstance(msg, HumanMessage):
                metadata = model_message_metadata('user', msg.additional_kwargs.get(CHAT_ID), inline=True)
                body = model_content(msg)
                if metadata:
                    from core.model_compatibility import annotate
                    body = annotate(body, metadata['content'])
                model_message = msg.model_copy(update={'content': body})
                openai_messages.append({
                    "role": "user",
                    "content": self._human_content_to_openai(model_message, session_id),
                })
            elif isinstance(msg, AIMessage):
                for identity, content in model_assistant_parts(msg):
                    metadata = model_message_metadata('assistant', identity)
                    if metadata:
                        # ID — контекст приложения, а не образец текста ответа модели.
                        openai_messages.append(metadata)
                    assistant_msg = {"role": "assistant", "content": content}
                    if msg.tool_calls:
                        assistant_msg["tool_calls"] = [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["args"]) if isinstance(tc["args"], dict) else tc["args"],
                                },
                            }
                            for tc in msg.tool_calls
                        ]
                    openai_messages.append(assistant_msg)
            elif isinstance(msg, ToolMessage):
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": str(msg.content),
                })
        return openai_messages

    def _convert_messages_to_local(self, messages, *, session_id=None, native_reasoning=True):
        """Keep thought markup out of ordinary local assistant content.

        The server's actual model template owns channel delimiters. Cloud native
        payloads and unsupported prefix-continuation requests use their own path.
        """
        formatted = self._convert_messages_to_openai(messages, session_id=session_id)
        if not native_reasoning:
            return formatted
        for message in formatted:
            content = message.get('content')
            if message.get('role') != 'assistant' or not isinstance(content, str):
                continue
            thoughts = []
            while True:
                match = re.match(r'^<think>([\s\S]*?)</think>', content)
                if not match:
                    break
                thoughts.append(match.group(1))
                content = content[match.end():].removeprefix('\n')
            if thoughts:
                message['content'] = content
                message['reasoning_content'] = '\n'.join(thoughts)
        return formatted

    @staticmethod
    def _attachment_http_error(status_code: int, detail: str) -> AttachmentError:
        detail = (detail or "").strip()
        if len(detail) > 1200:
            detail = detail[:1200] + "…"
        return AttachmentError(
            "Модель или выбранный OpenAI-compatible движок отклонил вложение. "
            "Скорее всего, этот тип входа не поддерживается текущей моделью/провайдером "
            f"или используется другой формат мультимодального API. HTTP {status_code}."
            + (f"\nОтвет провайдера: {detail}" if detail else "")
        )

    def embedding_snapshot(self):
        """Freeze the current embedding backend for one logical operation."""
        return self.embedding_service.snapshot()

    async def get_embedding(
        self,
        text: str,
        *,
        purpose: str = "document",
        task: Optional[str] = None,
    ) -> List[float]:
        try:
            session = self.embedding_snapshot()
            return await session.embed(text, purpose=purpose, task=task)
        except EmbeddingError:
            raise
        except Exception as e:
            raise EmbeddingError(f"Ошибка при получении эмбеддинга: {e}") from e

    @log_operation('model', 'MODEL_REQUEST')
    async def chat_completion(
        self,
        messages: List[BaseMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        connection = self.model_connection()
        if max_tokens is not None:
            from dataclasses import replace
            connection = replace(connection, options={**connection.options, 'max_tokens': int(max_tokens)})
        from core.model_transports import completion
        return await completion(self, connection, messages, tools, response_format, session_id)

    async def continuation_support(self):
        """Best-effort discovery, keyed by endpoint and model, never by a GGUF name."""
        connection = self.model_connection()
        if connection.mode == 'api':
            return 'unsupported'
        key = (connection.base_url, connection.model)
        if self._continuation_capabilities.get(key) == 'unsupported':
            return 'unsupported'
        if key not in self._continuation_server_info:
            info = {}
            try:
                root = connection.base_url.rstrip('/').removesuffix('/v1')
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(root + '/props')
                    if response.status_code == 200:
                        props = response.json()
                        if isinstance(props, dict) and (props.get('build_info') or 'chat_template' in props
                                                       or 'chat_template_caps' in props):
                            info = {'backend': 'llama.cpp', 'build': props.get('build_info'),
                                    'template_caps': props.get('chat_template_caps', {})}
            except (httpx.HTTPError, ValueError):
                pass
            self._continuation_server_info[key] = info
        # Discovery can identify a backend, but cannot certify its launch flags
        # or prove that an arbitrary model template consumes the prefix.
        return 'available' if self._continuation_server_info[key] else 'unknown'

    @asynccontextmanager
    async def _logged_stream(self, client, payload):
        try:
            from core.model_queue import model_slot
            async with model_slot(self.model_connection()), self._open_model_stream(client, payload) as response:
                yield response
        except asyncio.CancelledError:
            core_logger.event('model', 'MODEL_CANCEL', 'Stream stopped')
            raise
        except Exception as error:
            core_logger.event('model', 'MODEL_ERROR', str(error), level='ERROR')
            raise

    @asynccontextmanager
    async def _open_model_stream(self, client, payload):
        started = time.perf_counter()
        connection = self.model_connection()
        core_logger.event('model', 'MODEL_SEND', f"Request to {connection.model}",
            details={'endpoint':connection.base_url, 'messages':len(payload['messages']), 'tools':len(payload.get('tools', [])),
                     'continuation':payload.get('continue_final_message', False)})
        for attempt in range(3):
            async with client.stream('POST', f'{connection.base_url}/chat/completions', json=payload) as response:
                retry = False
                if response.status_code in {400, 422} and attempt < 2:
                    detail = (await response.aread()).decode('utf-8', errors='replace').casefold()
                    if 'reasoning_control' in payload and 'reasoning_control' in detail:
                        payload.pop('reasoning_control')
                        retry = True
                    elif 'stream_options' in payload and ('stream_options' in detail or 'include_usage' in detail):
                        payload.pop('stream_options')
                        self._usage_options_supported = False
                        retry = True
                if not retry:
                    core_logger.event('model', 'MODEL_HTTP', f'HTTP {response.status_code} · {time.perf_counter()-started:.3f} s')
                    yield response
                    return

    async def chat_completion_stream(
        self,
        messages: List[BaseMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        session_id: Optional[str] = None,
        continuation: Optional[str] = None,
        answer_only: bool = False,
        raw_request: bool = False,
        control_reasoning: bool = False,
        system_prompt_enabled: bool = True,
    ):
        """Асинхронный генератор токенов от OpenAI-compatible LLM-сервера."""
        connection = self.model_connection()
        if not system_prompt_enabled:
            messages = [message.model_copy(deep=True) for message in messages
                        if not isinstance(message, SystemMessage)
                        and not message.additional_kwargs.get('module_guidance')
                        and not message.additional_kwargs.get('runtime_instruction')]
            for message in messages:
                message.additional_kwargs.pop(CHAT_ID, None)
        if raw_request:
            latest=next((m for m in reversed(messages) if isinstance(m,HumanMessage)),None)
            if latest is None or not isinstance(latest.content,str):raise ValueError('Чистый запрос требует текст пользователя.')
            messages=[HumanMessage(content=latest.content)]
            tools=None;continuation=None;answer_only=False
        if connection.mode == 'api' or not (continuation or answer_only):
            if continuation:
                raise ContinuationUnsupported('Облачные API используют логическое продолжение с сохранённым контекстом.')
            from core.model_transports import stream_completion
            async with aclosing(stream_completion(self, connection, messages, tools, session_id=session_id,raw_request=raw_request,control_reasoning=control_reasoning,system_prompt_enabled=system_prompt_enabled)) as events:
                async for delta in events:yield delta
            return
        from core.model_compatibility import discover
        connection = await discover(connection, self)
        endpoint, model = connection.base_url, connection.model
        capability_key = (endpoint, model)
        capability = await self.continuation_support() if continuation or answer_only else 'unknown'
        native_reasoning = not continuation or self._continuation_server_info.get(capability_key, {}).get('backend') == 'llama.cpp'
        from core.model_transports import canonical_messages
        from core.model_compatibility import available_tools
        formatted_messages = canonical_messages(self, messages, connection, session_id)
        if continuation and messages and formatted_messages:
            last_message = messages[-1]
            kwargs = last_message.additional_kwargs
            if kwargs.get('continuation_prefix') and 'resume_reasoning_content' in kwargs:
                # A partial assistant has no completed provider payload yet.
                # Keep its two channels exact, even when model discovery resolved an alias.
                formatted_messages[-1] = {'role': 'assistant', 'content': last_message.content,
                    'reasoning_content': kwargs['resume_reasoning_content'], '_toshy_native': True}
        tools = available_tools(connection, tools)
        from core.model_transports import build_request, StreamDecoder
        _, payload = build_request(connection, formatted_messages, tools)
        formatted_messages = payload['messages']
        decoder = StreamDecoder(connection)

        has_attachments = self._has_attachments(messages)
        if continuation and capability == 'unsupported':
            raise ContinuationUnsupported('Этот сервер ранее отклонил продолжение сообщения ассистента.')
        if (continuation or answer_only) and self._continuation_server_info.get(capability_key, {}).get('backend') == 'llama.cpp':
            # Let the backend select each model's real reasoning delimiters.
            # Literal <think> tags in content do not close Gemma's thought channel.
            for message in formatted_messages:
                content = message.get('content')
                if message.get('role') == 'assistant' and isinstance(content, str):
                    match = re.match(r'^<think>([\s\S]*?)</think>\s*', content)
                    if match:
                        message['reasoning_content'] = match.group(1)
                        message['content'] = content[match.end():]
            if answer_only:
                payload['chat_template_kwargs'] = {'enable_thinking': False}
            if continuation and formatted_messages and formatted_messages[-1].get('role') == 'assistant':
                last = formatted_messages[-1]
                content = last.get('content')
                if not answer_only and isinstance(content, str) and content.startswith('<think>'):
                    # An interrupted thought has no closing tag. Send it as
                    # native reasoning so Gemma and other templates supply
                    # their own channel markers, rather than ordinary content.
                    last['reasoning_content'] = content[len('<think>'):]
                    last['content'] = ''
                if answer_only:
                    payload['continue_final_message'] = 'content'
                elif last.get('content'):
                    payload['continue_final_message'] = 'content'
                elif last.get('reasoning_content'):
                    payload['continue_final_message'] = 'reasoning_content'
                if payload.get('continue_final_message'):
                    # llama.cpp defaults to a new assistant generation prompt.
                    # It must be disabled when continuing the existing message.
                    payload['add_generation_prompt'] = False
                # Only new tokens belong in the stream. The saved prefix is
                # already present in the card and in the run checkpoint.
                payload['echo'] = False
        from core.reasoning_effort import apply
        apply(payload, connection)
        from core.token_usage import usage_request
        from core.reasoning_control import completion_control
        async with completion_control(self, connection, payload, enabled=control_reasoning) as control, httpx.AsyncClient(timeout=self.timeout, headers=connection.headers(), trust_env=connection.options.get('trust_env', True)) as client, usage_request(connection, session_id, messages=formatted_messages, tools=tools) as meter:
            if control_reasoning:
                yield control.change()
            request_started = time.perf_counter()
            async with self._logged_stream(client, payload) as response:
                meter.rejected = 400 <= response.status_code < 500
                if response.is_error and has_attachments:
                    raw = await response.aread()
                    detail = raw.decode("utf-8", errors="replace")
                    raise self._attachment_http_error(response.status_code, detail)
                if response.is_error and continuation:
                    raw = await response.aread()
                    detail = raw.decode('utf-8', errors='replace')
                    normalized = detail.casefold()
                    parameter_conflict = ('add_generation_prompt' in normalized
                                          and 'continue_final_message' in normalized and 'both' in normalized)
                    if response.status_code in {400, 422} and not parameter_conflict and any(token in normalized for token in (
                            'prefill', 'prefix', 'last message', 'end with', 'assistant', 'reasoning', 'chat template',
                            'continue_final_message', 'chat_template_kwargs', 'enable_thinking')):
                        self._continuation_capabilities[capability_key] = 'unsupported'
                        raise ContinuationUnsupported(detail)
                if response.is_error:
                    from core.model_transports import safe_error
                    raise safe_error(response, (await response.aread()).decode('utf-8', errors='replace'), connection)
                terminal = False
                first_delta = True
                finish_reason = None
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            terminal = True
                            break
                        try:
                            data = json.loads(data_str)
                        except ValueError as error:
                            raise InterruptedModelStream('Сервер модели прислал повреждённое событие.') from error
                        if data.get('error'):
                            raise InterruptedModelStream(str(data['error']))
                        meter.observe(data)
                        if 'reasoning_control' not in payload:
                            control.armed = False
                        status = control.observe(data)
                        if status and control_reasoning:
                            yield status
                        if data.get('usage'):
                            core_logger.event('stats', 'MODEL_USAGE', json.dumps(data['usage'], ensure_ascii=False), details=data['usage'])
                        choices = data.get('choices') or []
                        if not choices:  # optional usage-only chunk
                            continue
                        choice = choices[0]
                        delta = choice.get('delta') or {}
                        if delta:
                            if first_delta:
                                core_logger.event('model', 'MODEL_FIRST_DELTA', f'First chunk · wait {time.perf_counter()-request_started:.3f} s')
                                first_delta = False
                            from core.model_compatibility import normalize_delta
                            for decoded in decoder.feed(data):
                                yield normalize_delta(connection, decoded)
                        if choice.get('finish_reason') is not None:
                            finish_reason = choice['finish_reason']
                            terminal = True
                            meter.complete = True
                meter.complete |= terminal
                if not terminal:
                    core_logger.event('model', 'MODEL_DISCONNECT', 'Stream disconnected before the response completed', level='ERROR')
                    raise InterruptedModelStream('Соединение с моделью прервано до завершения ответа.')
                if finish_reason in {'length', 'content_filter', 'error'}:
                    raise InterruptedModelStream('Генерация прервана сервером: ' + str(finish_reason))
                control.active = False
                status = control.change()
                if status and control_reasoning:
                    yield status
                yield {'_provider_data': decoder.provider_data()}
                core_logger.event('model', 'MODEL_STREAM_END', f'Stream completed · {time.perf_counter()-request_started:.3f} s · {finish_reason or "DONE"}')
