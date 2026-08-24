#!/usr/bin/env python3
"""Self-check for the config-only agent contract.

The workshop's headline claim is that an agent is added by dropping a markdown
file in and changing no code. This is the check that fails if that stops being
true.

    python agent/test_config.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("CONFIG_DIR", str(REPO / "config"))
sys.path.insert(0, str(REPO / "agent"))

import agent_config  # noqa: E402


def test_every_definition_loads():
    ids = [p.stem.replace(".agent", "") for p in (REPO / "config/agents").glob("*.agent.md")]
    assert ids, "no agent definitions found"

    for agent_id in ids:
        a = agent_config.load_agent(agent_id)
        assert a.id == agent_id, f"{agent_id}.agent.md declares id '{a.id}' — filename is the id"
        assert a.model, f"{agent_id} has no model"
        assert a.system_prompt.strip(), f"{agent_id} has an empty system prompt"
        assert a.role in ("orchestrator", "specialist"), f"{agent_id} has role '{a.role}'"


def test_exactly_one_orchestrator():
    roles = [agent_config.load_agent(p.stem.replace(".agent", "")).role
             for p in (REPO / "config/agents").glob("*.agent.md")]
    assert roles.count("orchestrator") == 1, f"expected 1 orchestrator, found {roles.count('orchestrator')}"


def test_specialists_are_wired():
    for path in (REPO / "config/agents").glob("*.agent.md"):
        a = agent_config.load_agent(path.stem.replace(".agent", ""))
        if a.role != "specialist":
            continue
        assert a.description, f"{a.id} has no description — the orchestrator routes on it"
        assert a.collections, f"{a.id} has no collections — it would have nothing to query"
        assert a.skills, f"{a.id} has no skill"
        for skill in a.skills:
            assert a.skill_docs[skill].strip(), f"{a.id}: skill '{skill}' is empty"
            ref = REPO / "config/skills" / skill / "references/collections-schema.md"
            assert ref.exists(), f"{a.id}: skill '{skill}' has no collections-schema.md"


def test_roster_excludes_the_orchestrator():
    roster = agent_config.load_roster()
    ids = {a["id"] for a in roster}
    assert "orchestrator" not in ids, "orchestrator must not be able to route to itself"
    assert ids == {"career-coach", "job-match", "company-intel"}, ids
    for entry in roster:
        assert entry["description"], f"{entry['id']} has no routing description"


def test_skill_resources_are_readable_and_sandboxed():
    body = agent_config.read_skill_resource("career-coach", "collections-schema.md")
    assert "career_paths" in body

    for escape in ("../../../etc/passwd", "../../job-match/references/collections-schema.md"):
        try:
            agent_config.read_skill_resource("career-coach", escape)
        except (ValueError, FileNotFoundError):
            continue
        raise AssertionError(f"path traversal was not blocked: {escape}")


def test_a_new_agent_needs_no_code_change():
    """Copy the config tree, drop in a fourth agent, and confirm it is picked up.

    Nothing in this function touches Python outside config/ — that is the point.
    """
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "config"
        shutil.copytree(REPO / "config", staged)

        (staged / "agents" / "salary-negotiator.agent.md").write_text(
            "---\n"
            "id: salary-negotiator\n"
            "name: Salary Negotiator\n"
            "description: Rehearses a compensation negotiation with the candidate.\n"
            "role: specialist\n"
            "model: us.anthropic.claude-haiku-4-5-20251001-v1:0\n"
            "maxTokens: 2048\n"
            "temperature: 0.4\n"
            "skills: []\n"
            "collections:\n"
            "  - compensation_data\n"
            "memory:\n"
            "  shortTerm: true\n"
            "  longTerm: true\n"
            "---\n\n"
            "# Salary Negotiator\n\nYou rehearse negotiations, grounded in retrieved bands.\n"
        )

        original = agent_config.CONFIG_DIR
        try:
            agent_config.CONFIG_DIR = staged
            roster = agent_config.load_roster()
            assert "salary-negotiator" in {a["id"] for a in roster}, \
                "a new .agent.md did not appear in the orchestrator's roster"

            added = agent_config.load_agent("salary-negotiator")
            assert added.collections == ["compensation_data"]
            assert added.role == "specialist"
        finally:
            agent_config.CONFIG_DIR = original


def test_thinking_budget_must_leave_room_for_the_answer():
    """A budget at or above maxTokens is a Bedrock ValidationException on the
    first turn after a deploy — catch it here, where it costs nothing."""
    ok = agent_config._thinking_budget({"id": "x", "thinking": 1024, "maxTokens": 4096})
    assert ok == 1024, ok

    for bad in ({"id": "x", "thinking": 4096, "maxTokens": 4096},   # no room left
                {"id": "x", "thinking": 512, "maxTokens": 4096}):   # under the floor
        try:
            agent_config._thinking_budget(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted an unusable thinking budget: {bad}")


def test_thinking_is_off_unless_asked_for():
    """No reasoning stream is the default: it costs output tokens every turn."""
    assert agent_config._thinking_budget({"id": "x", "maxTokens": 4096}) == 0
    assert agent_config._thinking_budget({"id": "x", "thinking": False}) == 0
    assert agent_config._thinking_budget({"id": "x", "thinking": True}) == 1024


def test_every_shipped_agent_streams_reasoning():
    """The UI's reasoning panel is empty for any agent that skips this."""
    for agent in agent_config.load_roster() + [{"id": "orchestrator"}]:
        d = agent_config.load_agent(agent["id"])
        assert d.thinking_budget >= 1024, f"{d.id} has no thinking budget"
        assert d.max_tokens > d.thinking_budget, f"{d.id} left no room for the answer"


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
