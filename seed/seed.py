#!/usr/bin/env python3
"""Seed MongoDB Atlas — data, embeddings, and indexes.

This is what replaces a Bedrock Knowledge Base. Everything the agents can know
is in `seed/data/*.json`; this script embeds it and builds the Atlas Vector
Search indexes the agents query through MCP.

Idempotent: re-running replaces documents and leaves existing indexes alone.

    MONGODB_URI='mongodb+srv://...' AWS_REGION=us-east-1 python seed/seed.py

Embedding mode follows the agents' — `EMBEDDING_MODE` picks one of atlas-voyage
(default), auto, or titan. In the first two this script embeds the corpus and
stores vectors; in `auto` it stores plain text in `searchText` and Atlas embeds
it behind an `autoEmbed` index. Everything downstream is the same either way.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from pymongo import MongoClient, UpdateOne
from pymongo.operations import SearchIndexModel

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "agent"))
import embeddings  # noqa: E402 — single source of truth for provider + dimensions

DB_NAME = os.environ.get("MONGODB_DB", "riv_workshop")

# collection -> (business key, text-for-embedding builder, filterable fields)
DOMAIN = {
    "jobs": (
        "jobId",
        lambda d: "\n".join([
            f"{d['title']} at {d['company']} ({d['location']}, {d['remote']}, {d['seniority']})",
            d["description"],
            "Responsibilities: " + "; ".join(d.get("responsibilities", [])),
            "Skills: " + ", ".join(s["name"] for s in d.get("requiredSkills", [])),
        ]),
        ["seniority", "location", "remote", "companyId"],
    ),
    "career_paths": (
        "pathId",
        lambda d: "\n".join([
            f"Path to {d['targetRole']} ({d['seniority']}) from: {', '.join(d.get('fromRoles', []))}",
            d["description"],
            "Required skills: " + ", ".join(s["name"] for s in d.get("requiredSkills", [])),
            "Milestones: " + "; ".join(m["title"] for m in d.get("milestones", [])),
        ]),
        ["seniority"],
    ),
    "skills_taxonomy": (
        "skillId",
        lambda d: "\n".join([
            f"{d['name']} ({d['category']})",
            d["description"],
            "Resources: " + "; ".join(r["title"] for r in d.get("learningResources", [])),
        ]),
        ["category"],
    ),
    "compensation_data": (
        "benchmarkId",
        lambda d: (
            f"{d['role']} — {d['seniority']} level in {d['location']}. "
            f"Base {d['currency']} {d['base']['p25']}-{d['base']['p75']}, "
            f"total {d['currency']} {d['total']['p25']}-{d['total']['p75']}. "
            f"Benefits: {', '.join(d.get('benefits', []))}."
        ),
        ["seniority", "location", "role"],
    ),
    "companies": (
        "companyId",
        lambda d: "\n".join([
            f"{d['name']} — {d['industry']}, {d['size']} people, {d['stage']}, {d['headquarters']}",
            "Tech: " + ", ".join(d.get("techStack", [])),
            "Engineering: " + "; ".join(d.get("engineeringPractices", [])),
            "Culture: " + "; ".join(d.get("culture", [])),
            "Sentiment: " + "; ".join(
                d.get("sentiment", {}).get("themes", {}).get("positive", [])
                + d.get("sentiment", {}).get("themes", {}).get("negative", [])
            ),
        ]),
        ["industry", "stage"],
    ),
}

# Memory collections: created empty, indexed now so the first agent turn does
# not race an index build.
MEMORY_VECTOR = {
    "agent_facts": ["userId", "kind"],
    "chat_messages": ["userId", "sessionId", "role"],
}


def load(name: str) -> list[dict]:
    return json.loads((ROOT / "data" / f"{name}.json").read_text())


def seed_domain(db) -> None:
    for name, (key, to_text, _) in DOMAIN.items():
        docs = load(name)
        verb = "preparing" if embeddings.AUTO else "embedding"
        print(f"  {name}: {verb} {len(docs)} documents…", flush=True)
        # One batched call per collection rather than one per document: Voyage's
        # free tier allows only a handful of requests a minute. In auto mode this
        # makes no API call at all.
        fields = embeddings.index_fields([to_text(d) for d in docs])
        ops = []
        for doc, field in zip(docs, fields):
            doc.update(field)
            ops.append(UpdateOne({key: doc[key]}, {"$set": doc}, upsert=True))
        result = db[name].bulk_write(ops)
        print(f"  {name}: {result.upserted_count} inserted, {result.modified_count} updated")

    # candidates has no vector index — looked up by id, never searched.
    docs = load("candidates")
    db.candidates.bulk_write(
        [UpdateOne({"candidateId": d["candidateId"]}, {"$set": d}, upsert=True) for d in docs]
    )
    print(f"  candidates: {len(docs)} upserted")


def vector_index_definition(filters: list[str]) -> dict:
    """The index definition for the active embedding mode.

    `vector` and `autoEmbed` cannot coexist in one definition, which is why the
    mode is a deploy-time choice rather than a per-query one. The autoEmbed form
    omits numDimensions/similarity deliberately — Atlas fills in the right
    defaults for the model, and pinning them here would only go stale.
    """
    if embeddings.AUTO:
        vector_field = {"type": "autoEmbed", "modality": "text",
                        "path": embeddings.AUTO_FIELD, "model": embeddings.VOYAGE_MODEL}
    else:
        vector_field = {"type": "vector", "path": embeddings.VECTOR_FIELD,
                        "numDimensions": embeddings.EMBEDDING_DIMS, "similarity": "cosine"}

    return {"fields": [
        vector_field,
        *({"type": "filter", "path": f} for f in filters),
    ]}


def ensure_vector_index(db, collection: str, filters: list[str]) -> bool:
    """Create `<collection>_vector_index` if absent. Returns True if created."""
    coll = db[collection]
    if collection not in db.list_collection_names():
        db.create_collection(collection)
        coll = db[collection]

    name = f"{collection}_vector_index"
    if any(idx["name"] == name for idx in coll.list_search_indexes()):
        print(f"  {name}: already exists")
        return False

    coll.create_search_index(SearchIndexModel(
        name=name,
        type="vectorSearch",
        definition=vector_index_definition(filters),
    ))
    print(f"  {name}: creating…")
    return True


def wait_for_indexes(db, collections: list[str], timeout_s: int = 600) -> None:
    """Block until every vector index reports READY.

    Worth the wait: an agent querying a still-building index gets an empty
    result, which reads exactly like "no data" and sends attendees debugging
    the wrong thing.
    """
    deadline = time.time() + timeout_s
    pending = set(collections)
    while pending and time.time() < deadline:
        for collection in sorted(pending):
            name = f"{collection}_vector_index"
            status = next(
                (i.get("status") for i in db[collection].list_search_indexes() if i["name"] == name),
                None,
            )
            if status == "READY":
                pending.discard(collection)
                print(f"  {name}: READY")
            elif status == "FAILED":
                raise RuntimeError(f"{name} failed to build")
        if pending:
            time.sleep(10)
    if pending:
        raise TimeoutError(f"indexes still building after {timeout_s}s: {', '.join(sorted(pending))}")


def wait_for_auto_embeddings(db, collections: list[str], timeout_s: int = 900) -> None:
    """Block until an `autoEmbed` index actually answers a query.

    READY means the index exists, not that Atlas has finished embedding the
    documents behind it — that happens asynchronously after the write. So poll a
    real `$vectorSearch` rather than a status field: until it returns something,
    an agent asking a perfectly good question gets "nothing found".
    """
    pending = [c for c in collections if db[c].estimated_document_count()]
    deadline = time.time() + timeout_s
    while pending and time.time() < deadline:
        for collection in list(pending):
            hits = list(db[collection].aggregate([
                {"$vectorSearch": {
                    "index": f"{collection}_vector_index",
                    "path": embeddings.AUTO_FIELD,
                    "query": {"text": "engineer"},
                    "numCandidates": 20,
                    "limit": 1,
                }},
                {"$project": {"_id": 1}},
            ]))
            if hits:
                pending.remove(collection)
                print(f"  {collection}: embeddings ready")
        if pending:
            time.sleep(15)
    if pending:
        raise TimeoutError(
            f"Atlas is still generating embeddings after {timeout_s}s for: "
            f"{', '.join(sorted(pending))}. Check that storage auto-scaling is on — "
            f"a full disk pauses embedding generation and marks the index Stale."
        )


def main() -> None:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        sys.exit("MONGODB_URI is required")

    db = MongoClient(uri, serverSelectionTimeoutMS=20000)[DB_NAME]
    db.command("ping")
    detail = (f"model {embeddings.VOYAGE_MODEL}, generated in-cluster"
              if embeddings.AUTO else f"{embeddings.EMBEDDING_DIMS}d, generated here")
    print(f"━━ Seeding '{DB_NAME}' — embedding mode {embeddings.provider()} "
          f"({detail}) ━━\n")

    print("Documents:")
    seed_domain(db)

    print("\nLookup indexes:")
    for collection, (key, _, _) in DOMAIN.items():
        db[collection].create_index(key, unique=True)
    db.candidates.create_index("candidateId", unique=True)
    db.chat_messages.create_index([("sessionId", 1), ("createdAt", 1)])
    db.chat_sessions.create_index("sessionId", unique=True)
    db.chat_sessions.create_index([("userId", 1), ("updatedAt", -1)])
    db.agent_traces.create_index([("sessionId", 1), ("createdAt", -1)])
    db.agent_traces.create_index("traceId")
    print("  done")

    print("\nVector indexes:")
    created = []
    for collection, (_, _, filters) in DOMAIN.items():
        if ensure_vector_index(db, collection, filters):
            created.append(collection)
    for collection, filters in MEMORY_VECTOR.items():
        if ensure_vector_index(db, collection, filters):
            created.append(collection)

    if created:
        print("\nWaiting for vector indexes to build (a few minutes on a new cluster):")
        wait_for_indexes(db, created)
        if embeddings.AUTO:
            print("\nWaiting for Atlas to embed the documents:")
            wait_for_auto_embeddings(db, created)

    print("\n✅ Seed complete.")


if __name__ == "__main__":
    main()
