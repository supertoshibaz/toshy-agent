from __future__ import annotations

import copy
from core.background import run_data_task
from typing import List

from core.registry import BaseTool
from core.llm_client import LlmClient
from core.embeddings import DOCUMENT_QUERY_TASK, MEMORY_QUERY_TASK, EmbeddingError
from .manager import MemoryManager
from .settings import load_settings
from .page_delivery import new_pages


def _page_limit(memory, options):
    return options['page_limit'] if memory.limit_ranking else None


class RememberThisTool(BaseTool):
    def __init__(self, memory: MemoryManager, client: LlmClient):
        super().__init__(
            name="remember_this",
            description="Saves durable facts about the user to permanent memory. Proactively remember explicitly learned facts about who the user is, their work, goals, preferences, and the software, hardware and workflows they use, even without being asked. Keep facts concise and accurate; avoid duplicates and transient task progress.",
            args_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "The fact, rule, or preference to remember."}},
                "required": ["text"]
            }
        )
        self.memory = memory
        self.client = client

    async def execute(self, text: str, **kwargs) -> str:
        session = self.client.embedding_snapshot()
        # Compare vector-space metadata before touching the backend. This makes
        # model mismatch errors visible even if the newly selected remote
        # embedding endpoint is currently unreachable.
        self.memory.validate_permanent_embedding(session.identity)
        vector = await session.embed(text, purpose="document")
        fact_id = self.memory.save_permanent_fact(
            text,
            vector,
            embedding_model=session.identity,
            embedding_dimension=session.dimension,
        )
        return f"System: Fact successfully saved to permanent memory. ID: {fact_id}"


class ForgetThisTool(BaseTool):
    def __init__(self, memory: MemoryManager):
        super().__init__(
            name="forget_this",
            description="Deletes a specific fact from permanent memory using its ID.",
            args_schema={
                "type": "object",
                "properties": {"fact_id": {"type": "string", "description": "The ID of the fact to delete."}},
                "required": ["fact_id"]
            }
        )
        self.memory = memory

    async def execute(self, fact_id: str, **kwargs) -> str:
        success = self.memory.delete_permanent_fact(fact_id)
        if success:
            return f"System: Fact '{fact_id}' has been permanently deleted."
        return f"System: Error - Fact with ID '{fact_id}' not found."


class SearchMemoryTool(BaseTool):
    def __init__(self, memory: MemoryManager, client: LlmClient):
        super().__init__(
            name="search_memory",
            description="Searches your internal vector database for saved facts relevant to a query.",
            args_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The semantic query to search for."}},
                "required": ["query"]
            }
        )
        self.memory = memory
        self.client = client

    async def execute(self, query: str, **kwargs) -> str:
        try:
            session = self.client.embedding_snapshot()
            await run_data_task(self.memory.validate_permanent_embedding, session.identity)
            vector = await session.embed(query, purpose="query", task=MEMORY_QUERY_TASK)
            matches = await run_data_task(self.memory.search_permanent_memory,
                vector,
                limit=4,
                embedding_model=session.identity,
                embedding_dimension=session.dimension,
            )
            if not matches:
                return "System: No relevant facts found in memory."
            results = [f"- [ID: {m['id']}] {m['text']} (relevance: {m['score']:.2f})" for m in matches]
            return "System: Found relevant facts:\n" + "\n".join(results)
        except EmbeddingError:
            raise
        except Exception as e:
            return str(e)


class ListMemoriesTool(BaseTool):
    def __init__(self, memory: MemoryManager):
        super().__init__(
            name="list_memories",
            description="Show all saved permanent memory facts and their IDs.",
            args_schema={"type": "object", "properties": {}}
        )
        self.memory = memory

    async def execute(self, **kwargs) -> str:
        facts = self.memory.get_all_permanent_facts()
        if not facts:
            return "System: Permanent memory is currently empty."
        results = [f"[ID: {f['id']}] (Saved: {f['timestamp'][:10]}) - {f['text']}" for f in facts]
        return "System: All saved memories:\n" + "\n".join(results)


class SearchTempMemoryTool(BaseTool):
    """Stable model-facing search interface for the session temporary memory."""

    def __init__(self, memory: MemoryManager, client: LlmClient):
        super().__init__(
            name="search_temp_memory",
            description=(
                "Searches temporary memory as one logical database. ALWAYS provide both representations "
                "of the same search intent: 'semantic_query' is a natural-language semantic query for "
                "vector memory, while 'keywords' is a list of precise keywords or key phrases for "
                "paginated text memory. The backend uses only the representation required by the "
                "session's active memory mode and discards the other one. Search is always across the "
                "entire active temporary-memory database; do not target individual document IDs. "
                "This tool never searches module knowledge bases; use 'search_knowledge_base' for those."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "semantic_query": {
                        "type": "string",
                        "description": (
                            "Natural-language semantic formulation of what to find. Required on every "
                            "call even when the current memory mode is paginated text."
                        ),
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Precise keywords or key phrases for strict page search. Required on every "
                            "call even when the current memory mode is vector."
                        ),
                    },
                },
                "required": ["semantic_query", "keywords"],
            },
        )
        self.memory = memory
        self.client = client
        self.options = load_settings()
        self._search_schema = copy.deepcopy(self.args_schema)
        self._search_description = self.description

    def for_execution(self, context):
        tool = copy.copy(self)
        tool.options = load_settings()
        tool.args_schema = copy.deepcopy(self._search_schema)
        tool.description = self._search_description
        if tool.options['temp_query_arguments'] == 'active_mode':
            key = 'semantic_query' if context.temp_memory_mode == 'vector' else 'keywords'
            tool.args_schema['properties'] = {key: tool.args_schema['properties'][key]}
            tool.args_schema['required'] = [key]
            tool.args_schema['properties'][key]['description'] = (
                'Natural-language semantic formulation of what to find.' if key == 'semantic_query'
                else 'Precise keywords or key phrases for strict page search.')
            tool.description = (
                "Search the entire session temporary-memory database. Provide only '" + key +
                "' for its current memory type. This tool never searches module knowledge bases."
            )
        tool.description += (' Previously returned text pages are omitted until context compression; '
                             'updated page content can be returned again. Vector search is unchanged.')
        return tool

    async def execute(
        self,
        semantic_query: str = '',
        keywords: List[str] = None,
        **kwargs,
    ) -> str:
        mode = self.memory.active_mode

        if mode == "vector":
            query = str(semantic_query or "").strip()
            if not query:
                return "System: 'semantic_query' must not be empty."
            try:
                session = self.client.embedding_snapshot()
                await run_data_task(self.memory.validate_temp_embedding, session.identity)
                vector = await session.embed(query, purpose="query", task=DOCUMENT_QUERY_TASK)
                matches = await run_data_task(self.memory.search_temp_vector,
                    query_vector=vector,
                    limit=4,
                    embedding_model=session.identity,
                    embedding_dimension=session.dimension,
                )
                if not matches:
                    return f"System: No relevant temporary-memory chunks found for query: '{query}'"

                formatted = []
                for match in matches:
                    header = (
                        f"--- [Chunk: {match['chunk_id']} | Source: {match['source']} | "
                        f"Relevance: {match['score']:.2f}] ---"
                    )
                    formatted.append(f"{header}\n{match['text']}")
                return "System: Found relevant temporary-memory chunks:\n\n" + "\n\n".join(formatted)
            except EmbeddingError:
                raise
            except Exception as error:
                return str(error)

        clean_keywords = [str(item).strip() for item in (keywords or []) if str(item).strip()]
        if not clean_keywords:
            return "System: 'keywords' must contain at least one precise keyword or key phrase."

        matches = await run_data_task(self.memory.search_temp_text, keywords=clean_keywords, max_pages=None)
        if not matches:
            return f"System: No temporary-memory pages matched keywords: {clean_keywords}"
        matches = await new_pages(matches, 'temporary', _page_limit(self.memory, self.options))
        if not matches:
            return 'System: No new matching temporary-memory pages. Already returned pages are omitted until context compression.'

        formatted = []
        for match in matches:
            header = f"--- [Page: {match['page_num']} | Source: {match['source']}] ---"
            formatted.append(f"{header}\n{match['text']}")
        return f"System: Found {len(matches)} matching temporary-memory page(s):\n\n" + "\n\n".join(formatted)



class ListKnowledgeBasesTool(BaseTool):
    """Return the live KB catalog visible in the current execution context."""

    def __init__(self, memory: MemoryManager):
        super().__init__(
            name="list_knowledge_bases",
            description=(
                "Returns the CURRENT catalog of knowledge bases available to this run, including each "
                "database ID, human-readable name, manifest, owner, and available storage formats. "
                "The knowledge-base list embedded in the session-start system prompt is only an initial "
                "snapshot and can become stale if modules or databases change during the session. Use this "
                "tool when you need the current list or are unsure which database should be searched."
            ),
            args_schema={"type": "object", "properties": {}},
        )
        self.memory = memory

    async def execute(self, **kwargs) -> str:
        databases = self.memory.list_knowledge_bases()
        if not databases:
            return "System: No knowledge bases are currently available to this run."

        blocks = []
        for database in databases:
            formats = ", ".join(database.get("formats") or []) or "unknown"
            blocks.append(
                "\n".join(
                    [
                        f"ID: {database.get('id', '')}",
                        f"Name: {database.get('name', '')}",
                        f"Manifest: {database.get('manifest', '')}",
                        f"Owner: {database.get('owner', '')}",
                        f"Formats: {formats}",
                    ]
                )
            )
        return "System: Current knowledge bases:\n\n" + "\n\n".join(blocks)


class SearchKnowledgeBaseTool(BaseTool):
    """Search one explicitly selected pool of independent knowledge bases."""

    def __init__(self, memory: MemoryManager, client: LlmClient):
        super().__init__(
            name="search_knowledge_base",
            description=(
                "Searches one or more explicitly selected knowledge bases. 'databases' is a list of exact "
                "database IDs from the session-start catalog or list_knowledge_bases. Select only databases "
                "relevant to the current information need. ALWAYS provide both representations of the same "
                "search intent: 'semantic_query' for vector databases and 'keywords' for paginated text "
                "databases. The backend chooses the appropriate stored representation for each selected "
                "database. This tool never searches session temporary memory."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "databases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "One or more exact knowledge-base IDs. Use list_knowledge_bases if the current "
                            "catalog or the appropriate database is unclear."
                        ),
                    },
                    "semantic_query": {
                        "type": "string",
                        "description": (
                            "Natural-language semantic formulation of what to find. Required on every call, "
                            "including when all selected databases are text-only."
                        ),
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Precise keywords or key phrases for paginated text search. Required on every call, "
                            "including when all selected databases are vector-only."
                        ),
                    },
                },
                "required": ["databases", "semantic_query", "keywords"],
            },
        )
        self.memory = memory
        self.client = client
        self.options = load_settings()

    def for_execution(self, context):
        tool = copy.copy(self)
        tool.options = load_settings()
        tool.description += ' Previously returned text pages are omitted until context compression; vector search is unchanged.'
        return tool

    async def execute(
        self,
        databases: List[str],
        semantic_query: str,
        keywords: List[str],
        **kwargs,
    ) -> str:
        clean_ids = list(dict.fromkeys(
            str(database_id).strip()
            for database_id in (databases or [])
            if str(database_id).strip()
        ))
        if not clean_ids:
            return "System: 'databases' must contain at least one knowledge-base ID."

        plan, missing = self.memory.knowledge_search_plan(clean_ids)
        if missing:
            return (
                "System: These knowledge bases are not available to the current run: "
                + ", ".join(missing)
                + ". Use 'list_knowledge_bases' to get the current catalog."
            )

        text_entries = list(plan.get("text") or [])
        vector_entries = list(plan.get("vector") or [])
        if not text_entries and not vector_entries:
            return "System: The selected knowledge bases contain no searchable text or vector representation."

        sections: List[str] = []

        if vector_entries:
            query = str(semantic_query or "").strip()
            if not query:
                return "System: 'semantic_query' must not be empty when a selected database uses vector search."
            vector_ids = [str(entry.get("id") or "") for entry in vector_entries]
            try:
                session = self.client.embedding_snapshot()
                await run_data_task(self.memory.validate_knowledge_embedding, vector_ids, session.identity)
                query_vector = await session.embed(query, purpose="query", task=DOCUMENT_QUERY_TASK)
                matches = await run_data_task(self.memory.search_knowledge_vector,
                    vector_ids,
                    query_vector,
                    limit=5,
                    embedding_model=session.identity,
                    embedding_dimension=session.dimension,
                )
                if matches:
                    formatted = []
                    for match in matches:
                        header = (
                            f"--- [Database: {match['database_id']} | Chunk: {match['chunk_id']} | "
                            f"Source: {match['source']} | Relevance: {match['score']:.2f}] ---"
                        )
                        formatted.append(f"{header}\n{match['text']}")
                    sections.append("Vector matches:\n\n" + "\n\n".join(formatted))
            except EmbeddingError:
                raise
            except Exception as error:
                return str(error)

        if text_entries:
            clean_keywords = [
                str(item).strip() for item in (keywords or []) if str(item).strip()
            ]
            if not clean_keywords:
                return "System: 'keywords' must contain at least one keyword when a selected database uses text search."
            text_ids = [str(entry.get("id") or "") for entry in text_entries]
            matches = await run_data_task(self.memory.search_knowledge_text,
                text_ids,
                clean_keywords,
                max_pages=None,
            )
            had_matches = bool(matches)
            matches = await new_pages(matches, 'knowledge', _page_limit(self.memory, self.options))
            if matches:
                formatted = []
                for match in matches:
                    header = (
                        f"--- [Database: {match['database_id']} | Page: {match['page_num']} | "
                        f"Source: {match['source']}] ---"
                    )
                    formatted.append(f"{header}\n{match['text']}")
                sections.append("Text matches:\n\n" + "\n\n".join(formatted))
            elif had_matches:
                sections.append('Text search: no new matching pages. Already returned pages are omitted until context compression.')

        if not sections:
            return (
                "System: No matches found in selected knowledge bases: "
                + ", ".join(clean_ids)
            )
        return "System: Knowledge-base search results:\n\n" + "\n\n".join(sections)
