"""Static core instructions for the always-on Memory component."""

MEMORY_SYSTEM_PROMPT_TEXT = (
    "Permanent memory tools are always available: remember_this, forget_this, search_memory, list_memories.\n"
    "Proactively remember durable facts explicitly learned about the user: who they are, their work, "
    "goals, preferences, and the software, hardware or workflows they use. Do not wait for a request "
    "to remember useful facts. Save concise, accurate facts, avoid duplicates, and update outdated facts "
    "when corrected. Do not infer unsupported personal details or store transient task progress as a user fact.\n"
    "Temporary memory and knowledge bases are separate sources. search_temp_memory searches only the "
    "current session's temporary memory. Use search_knowledge_base for specialized knowledge bases. "
    "Use list_knowledge_bases for the current catalog or when unsure which database to choose. "
    "The database list at the end of the system prompt is a snapshot taken at session start; "
    "it does not update automatically.\n"
)
