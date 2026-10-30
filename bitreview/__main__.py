"""CLI: python -m bitreview --spec data/specs/day12_jwt_auth.yaml --diff data/prs/pr02_jwt_insecure.diff"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .github_export import to_github_review
from .pipeline import load_spec, review_pr
from .schemas import Severity

COLOR = {Severity.BLOCKER: "\033[91m", Severity.MAJOR: "\033[93m", Severity.MINOR: "\033[96m", Severity.PRAISE: "\033[92m"}
RESET = "\033[0m"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bitreview", description="AI co-reviewer for BitElevate daily PRs")
    p.add_argument("--spec", required=True, help="Daily task spec YAML")
    p.add_argument("--diff", required=True, help="Unified diff file ('-' for stdin), e.g. from `gh pr diff 42`")
    p.add_argument("--offline", action="store_true", help="Skip the LLM; rules + heuristics only")
    p.add_argument("--model", default=None, help="Claude model id (default: $BITREVIEW_MODEL or claude-opus-5-5)")
    p.add_argument("--json", action="store_true", help="Print the full ReviewResult as JSON")
    p.add_argument("--github-payload", metavar="FILE", help="Also write a GitHub 'create review' payload to FILE")
    a = p.parse_args(argv)

    diff_text = sys.stdin.read() if a.diff == "-" else Path(a.diff).read_text(encoding="utf-8")
    kwargs = {"use_llm": False} if a.offline else {}
    if a.model:
        kwargs["model"] = a.model
    result = review_pr(load_spec(a.spec), diff_text, pr_id=Path(a.diff).stem, **kwargs)

    if a.github_payload:
        Path(a.github_payload).write_text(json.dumps(to_github_review(result), indent=2), encoding="utf-8")

    if a.json:
        print(result.model_dump_json(indent=2))
        return 0

    print(f"\n{result.pr_id}  ->  {result.verdict.upper()}  score {result.weighted_score}/100  "
          f"confidence {result.confidence:.2f}  [{result.reviewer}]")
    print(f"Route: {result.routing.lane}  (~{result.routing.est_mentor_minutes:.0f} mentor-min)")
    for r in result.routing.reasons:
        print(f"   - {r}")
    print(f"\n{result.summary}\n")
    for s in result.criterion_scores:
        print(f"  {s.criterion_id:<14} {'#' * s.score}{'.' * (4 - s.score)}  {s.evidence}")
    print()
    for f in result.findings:
        print(f"  {COLOR[f.severity]}{f.severity.value:<8}{RESET} {f.file}:{f.line}  {f.title}  ({f.source})")
    if result.dropped_findings:
        print(f"\n  {len(result.dropped_findings)} AI comment(s) dropped by grounding guard")
    if result.stats.get("llm_error"):
        print(f"\n  note: {result.stats['llm_error']}")
    print(f"\nLearn next: {result.learning_tip}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
