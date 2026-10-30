"""Human-in-the-loop routing.

The AI never merges or approves on its own. It decides HOW MUCH mentor attention a PR needs:

  fast_track         - clean PR, high confidence: mentor skims the draft and clicks approve (~2 min)
  standard           - normal review: mentor edits/accepts the drafted comments (~5 min)
  senior_escalation  - security blocker, low confidence, AI-vs-rules disagreement, possible prompt
                       injection, or the AI failed: a senior evaluator reviews from scratch (~12 min)

Minute estimates are planning assumptions used in the business case, not measurements.
"""

from __future__ import annotations

from .schemas import Finding, LLMReview, RoutingDecision, Severity

MINUTES = {"fast_track": 2.0, "standard": 5.0, "senior_escalation": 12.0}
CONFIDENCE_FLOOR = 0.6
FAST_TRACK_CONFIDENCE = 0.85


def route(
    review: LLMReview,
    final_verdict: str,
    findings: list[Finding],
    static_findings: list[Finding],
    *,
    llm_used: bool,
    llm_error: str | None,
    injection_suspected: bool,
    dropped_count: int,
) -> RoutingDecision:
    reasons: list[str] = []

    if llm_error:
        reasons.append(f"AI review unavailable ({llm_error}); offline rules only")
    if injection_suspected:
        reasons.append("Diff contains text that looks like instructions to the reviewer (possible prompt injection)")
    if any(f.severity == Severity.BLOCKER and f.category == "security" for f in findings):
        reasons.append("Security blocker found - needs a senior eye before feedback goes out")
    # Rules-only mode is triage, not judgement: its confidence is a constant, so it is not a signal.
    if llm_used and review.confidence < CONFIDENCE_FLOOR:
        reasons.append(f"Low reviewer confidence ({review.confidence:.2f})")
    static_blockers = any(f.severity == Severity.BLOCKER for f in static_findings)
    if llm_used and static_blockers and review.verdict == "approve":
        reasons.append("Disagreement: rules flagged a blocker but the AI would approve")
    if dropped_count >= 3:
        reasons.append(f"{dropped_count} AI comments pointed at lines not in the diff (grounding problem)")

    if reasons:
        return RoutingDecision(lane="senior_escalation", reasons=reasons, est_mentor_minutes=MINUTES["senior_escalation"])

    # `review.verdict` is the reviewer's own opinion; `final_verdict` has the blocker override applied.
    if final_verdict == "approve" and review.confidence >= FAST_TRACK_CONFIDENCE and llm_used:
        return RoutingDecision(lane="fast_track", reasons=["Clean PR with high-confidence AI approval; mentor spot-check only"],
                               est_mentor_minutes=MINUTES["fast_track"])

    why = "AI drafted change requests; mentor edits and sends" if final_verdict == "request_changes" \
        else "Approval drafted; mentor confirms logic"
    return RoutingDecision(lane="standard", reasons=[why], est_mentor_minutes=MINUTES["standard"])
