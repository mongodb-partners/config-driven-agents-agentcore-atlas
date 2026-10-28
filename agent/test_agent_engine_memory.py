#!/usr/bin/env python3
"""Self-check for the `engine` long-term memory backend.

Covers the config seam (`memory.backend`) and the `AgentEngineBackend` adapter,
without a live memory server: a fake stands in for `agent_engine_sdk_memory.Memory`
and records the calls the backend makes, so we verify it drives `record_turn` /
`build_context` with the shapes `main.py` depends on — and that a failure degrades
rather than raising into the turn.

    python agent/test_agent_engine_memory.py
"""

import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("CONFIG_DIR", str(REPO / "config"))
sys.path.insert(0, str(REPO / "agent"))

# memory.py imports mongo_mcp (and, transitively, the mcp client and boto3-backed
# embeddings) at module scope — none of which are involved in the backend seam.
# Stub them so `import memory` works off a laptop without the deploy-time deps,
# matching the pattern in test_tool_trace.py.
for name, attrs in {
    "mcp": {}, "mcp.client": {}, "mcp.client.streamable_http": {},
    "mongo_mcp": {
        "REGION": "us-east-1",
        "vector_search": lambda *a, **k: [],
        "insert": lambda *a, **k: None,
        "call": lambda *a, **k: None,
    },
    "embeddings": {"REGION": "us-east-1", "index_fields": lambda texts: [{} for _ in texts]},
}.items():
    mod = sys.modules.setdefault(name, types.ModuleType(name))
    for k, v in attrs.items():
        setattr(mod, k, v)

import agent_config  # noqa: E402
import platform_memory  # noqa: E402


# ── Fakes standing in for the SDK ─────────────────────────────────────────────

class FakeContext:
    """A stand-in ContextResponse, matching the real model's field names.

    The SDK calls the chunk list `selected_memories` and types
    `formatted_context` as `str | list[dict[str, Any]]`. An earlier fake used
    `.chunks` and a plain `str`, so it happily passed while production raised
    `AttributeError: 'list' object has no attribute 'strip'` on the list form.
    """

    def __init__(self, selected_memories=None, formatted=""):
        self.selected_memories = selected_memories or []
        self.formatted_context = formatted


class FakeSession:
    """What bind() returns — records the calls the backend makes on it."""

    def __init__(self, parent, ctx):
        self.parent = parent
        self.ctx = ctx

    def build_context(self, **kwargs):
        self.parent.calls.append(("build_context", kwargs))
        if self.parent.raise_on_recall:
            raise self.parent.raise_on_recall
        return self.parent.context

    def record_turn(self, **kwargs):
        self.parent.calls.append(("record_turn", kwargs))
        if self.parent.raise_on_record:
            raise self.parent.raise_on_record
        return {"ok": True}


class FakeMemory:
    """Stands in for agent_engine_sdk_memory.Memory."""

    def __init__(self, *, service_account_token=None, project_id=None, base_url=None):
        self.service_account_token = service_account_token
        self.project_id = project_id
        self.base_url = base_url
        self.calls = []
        self.context = FakeContext([])
        self.raise_on_recall = None
        self.raise_on_record = None

    def bind(self, ctx):
        self.calls.append(("bind", ctx))
        return FakeSession(self, ctx)


class _FakeAuthError(Exception):
    """Stands in for MemoryAuthError."""


_RealAuthError = platform_memory.MemoryAuthError


class FakeCtx:
    """Stands in for MemoryRequestContext — captures identity fields."""

    def __init__(self, *, user_id=None, session_id=None, agent_id=None):
        self.user_id = user_id
        self.session_id = session_id
        self.agent_id = agent_id


def _install_fake(**mem_kwargs):
    """Swap the SDK symbols for fakes and hand back a backend bound to one.

    Returns (backend, fake_memory). The backend constructs the fake lazily on
    first use, so we pre-seed env credentials to get past the guard.
    """
    os.environ["AGENTIC_MEMORY_API_KEY"] = "test-key"
    os.environ["AGENTIC_MEMORY_PROJECT_ID"] = "test-project"

    captured = {}

    def factory(*, service_account_token=None, project_id=None, base_url=None):
        mem = FakeMemory(service_account_token=service_account_token,
                         project_id=project_id, base_url=base_url)
        for k, v in mem_kwargs.items():
            setattr(mem, k, v)
        captured["mem"] = mem
        return mem

    platform_memory.Memory = factory
    platform_memory.MemoryRequestContext = FakeCtx
    platform_memory._SDK_IMPORT_ERROR = None

    backend = platform_memory.AgentEngineBackend()
    return backend, captured


# ── Config seam ───────────────────────────────────────────────────────────────

def test_backend_defaults_to_atlas():
    """An agent that omits `backend` must default to atlas.

    Agents that declare one are opting out deliberately and say nothing about
    the default — asserting over *every* agent would make adopting `engine`
    anywhere a test failure.
    """
    for path in (REPO / "config/agents").glob("*.agent.md"):
        # ponytail: a raw regex, not a YAML parse — `backend:` appears nowhere in
        # these files but under `memory:`.
        if re.search(r"^\s*backend:", path.read_text(), re.M):
            continue
        a = agent_config.load_agent(path.stem.replace(".agent", ""))
        assert a.long_term_backend == "atlas", f"{a.id}: {a.long_term_backend}"


def test_backend_engine_parses():
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "config"
        shutil.copytree(REPO / "config", staged)
        (staged / "agents" / "mem-demo.agent.md").write_text(
            "---\n"
            "id: mem-demo\n"
            "name: Mem Demo\n"
            "description: Demonstrates the engine memory backend.\n"
            "role: specialist\n"
            "model: us.anthropic.claude-haiku-4-5-20251001-v1:0\n"
            "maxTokens: 2048\n"
            "skills: []\n"
            "collections:\n  - compensation_data\n"
            "memory:\n  shortTerm: true\n  longTerm: true\n  backend: engine\n"
            "---\n\n# Mem Demo\n\nYou demo memory.\n"
        )
        original = agent_config.CONFIG_DIR
        try:
            agent_config.CONFIG_DIR = staged
            a = agent_config.load_agent("mem-demo")
            assert a.long_term_backend == "engine", a.long_term_backend
        finally:
            agent_config.CONFIG_DIR = original


def test_unknown_backend_is_rejected_at_load():
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "config"
        shutil.copytree(REPO / "config", staged)
        (staged / "agents" / "bad-mem.agent.md").write_text(
            "---\n"
            "id: bad-mem\n"
            "name: Bad Mem\n"
            "description: Bad backend value.\n"
            "role: specialist\n"
            "model: us.anthropic.claude-haiku-4-5-20251001-v1:0\n"
            "maxTokens: 2048\n"
            "skills: []\n"
            "collections:\n  - compensation_data\n"
            "memory:\n  longTerm: true\n  backend: redis\n"
            "---\n\n# Bad Mem\n\nnope\n"
        )
        original = agent_config.CONFIG_DIR
        try:
            agent_config.CONFIG_DIR = staged
            try:
                agent_config.load_agent("bad-mem")
            except ValueError:
                return
            raise AssertionError("an unknown memory.backend was not rejected")
        finally:
            agent_config.CONFIG_DIR = original


def test_dispatcher_returns_the_right_backend():
    import memory

    class D:
        long_term_backend = "atlas"

    assert isinstance(memory.get_backend(D()), memory.AtlasBackend)

    _install_fake()

    class M:
        long_term_backend = "engine"

    assert isinstance(memory.get_backend(M()), platform_memory.AgentEngineBackend)


# ── AgentEngineBackend behaviour ──────────────────────────────────────────────

def test_recall_maps_chunks_to_facts():
    backend, cap = _install_fake(
        context=FakeContext(
            # MemoryChunk exposes `content`; dict entries vary in the wild.
            [type("C", (), {"content": "Prefers remote work."})(),
             {"text": "Has 8 years experience."}],
            formatted="formatted block",
        )
    )
    result = backend.recall("user_1", "sess_" + "x" * 30, "what do you know")

    assert not result.error, result.error
    texts = [f["text"] for f in result.facts]
    assert "Prefers remote work." in texts
    assert "Has 8 years experience." in texts
    assert result.formatted == "formatted block"

    # It bound identity and asked for the durable sources.
    kinds = [c[0] for c in cap["mem"].calls]
    assert "bind" in kinds and "build_context" in kinds
    bc = next(kw for name, kw in cap["mem"].calls if name == "build_context")
    assert bc["enabled_sources"] == {"episodic", "semantic"}
    assert bc["max_tokens"] > 500  # must clear the SDK's formatting reserve


def test_recall_degrades_on_error():
    backend, _ = _install_fake(raise_on_recall=RuntimeError("backend down"))
    result = backend.recall("user_1", "sess_" + "x" * 30, "q")
    assert result.facts == []
    assert "backend down" in result.error


def test_recall_handles_formatted_context_as_a_list():
    """The regression: the hosted gateway returns the list form, not a string.

    `formatted_context: str | list[dict[str, Any]]` — assuming `str` raised
    AttributeError out of recall and killed the whole turn.
    """
    backend, _ = _install_fake(
        context=FakeContext(formatted=[
            {"role": "user", "content": "I want to move into platform work."},
            {"role": "assistant", "content": "Noted — platform engineering."},
        ])
    )
    result = backend.recall("user_1", "sess_" + "x" * 30, "what do you know")

    assert not result.error, result.error
    texts = [f["text"] for f in result.facts]
    assert "I want to move into platform work." in texts
    assert "Noted — platform engineering." in texts
    # The injected block must be a string, whatever the wire shape was.
    assert isinstance(result.formatted, str)
    assert "platform work" in result.formatted


def test_recall_still_handles_formatted_context_as_a_string():
    backend, _ = _install_fake(context=FakeContext(formatted="  a plain block  "))
    result = backend.recall("user_1", "sess_" + "x" * 30, "q")
    assert not result.error, result.error
    assert result.formatted == "a plain block"
    assert [f["text"] for f in result.facts] == ["a plain block"]


def test_recall_degrades_instead_of_raising_on_an_unreadable_shape():
    """A shape the mappers cannot read must not break the turn."""
    class Hostile:
        @property
        def formatted_context(self):
            raise TypeError("surprise")

    backend, _ = _install_fake(context=Hostile())
    result = backend.recall("user_1", "sess_" + "x" * 30, "q")
    assert result.facts == []
    assert "surprise" in result.error


def test_short_term_reads_turns_from_the_list_form():
    backend, _ = _install_fake(
        context=FakeContext(formatted=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
    )
    turns = backend.short_term("user_1", "sess_" + "x" * 30)
    assert turns == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ], turns


def test_persist_records_both_turns():
    backend, cap = _install_fake()
    result = backend.persist(
        "user_1", "sess_" + "x" * 30, "career-coach", "hi there", "hello back"
    )
    assert not result.error, result.error
    assert result.messages == 2

    records = [kw for name, kw in cap["mem"].calls if name == "record_turn"]
    assert [r["role"] for r in records] == ["user", "assistant"]
    assert records[0]["content"] == "hi there"
    assert records[1]["content"] == "hello back"


def test_persist_skips_empty_halves():
    backend, cap = _install_fake()
    result = backend.persist("user_1", "sess_" + "x" * 30, "a", "only user", "   ")
    assert result.messages == 1
    records = [kw for name, kw in cap["mem"].calls if name == "record_turn"]
    assert len(records) == 1 and records[0]["role"] == "user"


def test_persist_degrades_on_error():
    backend, _ = _install_fake(raise_on_record=RuntimeError("write failed"))
    result = backend.persist("user_1", "sess_" + "x" * 30, "a", "u", "a")
    assert "write failed" in result.error


def test_missing_credentials_fail_fast():
    # No creds, no base_url — construction must raise a directed error, not an
    # opaque SDK ValueError mid-turn.
    for var in ("AGENTIC_MEMORY_API_KEY", "AGENTIC_MEMORY_PROJECT_ID",
                "AGENTIC_MEMORY_BASE_URL", "AGENT_ENGINE_CLIENT_ID",
                "AGENT_ENGINE_CLIENT_SECRET"):
        os.environ.pop(var, None)
    platform_memory.Memory = FakeMemory
    platform_memory.MemoryRequestContext = FakeCtx
    backend = platform_memory.AgentEngineBackend()
    try:
        backend._client()
    except RuntimeError as exc:
        assert "AGENTIC_MEMORY" in str(exc)
        return
    raise AssertionError("missing credentials did not fail fast")


# ── Token minting ─────────────────────────────────────────────────────────────
#
# The bug these pin: the SDK sends `service_account_token` verbatim as
# `Authorization: Bearer`, so handing it the raw client id returns AUTH_FAILED.
# What must reach it is the access token from the client_credentials exchange.

def _install_oauth_fake(token_responses):
    """Backend wired to fake OAuth + fake SDK. Returns (backend, state)."""
    for var in ("AGENTIC_MEMORY_API_KEY", "AGENTIC_MEMORY_BASE_URL"):
        os.environ.pop(var, None)
    os.environ["AGENT_ENGINE_CLIENT_ID"] = "client-id-not-a-token"
    os.environ["AGENT_ENGINE_CLIENT_SECRET"] = "client-secret"
    os.environ["AGENTIC_MEMORY_PROJECT_ID"] = "test-project"

    state = {"posts": [], "keys": []}

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, data=None, timeout=None):
        state["posts"].append((url, data))
        return FakeResponse(token_responses[min(len(state["posts"]) - 1,
                                                len(token_responses) - 1)])

    def factory(*, service_account_token=None, project_id=None, base_url=None):
        state["keys"].append(service_account_token)
        mem = FakeMemory(service_account_token=service_account_token,
                         project_id=project_id, base_url=base_url)
        state["mem"] = mem
        return mem

    platform_memory.httpx = types.SimpleNamespace(post=fake_post)
    platform_memory.Memory = factory
    platform_memory.MemoryRequestContext = FakeCtx
    platform_memory._SDK_IMPORT_ERROR = None
    return platform_memory.AgentEngineBackend(), state


def test_sdk_receives_the_token_not_the_client_id():
    backend, state = _install_oauth_fake([
        {"access_token": "minted-token-1", "expires_in": 3600},
    ])
    backend._client()

    assert state["posts"], (
        "no client_credentials exchange happened — the SDK was handed a raw "
        "credential instead of a minted token"
    )
    url, data = state["posts"][0]
    assert url.endswith("/oauth/token"), url
    assert data["grant_type"] == "client_credentials", data
    assert data["client_id"] == "client-id-not-a-token"
    assert data["client_secret"] == "client-secret"
    # The regression that caused MemoryAuthError: AUTH_FAILED.
    assert state["keys"] == ["minted-token-1"], state["keys"]


def test_token_is_reused_until_it_nears_expiry():
    backend, state = _install_oauth_fake([
        {"access_token": "minted-token-1", "expires_in": 3600},
        {"access_token": "minted-token-2", "expires_in": 3600},
    ])
    backend._client()
    backend._client()
    assert len(state["posts"]) == 1, "a valid token was re-minted needlessly"

    # A token whose remaining life is inside the refresh skew must be replaced.
    backend._token_expires_at = 0.0
    backend._client()
    assert state["keys"] == ["minted-token-1", "minted-token-2"], state["keys"]


def test_auth_failure_drops_the_cached_token():
    backend, state = _install_oauth_fake([
        {"access_token": "minted-token-1", "expires_in": 3600},
        {"access_token": "minted-token-2", "expires_in": 3600},
    ])
    platform_memory.MemoryAuthError = _FakeAuthError
    try:
        backend._client()
        state["mem"].raise_on_recall = _FakeAuthError("AUTH_FAILED")
        result = backend.recall("user_1", "sess_" + "x" * 30, "q")
        assert "AUTH_FAILED" in result.error
        # Without this, a revoked token would be replayed for the runtime's life.
        assert backend._memory is None

        state["mem"].raise_on_recall = None
        backend._client()
        assert state["keys"][-1] == "minted-token-2", state["keys"]
    finally:
        platform_memory.MemoryAuthError = _RealAuthError


def test_short_ttl_never_yields_an_already_expired_token():
    backend, _ = _install_oauth_fake([{"access_token": "t", "expires_in": 5}])
    backend._client()
    # expires_in (5) is well inside the 60s skew; clamping must still leave the
    # token usable, or every single turn would re-mint.
    assert backend._token_expires_at > platform_memory.time.monotonic()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
