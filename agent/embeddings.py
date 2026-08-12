"""Text embeddings — Bedrock Titan by default, Voyage AI if a key is present.

Attendees who already use Voyage models with Atlas can set VOYAGE_API_KEY and
get the same vectors they use elsewhere. Everyone else gets Titan with no extra
account to create. Both are 1024-dim, so the Atlas indexes are identical either
way and the seed script and the agents agree without a config flag.

Both providers rate-limit, and Voyage's free tier does so aggressively (single
digit requests per minute). So: batch where the API allows it, and retry with
backoff where it does not. Seeding embeds ~50 documents in a burst and will hit
a 429 on the first run otherwise.
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

# AGENT_REGION is set by Terraform; AWS_REGION is what the runtime and local
# shells provide. Either is fine, but one of them must be there.
REGION = os.environ.get("AWS_REGION") or os.environ.get("AGENT_REGION") or "us-east-1"

_PROVIDER = "voyage" if os.environ.get("VOYAGE_API_KEY") else "titan"
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
_VOYAGE_MODEL = os.environ.get("VOYAGE_EMBED_MODEL", "voyage-4")

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
    return _PROVIDER


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
                f"{_PROVIDER} embeddings kept failing ({reason}) — gave up after "
                f"{_RETRY_BUDGET_S:.0f}s. If this is Voyage, the account's rate "
                f"limit is likely below what one agent turn needs; use Bedrock "
                f"Titan (unset VOYAGE_API_KEY) or raise the tier."
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
    cleaned = [(t or "").strip() for t in texts]
    if not cleaned:
        return []
    for i, t in enumerate(cleaned):
        if not t:
            raise ValueError(f"cannot embed empty text at index {i}")

    if _PROVIDER == "voyage":
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
    def once():
        resp = httpx.post(
            _voyage_url(),
            headers={"Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}"},
            json={"input": batch, "model": _VOYAGE_MODEL,
                  "output_dimension": EMBEDDING_DIMS},
            timeout=60.0,
        )
        resp.raise_for_status()
        # The API preserves input order, but it returns an index — sort on it
        # rather than trusting position.
        data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    return _retry(once)
