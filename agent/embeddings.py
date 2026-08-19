"""Text embeddings — one of three modes, chosen at deploy time.

`EMBEDDING_MODE` decides who does the embedding. Nothing above this module cares
which one is active: `index_fields()` says what to store, `query_clause()` says
how to search, and those two are the whole difference.

  atlas-voyage  (default) This process calls a Voyage model and writes the
                resulting vector into the document. The key decides the
                endpoint: an `al-…` key is MongoDB's Atlas Embedding and
                Reranking API, a `pa-…` key is Voyage AI's own.

  auto          MongoDB Atlas Automated Embedding. This process never embeds
                anything: documents carry plain text in `searchText`, the index
                is an `autoEmbed` index, and `$vectorSearch` takes the query as
                text. No API key anywhere in the stack.

  titan         Bedrock Titan v2. No account beyond AWS — the fallback for
                attendees with no Atlas/Voyage key at all.

Rate limits matter in the two modes that call an API. Voyage's free tier allows
single-digit requests per minute, and seeding embeds ~50 documents in a burst,
so: batch where the API allows it, retry with backoff where it does not. `auto`
sidesteps this entirely — Atlas does the embedding inside the cluster.
"""

from __future__ import annotations

import json
import os
import random
import time

import boto3
import httpx
from botocore.exceptions import ClientError

EMBEDDING_DIMS = 1024

# Where the vector lives in a document (atlas-voyage, titan) and where the text
# Atlas embeds for us lives (auto). Never both — an Atlas index cannot mix
# `vector` and `autoEmbed` fields, so the two are mutually exclusive by design.
VECTOR_FIELD = "embedding"
AUTO_FIELD = "searchText"

MODES = ("atlas-voyage", "auto", "titan")
MODE = os.environ.get("EMBEDDING_MODE", "atlas-voyage").strip() or "atlas-voyage"
if MODE not in MODES:
    raise ValueError(f"EMBEDDING_MODE={MODE!r} — must be one of {', '.join(MODES)}")

AUTO = MODE == "auto"

# AGENT_REGION is set by Terraform; AWS_REGION is what the runtime and local
# shells provide. Either is fine, but one of them must be there.
REGION = os.environ.get("AWS_REGION") or os.environ.get("AGENT_REGION") or "us-east-1"

_TITAN_MODEL = os.environ.get("TITAN_EMBED_MODEL", "amazon.titan-embed-text-v2:0")

# Voyage models are reachable through two different endpoints, and a key issued
# for one is rejected by the other with a 403.
#
#   al-…  MongoDB Atlas model API key  → Atlas Embedding and Reranking API
#   pa-…  native Voyage AI key         → api.voyageai.com
#
# Detected from the key prefix so attendees do not have to know which kind they
# were handed; VOYAGE_API_BASE overrides it if that ever guesses wrong.
_ATLAS_EMBED_URL = "https://ai.mongodb.com/v1/embeddings"
_VOYAGE_EMBED_URL = "https://api.voyageai.com/v1/embeddings"


def _voyage_url() -> str:
    override = os.environ.get("VOYAGE_API_BASE")
    if override:
        return override
    key = os.environ.get("VOYAGE_API_KEY", "")
    return _ATLAS_EMBED_URL if key.startswith("al-") else _VOYAGE_EMBED_URL
# voyage-4 is the current generation and the family Atlas Vector Search supports
# natively. voyage-3.5 / voyage-4-lite / voyage-4-large also work — all of them
# honour output_dimension=1024, so the Atlas indexes are unchanged either way.
#
# In `auto` mode this same name goes into the index definition, and Atlas accepts
# a narrower set there (voyage-4, voyage-4-large, voyage-4-lite, voyage-code-3).
# Terraform validates the pairing before anything is deployed.
VOYAGE_MODEL = os.environ.get("VOYAGE_EMBED_MODEL", "voyage-4")
AUTO_EMBED_MODELS = ("voyage-4", "voyage-4-large", "voyage-4-lite", "voyage-code-3")

# Voyage accepts up to 128 inputs per request, which turns the whole seed into a
# single call. Titan has no batch API, so it is embedded one at a time.
_VOYAGE_BATCH = 128

_MAX_ATTEMPTS = 6
# Total wall-clock budget for one embed call, retries included. Must stay well
# under the orchestrator's per-chunk stream timeout.
_RETRY_BUDGET_S = float(os.environ.get("EMBED_RETRY_BUDGET_S", "45"))
_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}
_RETRYABLE_AWS = {"ThrottlingException", "TooManyRequestsException",
                  "ServiceUnavailableException", "ModelTimeoutException",
                  "InternalServerException"}

_bedrock = None


def provider() -> str:
    return MODE


def index_fields(texts: list[str]) -> list[dict]:
    """The per-document fields the vector index needs, for the active mode.

    One call for a whole collection: `auto` needs no round trip at all, and the
    API modes get a single batched request instead of one per document. Callers
    merge the returned dict into their document and stay mode-agnostic.
    """
    if AUTO:
        return [{AUTO_FIELD: t} for t in texts]
    return [{VECTOR_FIELD: v} for v in embed_many(texts)]


def query_clause(query_text: str) -> dict:
    """The `$vectorSearch` fields that name what to compare against what.

    The mirror image of `index_fields`, and deliberately next to it: the path
    written at index time and the path queried at search time have to be the same
    field, and a mismatch produces zero results rather than an error.
    """
    if AUTO:
        return {"path": AUTO_FIELD, "query": {"text": query_text}}
    return {"path": VECTOR_FIELD, "queryVector": embed(query_text)}


def _retry(fn):
    """Call `fn`, backing off on rate limits. Honours Retry-After when given.

    Bounded by a wall-clock budget, not just an attempt count. Unbounded backoff
    is worse than failing: an agent stuck retrying for two minutes stalls the
    whole turn, and the orchestrator abandons the response stream long before the
    retry succeeds. Failing inside the budget produces a visible error instead of
    a truncated answer.
    """
    deadline = time.monotonic() + _RETRY_BUDGET_S
    delay = 2.0

    for attempt in range(_MAX_ATTEMPTS):
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in _RETRYABLE_HTTP:
                raise
            retry_after = exc.response.headers.get("retry-after")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            reason = f"HTTP {status}"
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in _RETRYABLE_AWS:
                raise
            wait, reason = delay, code
        except httpx.TransportError as exc:
            wait, reason = delay, type(exc).__name__

        # Jitter so parallel agents do not retry in lockstep.
        wait += random.uniform(0, 0.5)
        if attempt == _MAX_ATTEMPTS - 1 or time.monotonic() + wait > deadline:
            raise RuntimeError(
                f"{MODE} embeddings kept failing ({reason}) — gave up after "
                f"{_RETRY_BUDGET_S:.0f}s. If this is Voyage, the account's rate "
                f"limit is likely below what one agent turn needs; raise the "
                f"tier, or redeploy with embedding_mode = \"auto\" (Atlas embeds "
                f"in-cluster, no key, no rate limit) or \"titan\"."
            )
        time.sleep(wait)
        delay = min(delay * 2, 20.0)


def embed(text: str) -> list[float]:
    """Embed a single string. Returns a 1024-dim vector."""
    return embed_many([text])[0]


def embed_many(texts: list[str]) -> list[list[float]]:
    """Embed a list of strings, in provider-native batches where possible.

    Used by the seed script so the whole corpus costs one Voyage request instead
    of one per document — the difference between seeding in seconds and being
    rate-limited on the eighth document.
    """
    if AUTO:
        raise RuntimeError(
            "embed() called in auto mode — Atlas owns the embeddings here. "
            "Search with mongo_mcp.vector_search(query_text=…) instead."
        )

    cleaned = [(t or "").strip() for t in texts]
    if not cleaned:
        return []
    for i, t in enumerate(cleaned):
        if not t:
            raise ValueError(f"cannot embed empty text at index {i}")

    if MODE == "atlas-voyage":
        out: list[list[float]] = []
        for i in range(0, len(cleaned), _VOYAGE_BATCH):
            out.extend(_embed_voyage(cleaned[i:i + _VOYAGE_BATCH]))
        return out
    return [_embed_titan(t) for t in cleaned]


def _embed_titan(text: str) -> list[float]:
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    def once():
        resp = _bedrock.invoke_model(
            modelId=_TITAN_MODEL,
            body=json.dumps({"inputText": text, "dimensions": EMBEDDING_DIMS,
                             "normalize": True}),
        )
        return json.loads(resp["body"].read())["embedding"]

    return _retry(once)


def _embed_voyage(batch: list[str]) -> list[list[float]]:
    if not os.environ.get("VOYAGE_API_KEY"):
        raise RuntimeError(
            "EMBEDDING_MODE=atlas-voyage needs VOYAGE_API_KEY (an `al-…` Atlas "
            "model API key or a `pa-…` Voyage key). Set it, or pick another mode."
        )

    def once():
        resp = httpx.post(
            _voyage_url(),
            headers={"Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}"},
            json={"input": batch, "model": VOYAGE_MODEL,
                  "output_dimension": EMBEDDING_DIMS},
            timeout=60.0,
        )
        resp.raise_for_status()
        # The API preserves input order, but it returns an index — sort on it
        # rather than trusting position.
        data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    return _retry(once)
