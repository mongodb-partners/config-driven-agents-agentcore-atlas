"""The only door to MongoDB Atlas.

Every read and write in this process — the agent's own tool calls, the memory
layer, the trace writer — goes through the MongoDB MCP server running in its own
AgentCore Runtime. There is no pymongo in the agent image, by design: that is the
architectural point the workshop is teaching.

Calls are SigV4-signed against `bedrock-agentcore`, so the agent runtime's IAM
role *is* the authentication on this path. No Gateway, no bearer tokens, no
secrets to rotate.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
import uuid
from typing import Any

import boto3
import httpx
from botocore.auth import SigV4Auth as BotoSigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient

import embeddings

MCP_RUNTIME_ARN = os.environ["MCP_RUNTIME_ARN"]
REGION = embeddings.REGION
DB = os.environ.get("MONGODB_DB", "riv_workshop")

# MongoDB MCP Server 2.x is multi-connection: every tool takes a connectionId.
# A server started with MDB_MCP_CONNECTION_STRING exposes exactly one, named
# "preconfigured", dialled lazily on first use. `list-connections` on the running
# server confirms the name if this ever changes.
CONNECTION_ID = os.environ.get("MCP_CONNECTION_ID", "preconfigured")

# Headers botocore signs. Anything httpx adds later is unsigned and harmless;
# anything signed here must reach the server byte-identical.
_SIGNED = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version"}


class _SigV4(httpx.Auth):
    def __init__(self) -> None:
        self._session = boto3.Session()

    def auth_flow(self, request):
        creds = self._session.get_credentials().get_frozen_credentials()
        signable = {k: v for k, v in request.headers.items() if k.lower() in _SIGNED}
        aws_req = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=signable,
        )
        BotoSigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_req)
        for header in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token", "X-Amz-Content-SHA256"):
            if header in aws_req.headers:
                request.headers[header] = aws_req.headers[header]
        yield request


def _endpoint() -> str:
    arn = urllib.parse.quote(MCP_RUNTIME_ARN, safe="")
    return f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{arn}/invocations?qualifier=DEFAULT"


_client: MCPClient | None = None
_lock = threading.Lock()


def client() -> MCPClient:
    """Process-wide MCP client, started on first use."""
    global _client
    with _lock:
        if _client is None:
            # AgentCore requires a session id of 33+ chars on the runtime header.
            session_id = f"mcp-{uuid.uuid4().hex}"
            c = MCPClient(lambda: streamablehttp_client(
                _endpoint(),
                headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id},
                auth=_SigV4(),
                timeout=60.0,
            ))
            c.start()
            _client = c
        return _client


_JSON_START = re.compile(r"[\[{]")


def _extract_json(text: str) -> Any:
    """Pull the JSON payload out of an MCP text response.

    The MongoDB MCP server wraps results in a human-readable sentence ("Found 3
    documents in ..."), so we take the first balanced JSON value in the string
    rather than assuming the whole body parses.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _JSON_START.search(text)
    if not match:
        return None
    start = match.start()
    opener = text[start]
    closer = "]" if opener == "[" else "}"
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def call(tool: str, **args) -> Any:
    """Invoke an MCP tool programmatically and return its parsed payload.

    Used by the framework (memory, traces, vector search). Agents call MCP tools
    through Strands instead — same server, same IAM path.
    """
    args.setdefault("database", DB)
    args.setdefault("connectionId", CONNECTION_ID)
    result = client().call_tool_sync(f"call-{uuid.uuid4().hex[:8]}", tool, args)
    if result.get("status") == "error":
        raise RuntimeError(f"MCP tool '{tool}' failed: {result.get('content')}")

    payloads = [
        _extract_json(block["text"])
        for block in result.get("content", [])
        if "text" in block
    ]
    payloads = [p for p in payloads if p is not None]
    if not payloads:
        return []
    if len(payloads) == 1:
        return payloads[0]
    # Multi-block responses are one document per block.
    return payloads


def find(collection: str, filter: dict, limit: int = 10, projection: dict | None = None) -> list[dict]:
    docs = call(
        "find",
        collection=collection,
        filter=filter,
        limit=limit,
        projection=projection or {"embedding": 0},
    )
    return docs if isinstance(docs, list) else [docs]


def insert(collection: str, documents: list[dict]) -> None:
    if documents:
        call("insert-many", collection=collection, documents=documents)


def vector_search(
    collection: str,
    query_vector: list[float],
    limit: int = 5,
    filter: dict | None = None,
    num_candidates: int | None = None,
) -> list[dict]:
    """`$vectorSearch` through the MCP `aggregate` tool.

    The 1024-float vector is built here and passed straight to MCP, so it never
    passes through a model's context window.
    """
    stage: dict[str, Any] = {
        "index": f"{collection}_vector_index",
        "path": "embedding",
        "queryVector": query_vector,
        "numCandidates": num_candidates or max(limit * 15, 100),
        "limit": limit,
    }
    if filter:
        stage["filter"] = filter
    # $set + $unset rather than $project: a projection cannot mix a computed
    # field with exclusions.
    pipeline = [
        {"$vectorSearch": stage},
        {"$set": {"score": {"$meta": "vectorSearchScore"}}},
        {"$unset": ["embedding", "_id"]},
    ]
    docs = call("aggregate", collection=collection, pipeline=pipeline)
    return docs if isinstance(docs, list) else [docs]
