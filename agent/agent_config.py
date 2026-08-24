"""Load agent definitions from `config/agents/*.agent.md`.

This module is the whole reason a new agent needs zero Python. A definition is
YAML frontmatter (the wiring) plus markdown (the system prompt). Drop a new
`.agent.md` in, re-apply Terraform, and it becomes a runtime with its own
identity, model, and collection scope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))


# Bedrock rejects a thinking budget under 1024 tokens.
_MIN_THINKING_BUDGET = 1024
_DEFAULT_THINKING_BUDGET = 1024


@dataclass
class AgentDef:
    id: str
    name: str
    description: str
    role: str  # "orchestrator" | "specialist"
    model: str
    max_tokens: int
    temperature: float
    skills: list[str]
    collections: list[str]
    short_term: bool
    long_term: bool
    # 0 means no extended thinking, and therefore no reasoning stream: Bedrock
    # only emits reasoningContent deltas when a thinking budget is set.
    thinking_budget: int
    system_prompt: str
    skill_docs: dict[str, str] = field(default_factory=dict)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ValueError("agent definition must start with YAML frontmatter")
    _, fm, body = text.split("---", 2)
    return yaml.safe_load(fm) or {}, body.strip()


def _load_skill(skill: str) -> str:
    path = CONFIG_DIR / "skills" / skill / "SKILL.md"
    if not path.exists():
        raise FileNotFoundError(f"agent references unknown skill '{skill}' ({path})")
    return path.read_text()


def _thinking_budget(meta: dict) -> int:
    """Tokens the model may spend thinking out loud, from `thinking:` frontmatter.

    `thinking: true` takes the default; a number sets the budget explicitly.
    Bedrock requires at least 1024, and requires the budget to leave room for the
    answer — so a budget that would crowd out maxTokens is an error here rather
    than a ValidationException on the first turn after a deploy.
    """
    raw = meta.get("thinking", False)
    if raw is False or raw is None:
        return 0
    budget = _DEFAULT_THINKING_BUDGET if raw is True else int(raw)
    max_tokens = int(meta.get("maxTokens", 4096))
    if budget < _MIN_THINKING_BUDGET:
        raise ValueError(
            f"{meta['id']}: thinking budget {budget} is below the Bedrock minimum "
            f"of {_MIN_THINKING_BUDGET}"
        )
    if budget >= max_tokens:
        raise ValueError(
            f"{meta['id']}: thinking budget {budget} leaves nothing for the answer "
            f"— raise maxTokens above it (currently {max_tokens})"
        )
    return budget


def load_agent(agent_id: str) -> AgentDef:
    path = CONFIG_DIR / "agents" / f"{agent_id}.agent.md"
    if not path.exists():
        raise FileNotFoundError(f"no agent definition at {path}")
    meta, body = _split_frontmatter(path.read_text())
    memory = meta.get("memory") or {}
    skills = meta.get("skills") or []
    return AgentDef(
        id=meta["id"],
        name=meta.get("name", meta["id"]),
        description=(meta.get("description") or "").strip(),
        role=meta.get("role", "specialist"),
        model=meta["model"],
        max_tokens=int(meta.get("maxTokens", 4096)),
        temperature=float(meta.get("temperature", 0.3)),
        skills=skills,
        collections=meta.get("collections") or [],
        short_term=bool(memory.get("shortTerm", False)),
        long_term=bool(memory.get("longTerm", False)),
        thinking_budget=_thinking_budget(meta),
        system_prompt=body,
        skill_docs={s: _load_skill(s) for s in skills},
    )


def load_roster() -> list[dict]:
    """Every specialist's routing card, for the orchestrator's system prompt.

    Built by scanning the directory, so a new `.agent.md` shows up in the
    orchestrator's routing table without anyone editing the orchestrator.
    """
    roster = []
    for path in sorted((CONFIG_DIR / "agents").glob("*.agent.md")):
        meta, _ = _split_frontmatter(path.read_text())
        if meta.get("role") == "orchestrator":
            continue
        roster.append({
            "id": meta["id"],
            "name": meta.get("name", meta["id"]),
            "description": " ".join((meta.get("description") or "").split()),
        })
    return roster


def read_skill_resource(skill: str, resource: str) -> str:
    """Read a file under `config/skills/<skill>/references/`.

    Path-scoped to that directory: an agent cannot read its way out to another
    skill's references or to the rest of the image.
    """
    base = (CONFIG_DIR / "skills" / skill / "references").resolve()
    target = (base / resource).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"resource '{resource}' escapes the skill reference directory")
    if not target.exists():
        available = ", ".join(p.name for p in base.glob("*")) or "(none)"
        raise FileNotFoundError(f"no resource '{resource}'. Available: {available}")
    return target.read_text()
