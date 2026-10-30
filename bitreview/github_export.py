"""Convert a ReviewResult into a GitHub "create review" payload and a Markdown report.

The payload matches POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews. In production the
mentor clicks "Send" in the dashboard and the service posts it with a GitHub App token; this PoC
only builds the JSON so nothing is published without a human.
"""

from __future__ import annotations

from .schemas import ReviewResult, Severity

ICON = {Severity.BLOCKER: "🛑", Severity.MAJOR: "⚠️", Severity.MINOR: "💡", Severity.PRAISE: "✅"}


def to_github_review(result: ReviewResult, commit_id: str | None = None) -> dict:
    comments = []
    for f in result.findings:
        body = f"{ICON[f.severity]} **{f.title}** `{f.severity.value} · {f.category}`\n\n{f.comment}"
        if f.suggestion:
            body += f"\n\n**Suggested fix:** {f.suggestion}"
        comments.append({"path": f.file, "line": f.line, "side": "RIGHT", "body": body})

    payload = {
        "event": "REQUEST_CHANGES" if result.verdict == "request_changes" else "COMMENT",
        "body": to_markdown_summary(result),
        "comments": comments,
    }
    if commit_id:
        payload["commit_id"] = commit_id
    return payload


def to_markdown_summary(result: ReviewResult) -> str:
    rows = "\n".join(f"| {s.criterion_id} | {s.score}/4 | {s.evidence} |" for s in result.criterion_scores)
    return (
        f"### BitReview draft - {result.verdict.replace('_', ' ')} ({result.weighted_score}/100)\n\n"
        f"{result.summary}\n\n| Criterion | Score | Evidence |\n|---|---|---|\n{rows}\n\n"
        f"**Learn next:** {result.learning_tip}\n\n"
        f"<sub>Drafted by {result.reviewer}, reviewed by your mentor before sending.</sub>"
    )
