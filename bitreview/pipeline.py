"""End-to-end orchestration: parse -> static checks -> AI review -> grounding guard -> route."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .diff_parser import DiffTooLargeError, parse_unified_diff
from .heuristic_reviewer import review_offline
from .llm_reviewer import DEFAULT_MODEL, LLMReviewError, has_credentials, review_with_claude
from .router import route
from .schemas import Finding, ReviewResult, Severity, TaskSpec
from .static_checks import run_static_checks

INJECTION_RE = re.compile(
    r"(ignore (all |the )?(previous|prior|above) instructions|you are now|system prompt|"
    r"(approve|lgtm) this (pr|pull request)|give (this|me) (full|maximum) (marks|score))",
    re.I,
)


def load_spec(path: str | Path) -> TaskSpec:
    return TaskSpec.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Keep one finding per (file, line, category); prefer LLM wording, keep the higher severity."""
    order = list(Severity)
    best: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.file, f.line, f.category)
        cur = best.get(key)
        if cur is None:
            best[key] = f
            continue
        winner = f if (f.source == "llm" and cur.source != "llm") else cur
        sev = min(f.severity, cur.severity, key=order.index)
        best[key] = winner.model_copy(update={"severity": sev})
    return sorted(best.values(), key=lambda f: (order.index(f.severity), f.file, f.line))


def review_pr(
    spec: TaskSpec,
    diff_text: str,
    pr_id: str = "local",
    *,
    use_llm: bool | None = None,
    model: str = DEFAULT_MODEL,
) -> ReviewResult:
    diff = parse_unified_diff(diff_text)
    static_findings, stats = run_static_checks(diff, spec)
    injection = any(INJECTION_RE.search(t) for f in diff.files for t in f.added.values())

    use_llm = has_credentials() if use_llm is None else use_llm
    llm_error: str | None = None
    llm_meta: dict = {}
    review = None

    if use_llm:
        try:
            review, llm_meta = review_with_claude(spec, diff.render_for_llm(), static_findings, model=model)
        except DiffTooLargeError as e:
            llm_error = str(e)
        except LLMReviewError as e:
            llm_error = str(e)

    llm_used = review is not None
    if review is None:
        review = review_offline(spec, diff, static_findings)

    # Grounding guard: an inline comment on a line that is not in the diff is either a hallucination
    # or un-postable to GitHub. Drop it, but count it - many drops is itself an escalation signal.
    kept, dropped = [], []
    for f in review.findings:
        (kept if diff.is_commentable(f.file, f.line) else dropped).append(f)

    findings = _dedupe(kept + static_findings)
    if injection and not any(f.category == "security" and "inject" in f.title.lower() for f in findings):
        line = next((f.path, n) for f in diff.files for n, t in f.added.items() if INJECTION_RE.search(t))
        findings.insert(0, Finding(
            file=line[0], line=line[1], severity=Severity.BLOCKER, category="security",
            title="Text addressed to the reviewer (prompt injection attempt)",
            comment="Code reviews are graded by mentors with AI assistance. Instructions aimed at the reviewer are treated as an integrity issue.",
            source="static",
        ))

    weights = {c.id: c.weight for c in spec.rubric}
    total_w = sum(weights.get(s.criterion_id, 0) for s in review.criterion_scores) or 1.0
    weighted = sum(s.score / 4 * weights.get(s.criterion_id, 0) for s in review.criterion_scores) / total_w * 100

    # Hard rule: an AI "approve" can never override a confirmed blocker.
    verdict = review.verdict
    if any(f.severity == Severity.BLOCKER for f in findings):
        verdict = "request_changes"

    routing = route(
        review, verdict, findings, static_findings,
        llm_used=llm_used, llm_error=llm_error, injection_suspected=injection, dropped_count=len(dropped),
    )

    return ReviewResult(
        task_id=spec.id,
        pr_id=pr_id,
        reviewer=llm_meta.get("model", model) if llm_used else "heuristic-offline",
        summary=review.summary,
        learning_tip=review.learning_tip,
        criterion_scores=review.criterion_scores,
        weighted_score=round(weighted, 1),
        findings=findings,
        dropped_findings=dropped,
        verdict=verdict,
        confidence=review.confidence,
        routing=routing,
        stats={**stats, **llm_meta, "llm_error": llm_error, "injection_suspected": injection},
    )
