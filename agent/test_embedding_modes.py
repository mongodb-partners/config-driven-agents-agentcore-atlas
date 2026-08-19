#!/usr/bin/env python3
"""Self-check for the three embedding modes.

The index is written by `seed.py` and queried by `mongo_mcp.py` — different
processes, different machines, one shared assumption: the field the vector (or
the text) lives in. If those two ever disagree, every search returns zero
documents and nothing raises. This is the check that fails instead.

    python agent/test_embedding_modes.py
"""

import importlib
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "agent"), str(REPO / "seed")]

import embeddings  # noqa: E402
import seed  # noqa: E402 — reads the same module object, so a reload reaches it


def as_mode(mode: str):
    """Re-read embeddings under `mode`, with the network stubbed out."""
    os.environ["EMBEDDING_MODE"] = mode
    importlib.reload(embeddings)
    embeddings.embed_many = lambda texts: [[0.0] * embeddings.EMBEDDING_DIMS for _ in texts]
    embeddings.embed = lambda text: [0.0] * embeddings.EMBEDDING_DIMS
    return embeddings


def indexed_path(definition: dict) -> str:
    return next(f["path"] for f in definition["fields"] if f["type"] != "filter")


def test_index_and_query_agree_on_the_field():
    for mode in embeddings.MODES:
        e = as_mode(mode)
        written = indexed_path(seed.vector_index_definition(["seniority"]))
        queried = e.query_clause("senior backend roles in Berlin")["path"]
        assert written == queried, f"{mode}: seed indexes '{written}', search queries '{queried}'"

        stored = e.index_fields(["some document text"])[0]
        assert written in stored, f"{mode}: index path '{written}' is not written to the document"


def test_auto_mode_hands_the_work_to_atlas():
    e = as_mode("auto")

    field = e.index_fields(["a job description"])[0]
    assert field == {"searchText": "a job description"}, field

    clause = e.query_clause("machine learning")
    assert clause["query"] == {"text": "machine learning"}, clause
    assert "queryVector" not in clause, "auto mode must not send a vector"

    vector_field = seed.vector_index_definition([])["fields"][0]
    assert vector_field["type"] == "autoEmbed", vector_field
    assert vector_field["model"] in e.AUTO_EMBED_MODELS, vector_field
    assert vector_field["modality"] == "text", vector_field

    # The guard that turns a silent wrong-mode call into a readable failure.
    importlib.reload(embeddings)  # drop the stub, we want the real refusal
    try:
        embeddings.embed_many(["anything"])
    except RuntimeError as exc:
        assert "auto mode" in str(exc), exc
    else:
        raise AssertionError("embed_many() should refuse to run in auto mode")


def test_api_modes_carry_their_own_vectors():
    for mode in ("atlas-voyage", "titan"):
        e = as_mode(mode)

        field = e.index_fields(["a job description"])[0]
        assert list(field) == ["embedding"], field
        assert len(field["embedding"]) == e.EMBEDDING_DIMS

        clause = e.query_clause("machine learning")
        assert len(clause["queryVector"]) == e.EMBEDDING_DIMS
        assert "query" not in clause, "an api mode must not ask Atlas to embed"

        vector_field = seed.vector_index_definition([])["fields"][0]
        assert vector_field["type"] == "vector", vector_field
        assert vector_field["numDimensions"] == e.EMBEDDING_DIMS, vector_field


def test_filters_survive_every_mode():
    for mode in embeddings.MODES:
        as_mode(mode)
        definition = seed.vector_index_definition(["seniority", "location"])
        filters = [f["path"] for f in definition["fields"] if f["type"] == "filter"]
        assert filters == ["seniority", "location"], (mode, filters)


def test_an_unknown_mode_fails_loudly():
    os.environ["EMBEDDING_MODE"] = "openai"
    try:
        importlib.reload(embeddings)
    except ValueError as exc:
        assert "openai" in str(exc), exc
    else:
        raise AssertionError("an unknown EMBEDDING_MODE should not be silently accepted")
    finally:
        os.environ["EMBEDDING_MODE"] = "atlas-voyage"
        importlib.reload(embeddings)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n✅ {len(tests)} checks passed.")
