"""Offline reviewer: no API key, no network, fully deterministic.

It turns static findings + simple spec-coverage signals into the same `LLMReview` shape the Claude
reviewer returns, so the rest of the pipeline, the UI and the eval harness work identically.
It is the baseline the LLM must beat in eval/run_eval.py - and the graceful-degradation path when
the API is down.
"""

from __future__ import annotations

import re

from .diff_parser import ParsedDiff
from .schemas import CriterionScore, Finding, LLMReview, Severity, TaskSpec

PENALTY = {Severity.BLOCKER: 2, Severity.MAJOR: 1, Severity.MINOR: 0, Severity.PRAISE: 0}


def review_offline(spec: TaskSpec, diff: ParsedDiff, static_findings: list[Finding]) -> LLMReview:
    added_text = "\n".join(t for f in diff.files for t in f.added.values())
    scores: list[CriterionScore] = []

    for c in spec.rubric:
        score, evidence = 3, []
        missing = [p for p in c.expect if not re.search(p, added_text, re.I | re.M)]
        if c.expect:
            covered = len(c.expect) - len(missing)
            if covered == 0:
                score = 1
            elif missing:
                score = 2
            else:
                evidence.append("all expected constructs present")
            if missing:
                evidence.append(f"missing: {', '.join(missing)}")
        hits = [f for f in static_findings if f.category in c.categories]
        for f in hits:
            score -= PENALTY[f.severity]
            evidence.append(f"{f.title} ({f.file}:{f.line})")
        scores.append(CriterionScore(criterion_id=c.id, score=max(0, min(4, score)),
                                     evidence="; ".join(evidence) or "no issues detected by rules"))

    blockers = [f for f in static_findings if f.severity == Severity.BLOCKER]
    weak = [s for s in scores if s.score <= 1]
    verdict = "request_changes" if blockers or weak or any(f.severity == Severity.MAJOR for f in static_findings) else "approve"
    # Rules cannot judge design or logic, so the offline reviewer is never very confident.
    confidence = 0.55 if verdict == "approve" else 0.65

    if verdict == "approve":
        summary = "Automated checks found no blocking issues and the expected constructs for today's spec are present. A mentor should still read the logic."
    else:
        top = sorted(static_findings, key=lambda f: list(Severity).index(f.severity))[:3]
        summary = "Changes requested. Main issues: " + "; ".join(f.title for f in top) + "." if top else \
            "Changes requested: today's acceptance criteria do not appear to be fully covered."

    return LLMReview(
        summary=summary,
        criterion_scores=scores,
        findings=[],  # static findings are merged by the pipeline
        verdict=verdict,
        confidence=confidence,
        learning_tip="Re-read today's acceptance criteria and map each one to a test before opening the PR.",
    )
