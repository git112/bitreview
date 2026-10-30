"""Evaluate the reviewer against the labelled sample PRs.

    python eval/run_eval.py              # offline baseline (rules + heuristics), free
    python eval/run_eval.py --llm        # Claude reviewer (needs ANTHROPIC_API_KEY; ~8 API calls)

Metrics
  verdict accuracy     - approve / request_changes matches the label
  issue recall         - labelled issues found (overall, rule-detectable, reasoning-only)
  precision            - blocker/major findings that correspond to a labelled issue
  false blocks         - clean PRs that received any blocker/major finding
  routing mix          - share of PRs per mentor lane, and the implied mentor minutes
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bitreview.pipeline import load_spec, review_pr  # noqa: E402
from bitreview.schemas import Severity  # noqa: E402

LINE_TOLERANCE = 3
BASELINE_MINUTES = 10.0  # assumed fully-manual mentor review time per daily PR (see docs)


def matches(finding, issue) -> bool:
    if finding.file != issue["file"] or finding.category not in issue["categories"]:
        return False
    return issue.get("any_line") or abs(finding.line - issue["line"]) <= LINE_TOLERANCE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="use the Claude reviewer")
    ap.add_argument("--out", default=None, help="write per-PR results JSON here")
    a = ap.parse_args()

    labels = yaml.safe_load((ROOT / "data/prs/labels.yaml").read_text(encoding="utf-8"))
    rows, per_pr = [], []
    tot = Counter()

    for pr_id, lab in labels.items():
        spec = load_spec(ROOT / f"data/specs/{lab['spec']}.yaml")
        diff = (ROOT / f"data/prs/{pr_id}.diff").read_text(encoding="utf-8")
        res = review_pr(spec, diff, pr_id=pr_id, use_llm=a.llm)

        graded = [f for f in res.findings if f.severity != Severity.PRAISE]
        serious = [f for f in graded if f.severity in (Severity.BLOCKER, Severity.MAJOR)]
        found = [i for i in lab["issues"] if any(matches(f, i) for f in graded)]
        tp_serious = [f for f in serious if any(matches(f, i) for i in lab["issues"])]

        tot["prs"] += 1
        tot["verdict_ok"] += res.verdict == lab["verdict"]
        tot["issues"] += len(lab["issues"])
        tot["found"] += len(found)
        tot["rule_issues"] += sum(i["rule_detectable"] for i in lab["issues"])
        tot["rule_found"] += sum(i["rule_detectable"] for i in found)
        tot["reason_issues"] += sum(not i["rule_detectable"] for i in lab["issues"])
        tot["reason_found"] += sum(not i["rule_detectable"] for i in found)
        tot["serious"] += len(serious)
        tot["serious_tp"] += len(tp_serious)
        tot["clean_prs"] += not lab["issues"]
        tot["false_block"] += (not lab["issues"]) and bool(serious)
        tot[f"lane_{res.routing.lane}"] += 1
        tot["mentor_minutes"] += res.routing.est_mentor_minutes

        missed = [i["id"] for i in lab["issues"] if i not in found]
        rows.append((pr_id, lab["verdict"], res.verdict, f"{len(found)}/{len(lab['issues'])}",
                     res.routing.lane, ", ".join(missed) or "-"))
        per_pr.append(json.loads(res.model_dump_json()))

    w = [24, 16, 16, 7, 18, 0]
    hdr = ("PR", "expected", "got", "issues", "route", "missed")
    print("".join(h.ljust(n) for h, n in zip(hdr, w)))
    for r in rows:
        print("".join(str(c).ljust(n) for c, n in zip(r, w)))

    pct = lambda n, d: f"{100 * n / d:.0f}%" if d else "n/a"  # noqa: E731
    reviewer = "Claude" if a.llm else "offline rules+heuristics"
    print(f"\nReviewer: {reviewer}")
    print(f"Verdict accuracy        {tot['verdict_ok']}/{tot['prs']}  ({pct(tot['verdict_ok'], tot['prs'])})")
    print(f"Issue recall (all)      {tot['found']}/{tot['issues']}  ({pct(tot['found'], tot['issues'])})")
    print(f"  rule-detectable       {tot['rule_found']}/{tot['rule_issues']}  ({pct(tot['rule_found'], tot['rule_issues'])})")
    print(f"  reasoning-only        {tot['reason_found']}/{tot['reason_issues']}  ({pct(tot['reason_found'], tot['reason_issues'])})")
    print(f"Blocker/major precision {tot['serious_tp']}/{tot['serious']}  ({pct(tot['serious_tp'], tot['serious'])})")
    print(f"False blocks on clean PRs {tot['false_block']}/{tot['clean_prs']}")
    lanes = {k[5:]: v for k, v in tot.items() if k.startswith("lane_")}
    print(f"Routing mix             {lanes}")
    baseline = BASELINE_MINUTES * tot["prs"]
    print(f"Mentor minutes          {tot['mentor_minutes']:.0f} vs {baseline:.0f} fully manual "
          f"({pct(baseline - tot['mentor_minutes'], baseline)} saved, planning assumptions)")

    if a.out:
        Path(a.out).write_text(json.dumps(per_pr, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
