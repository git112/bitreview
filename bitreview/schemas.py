"""Data contracts shared by every stage of the review pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    BLOCKER = "blocker"   # must be fixed before merge (security, broken behaviour)
    MAJOR = "major"       # spec not met / significant quality issue
    MINOR = "minor"       # style, naming, small improvements
    PRAISE = "praise"     # positive reinforcement - mentors asked for this explicitly


Category = Literal[
    "security", "correctness", "spec", "testing", "performance", "quality", "hygiene",
]


# ---------------------------------------------------------------- task spec
class RubricCriterion(BaseModel):
    id: str
    name: str
    description: str
    weight: float = 1.0
    # Used only by the offline heuristic reviewer (the LLM reads `description` instead):
    categories: list[str] = Field(default_factory=list)   # finding categories that lower this score
    expect: list[str] = Field(default_factory=list)       # regexes expected somewhere in the added lines


class TaskSpec(BaseModel):
    """One BitElevate daily sprint task (mirrors the 'daily spec' on the intern portal)."""

    id: str
    lab: str
    day: int
    title: str
    objective: str
    file_targets: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    rubric: list[RubricCriterion] = Field(default_factory=list)
    mentor_notes: str = ""


# ---------------------------------------------------------------- findings
class Finding(BaseModel):
    """A single inline review comment anchored to a line in the PR's new file version."""

    file: str
    line: int = Field(description="Line number in the NEW version of the file, must be an added/changed line in the diff")
    severity: Severity
    category: Category
    title: str = Field(description="Short headline, <= 80 chars")
    comment: str = Field(description="Mentor-style explanation: what is wrong and why it matters")
    suggestion: str = Field(default="", description="Concrete fix or code suggestion; empty if none")
    source: Literal["static", "llm", "heuristic"] = "llm"


class CriterionScore(BaseModel):
    criterion_id: str
    score: int = Field(ge=0, le=4, description="0=missing, 1=poor, 2=partial, 3=meets, 4=exceeds")
    evidence: str = Field(description="Cite file:line or a concrete observation from the diff")


class LLMReview(BaseModel):
    """Structured output the LLM must return (validated by the SDK's parse helper)."""

    summary: str = Field(description="2-4 sentence overview for the intern, encouraging but direct")
    criterion_scores: list[CriterionScore]
    findings: list[Finding]
    verdict: Literal["approve", "request_changes"]
    confidence: float = Field(ge=0.0, le=1.0, description="How sure you are the verdict is right")
    learning_tip: str = Field(description="One concept the intern should study next, tied to this PR")


# ---------------------------------------------------------------- pipeline output
class RoutingDecision(BaseModel):
    lane: Literal["fast_track", "standard", "senior_escalation"]
    reasons: list[str]
    est_mentor_minutes: float


class ReviewResult(BaseModel):
    task_id: str
    pr_id: str
    reviewer: str                     # e.g. "claude-opus-5-5" or "heuristic-offline"
    summary: str
    learning_tip: str
    criterion_scores: list[CriterionScore]
    weighted_score: float             # 0-100
    findings: list[Finding]
    dropped_findings: list[Finding]   # LLM comments rejected by the grounding guard
    verdict: Literal["approve", "request_changes"]
    confidence: float
    routing: RoutingDecision
    stats: dict
