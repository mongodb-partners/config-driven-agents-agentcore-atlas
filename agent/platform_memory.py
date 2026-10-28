"""The `engine` long-term memory backend — Atlas Agent Engine memory via its SDK.

This is the alternative to the built-in Atlas backend in `memory.py`. An agent
opts in with `memory.backend: engine` in its `.agent.md`; nothing here runs for
an `atlas` agent.

It wraps `agent_engine_sdk_memory.Memory` behind the same small seam the Atlas
backend exposes (`recall` / `persist` / `short_term`), so `main.py` stays
backend-agnostic and the UI's Memory tab keeps rendering the same trace events.

Two guarantees this module keeps:

- **The framework writes; agents read.** Exactly as the Atlas path: writes here
  are called from framework code after the answer streams, never exposed as a
  tool the model can call.
- **A memory outage never breaks a turn.** Every SDK call is wrapped so a
  transport or usage error degrades to "no recall / no persist this turn" with a
  reason the caller can trace, rather than raising into the answer path.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx

# The SDK is an optional dependency in spirit: only `engine` agents need it, and
# the import is deferred so an `atlas`-only deployment that never installed it
# still imports this module cleanly. `_require_sdk()` raises a clear message.
#
# Only `MemoryIdentityError` is a doc-confirmed exception name. The SDK has a
# typed transport-error family in errors.py but the docs do not enumerate the
# class names — so resolve them with getattr and fall back to Exception. A name
# the SDK renamed then degrades to the generic guards below instead of raising
# ImportError here.
try:  # pragma: no cover - import wiring, exercised indirectly
    import agent_engine_sdk_memory as _sdk
    from agent_engine_sdk_memory import Memory, MemoryRequestContext

    MemoryAuthError = getattr(_sdk, "MemoryAuthError", Exception)
    MemoryAPIError = getattr(_sdk, "MemoryAPIError", Exception)
    MemoryClientError = getattr(_sdk, "MemoryClientError", Exception)
    MemoryIdentityError = getattr(_sdk, "MemoryIdentityError", Exception)
    _SDK_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 - surface at use, not at import
    Memory = None  # type: ignore[assignment]
    MemoryRequestContext = None  # type: ignore[assignment]
    MemoryAPIError = MemoryClientError = MemoryIdentityError = Exception  # type: ignore[misc,assignment]
    MemoryAuthError = Exception  # type: ignore[misc,assignment]
    _SDK_IMPORT_ERROR = exc


# The build_context construction budget. The SDK reserves 500 tokens for
# formatting, so this must comfortably exceed 500 to leave room for memories.
_CONTEXT_MAX_TOKENS = int(os.environ.get("AGENTIC_MEMORY_MAX_TOKENS", "2048"))

# Where the OAuth client_credentials exchange happens. The same endpoint the
# image build uses to reach the private package registry — one platform, one
# client, two uses.
_TOKEN_URL = "https://agentengine.mongodb.com/api/v1/oauth/token"

# Re-mint this many seconds before the token actually expires, so a turn never
# starts with a credential that dies mid-flight.
_TOKEN_SKEW_SECONDS = 60

# Assumed lifetime when the token response omits `expires_in`. Deliberately
# short: guessing too low costs an extra token request, guessing too high costs
# a window of failing turns.
_TOKEN_TTL_FALLBACK_SECONDS = 300

# Long-term recall pulls from the durable sources; short-term (the current
# session buffer) is handled separately via the stm source.
_RECALL_SOURCES = {"episodic", "semantic"}
_STM_SOURCES = {"stm"}


@dataclass
class RecallResult:
    """What a recall produced, plus enough to trace it.

    `facts` matches the shape `main.py`'s `_facts_block` consumes: a list of
    `{"text": ...}` dicts. `formatted` is the SDK's ready-to-inject context
    string when present. `error` is set instead when recall degraded.
    """

    facts: list[dict] = field(default_factory=list)
    formatted: str = ""
    error: str = ""


@dataclass
class PersistResult:
    """What a persist wrote, mirroring the atlas backend's trace surface.

    `messages` is how many turn halves were recorded; the engine service does
    its own durable extraction server-side, so there is no local extraction
    token usage to report (unlike the atlas backend's second Bedrock call).
    """

    messages: int = 0
    error: str = ""


class AgentEngineBackend:
    """The `engine` long-term backend over `agent_engine_sdk_memory.Memory`.

    One `Memory` instance per process; `bind(...)` per turn scopes it to the
    (user, session) identity. Thread-safe across users for that reason.
    """

    name = "engine"

    def __init__(self) -> None:
        self._memory = None  # lazily constructed on first use
        # When the current client's bearer token stops being usable. Monotonic,
        # so a clock adjustment cannot push it into the far future.
        self._token_expires_at = 0.0

    # ── construction ─────────────────────────────────────────────────────────

    def _require_sdk(self):
        if Memory is None:
            raise RuntimeError(
                "memory.backend 'engine' needs the agent-engine-sdk-memory package, "
                f"which failed to import: {_SDK_IMPORT_ERROR}. Add it to "
                "agent/requirements.txt, or switch the agent to memory.backend: atlas."
            )

    def _mint_token(self) -> str:
        """Exchange the service-account credentials for an access token.

        The SDK sends whatever it is handed as `service_account_token` verbatim,
        as `Authorization: Bearer <value>`. So what it needs is an access token —
        handing it the raw client id is what produces AUTH_FAILED.
        """
        client_id = os.environ.get("AGENT_ENGINE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("AGENT_ENGINE_CLIENT_SECRET", "").strip()
        try:
            resp = httpx.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - one directed message, not a stack
            raise RuntimeError(
                f"Atlas Agent Engine token request to {_TOKEN_URL} failed: "
                f"{type(exc).__name__}: {exc}. Check agent_engine_client_id and "
                "agent_engine_client_secret."
            ) from exc

        token = (payload.get("access_token") or "").strip()
        if not token:
            raise RuntimeError(
                f"Agent Engine token response from {_TOKEN_URL} carried no access_token."
            )

        try:
            ttl = int(payload.get("expires_in", _TOKEN_TTL_FALLBACK_SECONDS))
        except (TypeError, ValueError):
            ttl = _TOKEN_TTL_FALLBACK_SECONDS
        # Never let skew produce an already-expired token on a very short TTL.
        self._token_expires_at = time.monotonic() + max(ttl - _TOKEN_SKEW_SECONDS, 30)
        return token

    def _invalidate(self) -> None:
        """Drop the cached client so the next call mints a fresh token.

        The bearer token is baked into the client's headers at construction, so
        rotating it means rebuilding the client — there is nothing to mutate.
        """
        self._memory = None
        self._token_expires_at = 0.0

    def _client(self):
        """The shared Memory client, rebuilt whenever its bearer token rotates.

        Fails fast with a directed message when no credential is available,
        rather than surfacing an opaque SDK ValueError mid-turn.
        """
        if self._memory is not None and time.monotonic() < self._token_expires_at:
            return self._memory

        self._require_sdk()
        project_id = os.environ.get("AGENTIC_MEMORY_PROJECT_ID", "").strip()
        base_url = os.environ.get("AGENTIC_MEMORY_BASE_URL", "").strip()

        # Three ways to authenticate, most specific first:
        #   1. a pre-minted access token, handed over directly
        #   2. service-account credentials, exchanged here for one — the hosted path
        #   3. nothing, for a self-hosted backend that does its own auth
        token = os.environ.get("AGENTIC_MEMORY_API_KEY", "").strip()
        if token:
            self._token_expires_at = float("inf")  # not ours to expire
        elif os.environ.get("AGENT_ENGINE_CLIENT_ID", "").strip():
            token = self._mint_token()
        elif base_url:
            self._token_expires_at = float("inf")  # unauthenticated self-hosted
        else:
            raise RuntimeError(
                "memory.backend 'engine' needs AGENT_ENGINE_CLIENT_ID and "
                "AGENT_ENGINE_CLIENT_SECRET (+ AGENTIC_MEMORY_PROJECT_ID for the hosted "
                "service), a pre-minted AGENTIC_MEMORY_API_KEY, or "
                "AGENTIC_MEMORY_BASE_URL for a self-hosted backend. None are set. "
                "Terraform fills the first two from the agent_engine_client_id and "
                "agent_engine_client_secret variables."
            )

        self._memory = Memory(
            service_account_token=token or None,
            project_id=project_id or None,
            base_url=base_url or None,
        )
        return self._memory

    def _session(self, user_id: str, session_id: str, agent_id: str = ""):
        ctx = MemoryRequestContext(
            user_id=user_id, session_id=session_id, agent_id=agent_id or None
        )
        return self._client().bind(ctx)

    # ── recall ───────────────────────────────────────────────────────────────

    def recall(self, user_id: str, session_id: str, query: str) -> RecallResult:
        """Durable context for this user, mapped into the atlas backend's shape.

        Pulls episodic + semantic memory via build_context and returns both the
        formatted string (ready to inject) and a `{"text": ...}` fact list so the
        existing prompt-assembly path is untouched.
        """
        try:
            session = self._session(user_id, session_id)
            context = session.build_context(
                query=query,
                enabled_sources=_RECALL_SOURCES,
                max_tokens=_CONTEXT_MAX_TOKENS,
            )
        except MemoryAuthError as exc:
            # Revoked, or expired earlier than `expires_in` promised. Drop the
            # token so the next turn mints a new one instead of replaying a dead
            # credential for the rest of this runtime's life.
            self._invalidate()
            return RecallResult(error=f"{type(exc).__name__}: {exc}")
        except (MemoryClientError, MemoryIdentityError, MemoryAPIError) as exc:
            return RecallResult(error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - degrade, never break the turn
            return RecallResult(error=f"{type(exc).__name__}: {exc}")

        # Inside its own guard, not the caller's. The module promises a memory
        # problem never breaks a turn, and a response whose shape surprises the
        # mappers is exactly such a problem — it used to raise straight through
        # into the answer path.
        try:
            return RecallResult(
                facts=_facts_from_context(context),
                formatted=_text_from_formatted(
                    getattr(context, "formatted_context", "")
                ),
            )
        except Exception as exc:  # noqa: BLE001 - degrade, never break the turn
            return RecallResult(error=f"unreadable context: {type(exc).__name__}: {exc}")

    # ── short-term ───────────────────────────────────────────────────────────

    def short_term(self, user_id: str, session_id: str) -> list[dict]:
        """The current session's turns, via the stm source.

        Returns the same `{"role", "content"}` shape `main.py`'s `_history_block`
        expects. Degrades to empty on any error.
        """
        try:
            session = self._session(user_id, session_id)
            context = session.build_context(
                query="",
                enabled_sources=_STM_SOURCES,
                max_tokens=_CONTEXT_MAX_TOKENS,
            )
        except Exception:  # noqa: BLE001 - short-term recall is best-effort
            return []
        try:
            return _turns_from_context(context)
        except Exception:  # noqa: BLE001 - an odd shape is not worth a dead turn
            return []

    # ── persist ──────────────────────────────────────────────────────────────

    def persist(self, user_id: str, session_id: str, agent_id: str,
                user_text: str, assistant_text: str) -> PersistResult:
        """Record both halves of the turn. The service extracts durable memory.

        Unlike the atlas backend, there is no local LLM extraction call — the
        managed service consolidates episodic/semantic memory server-side, so
        this is cheaper per turn.
        """
        try:
            session = self._session(user_id, session_id, agent_id)
            written = 0
            # record_turn takes only role/content — the bound context already
            # carries the session, so no session_id kwarg.
            if user_text.strip():
                session.record_turn(role="user", content=user_text)
                written += 1
            if assistant_text.strip():
                session.record_turn(role="assistant", content=assistant_text)
                written += 1
        except MemoryAuthError as exc:
            self._invalidate()
            return PersistResult(error=f"{type(exc).__name__}: {exc}")
        except (MemoryClientError, MemoryIdentityError, MemoryAPIError) as exc:
            return PersistResult(error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - degrade, never break the turn
            return PersistResult(error=f"{type(exc).__name__}: {exc}")
        return PersistResult(messages=written)


# ── mapping SDK responses into the shapes main.py already renders ─────────────

def _text_from_formatted(formatted) -> str:
    """Flatten `ContextResponse.formatted_context` to a string.

    The SDK types this field as `str | list[dict[str, Any]]` and the hosted
    gateway returns the list form, so assuming `str` raised
    `AttributeError: 'list' object has no attribute 'strip'` on every recall.

    The dict keys are only `dict[str, Any]` in the model, so read the plausible
    text keys in order and fall back to stringifying rather than dropping a
    memory we cannot name.
    """
    if formatted is None:
        return ""
    if isinstance(formatted, str):
        return formatted.strip()
    if isinstance(formatted, list):
        parts = []
        for item in formatted:
            text = _text_from_item(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    return str(formatted).strip()


def _text_from_item(item) -> str:
    """The text inside one formatted-context entry, whatever it is keyed as."""
    if isinstance(item, dict):
        for key in ("content", "text", "value", "formatted_context"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # A dict we cannot name is still recall — keep it readable rather than
        # silently returning nothing.
        return str(item).strip()
    value = getattr(item, "content", None) or getattr(item, "text", None)
    if isinstance(value, str):
        return value.strip()
    return str(item).strip() if item else ""


def _chunks_of(context) -> list:
    """The memory chunks on a ContextResponse.

    `selected_memories` is the real field name. `build_context` has no
    `include_memories` parameter, so it is normally empty and
    `formatted_context` carries everything — this stays only for the call paths
    that do populate it.
    """
    for attr in ("selected_memories", "chunks", "memories"):
        chunks = getattr(context, attr, None)
        if chunks:
            return list(chunks)
    return []


def _facts_from_context(context) -> list[dict]:
    """Best-effort `{"text": ...}` list from a ContextResponse.

    The prompt-assembly path wants text lines. Prefer per-chunk text, and fall
    back to the formatted block — as one entry per list item when the gateway
    returned the list form, so `main.py` renders separate facts rather than one
    wall of text.
    """
    facts: list[dict] = []
    for chunk in _chunks_of(context):
        # MemoryChunk calls it `content`; dict responses vary.
        text = _text_from_item(chunk)
        if text:
            facts.append({"text": text})
    if facts:
        return facts

    formatted = getattr(context, "formatted_context", None)
    if isinstance(formatted, list):
        return [{"text": text} for text in
                (_text_from_item(i) for i in formatted) if text]
    text = _text_from_formatted(formatted)
    return [{"text": text}] if text else []


def _turns_from_context(context) -> list[dict]:
    """Best-effort `{"role", "content"}` turns from an stm ContextResponse."""
    turns: list[dict] = []

    def add(role, item):
        text = _text_from_item(item)
        if text:
            turns.append({"role": role or "user", "content": text})

    for chunk in _chunks_of(context):
        role = chunk.get("role") if isinstance(chunk, dict) else getattr(chunk, "role", None)
        add(role, chunk)
    if turns:
        return turns

    # The stm source commonly comes back only as formatted_context; its list
    # form is already turn-shaped, so keep the roles when they are there.
    formatted = getattr(context, "formatted_context", None)
    if isinstance(formatted, list):
        for item in formatted:
            role = item.get("role") if isinstance(item, dict) else None
            add(role, item)
    return turns
