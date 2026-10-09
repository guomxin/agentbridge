"""Typed public skill authoring envelopes, independent of quality evaluation.

SkillAuthoringData remains the legacy MCP envelope. Action-specific request
shapes are declared in skill_actions; domain methods retain value validation.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SkillSelection(StrictModel):
    use_when: str = Field(min_length=1, max_length=500)
    not_for: str = Field(min_length=1, max_length=500)
    output: str = Field(min_length=1, max_length=500)


class SkillDatabaseDependencies(StrictModel):
    all: list[str] = Field(default_factory=list)
    any: list[str] = Field(default_factory=list)


class SkillDependencies(SkillDatabaseDependencies):
    database: SkillDatabaseDependencies | None = None


class SkillMethod(StrictModel):
    inputs: list[str] = Field(default_factory=list, max_length=20)
    steps: list[str] = Field(default_factory=list, max_length=30)
    exceptions: list[str] = Field(default_factory=list, max_length=20)
    acceptance: list[str] = Field(default_factory=list, max_length=20)


class SkillProposal(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    selection: SkillSelection
    instructions: str = Field(min_length=1, max_length=40000)
    profiles: dict[str, SkillDependencies] = Field(default_factory=lambda: {'use': SkillDependencies()})
    executionMode: Literal['read_exploration', 'durable_plan', 'controlled_action'] = 'read_exploration'
    references: dict[str, str] = Field(default_factory=dict)
    required_resources: dict[str, list[str]] | None = None
    method: SkillMethod | None = None


class SkillAuthoringData(StrictModel):
    """Legacy action envelope with discoverable, typed fields; dispatcher checks action shape."""
    proposal: SkillProposal | None = None
    draft_id: str | None = None
    expected_revision: int | None = None
    request_key: str | None = None
    provenance: dict | None = None
    profile: str | None = None
    prompt: str | None = None
    test_id: str | None = None
    output: str | None = None
    reason: str | None = None
    audience: list[str] | None = None
    profiles: list[str] | None = None
    request_id: str | None = None
    value: dict | None = None
    target_revision: int | None = None
    material: str | None = None
    scope: str | None = None
    automatic: bool | None = None
    task_ids: list[str] | None = None
    enabled: bool | None = None
    cases: list[dict] | None = None
    repeats: int | None = None
    job_id: str | None = None
    package: dict | None = None
    skill_id: str | None = None
    rating: str | None = None
    comment: str | None = None
    version: str | None = None
    binding_id: str | None = None
    path: str | None = None
    query: str | None = None
    limit: int | None = None
    steps: list[dict] | None = None
