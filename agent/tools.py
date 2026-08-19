"""The local tool surface, built from an agent's config.

Three tools that MCP cannot provide on its own:

- `vector_search`      — takes text; the vector is built outside the model's
                         context, or by Atlas itself in auto-embedding mode
- `recall_conversation`— the same, scoped to this candidate's own past turns
- `read_skill_resource`— pulls a reference doc off the image on demand

Everything else an agent can do comes from the MongoDB MCP server.
"""

from __future__ import annotations

import contextvars
import json

from strands import tool

import agent_config
import memory
import mongo_mcp

# Set per request in main.py; tools read the caller's identity from here rather
# than taking it as an argument the model could set.
ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("ctx", default={})

# Agents get wrappers, never the MCP tools themselves. Two reasons:
#   1. MongoDB MCP Server 2.x requires `connectionId` and `database` on every
#      call — infrastructure the model cannot know and would hallucinate.
#   2. Reads only, scoped to the agent's configured collections. The framework
#      owns every write (memory, traces), so an agent cannot corrupt its history.


def build_tools(agent: agent_config.AgentDef) -> list:
    """Assemble the tool list for one agent from its config block."""
    allowed = set(agent.collections)

    collection_list = ", ".join(sorted(allowed)) or "none configured"

    @tool(description=(
        "Semantically search a collection and return the closest documents. Use this "
        "whenever you need to find records by meaning rather than by an exact field "
        "value. Write query_text as a natural-language description of what you are "
        "looking for — it is embedded and compared against every document, so richer "
        "text gives better matches than keywords.\n\n"
        f"collection must be one of: {collection_list}. Anything else is rejected.\n"
        "limit is 1-20. filter is an optional JSON object of exact-match pre-filters "
        'applied before the search, e.g. {"seniority": "senior"} — only use fields the '
        "collection schema marks as filterable."
    ))
    def vector_search(collection: str, query_text: str, limit: int = 5,
                      filter: str | None = None) -> str:
        if collection not in allowed:
            return (f"Error: '{collection}' is not available to you. "
                    f"You may search: {', '.join(sorted(allowed)) or '(none)'}.")
        try:
            parsed = json.loads(filter) if filter else None
        except json.JSONDecodeError:
            return f"Error: filter is not valid JSON: {filter}"

        docs = mongo_mcp.vector_search(
            collection, query_text, limit=max(1, min(limit, 20)), filter=parsed,
        )
        if not docs:
            return f"No documents in '{collection}' matched. Do not invent one — say nothing was found."
        return json.dumps(docs, default=str)

    @tool
    def recall_conversation(query_text: str, limit: int = 5) -> str:
        """Search this candidate's earlier conversations for something relevant.

        Use when the candidate refers to something you cannot see in the current
        conversation ("the role we discussed", "like I said before"), or when
        knowing their history would materially change your answer. Searches by
        meaning across all their past sessions.

        Args:
            query_text: What to look for, in natural language.
            limit: How many past turns to return (1-10).
        """
        c = ctx.get()
        if not c.get("userId"):
            return "No candidate identity on this request; conversation recall is unavailable."
        turns = memory.recall_conversation(
            c["userId"], query_text, limit=max(1, min(limit, 10)),
            exclude_session=c.get("sessionId"),
        )
        if not turns:
            return "Nothing relevant found in earlier conversations."
        return json.dumps(
            [{"role": t.get("role"), "text": t.get("text"), "at": t.get("createdAt")} for t in turns],
            default=str,
        )

    @tool
    def read_skill_resource(resource: str) -> str:
        """Read one of your skill's reference documents.

        Currently available: `collections-schema.md` — the exact field names,
        types, and value enums for every collection you can query. Read it before
        writing a filter or citing a field you are unsure about.

        Args:
            resource: The filename, e.g. 'collections-schema.md'.
        """
        if not agent.skills:
            return "You have no skill references."
        try:
            return agent_config.read_skill_resource(agent.skills[0], resource)
        except (FileNotFoundError, ValueError) as exc:
            return f"Error: {exc}"

    @tool(description=(
        "Look up documents by exact field values. Use this when you already know "
        "an identifier — a candidateId, jobId, companyId — or need an exact match "
        "on a field. For anything descriptive, use vector_search instead.\n\n"
        f"collection must be one of: {collection_list}.\n"
        'filter is a JSON object of exact matches, e.g. {"candidateId": "cand-0001"}. '
        "limit is 1-20."
    ))
    def mongodb_find(collection: str, filter: str, limit: int = 5) -> str:
        if collection not in allowed:
            return (f"Error: '{collection}' is not available to you. "
                    f"You may query: {', '.join(sorted(allowed)) or '(none)'}.")
        try:
            parsed = json.loads(filter) if filter else {}
        except json.JSONDecodeError:
            return f"Error: filter is not valid JSON: {filter}"

        docs = mongo_mcp.find(collection, parsed, limit=max(1, min(limit, 20)))
        if not docs:
            return f"No documents in '{collection}' matched {filter}. Say nothing was found."
        return json.dumps(docs, default=str)

    tools = [vector_search, recall_conversation]
    if allowed:
        tools.append(mongodb_find)
    if agent.skills:
        tools.append(read_skill_resource)
    return tools
