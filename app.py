"""BitReview mentor console - Streamlit demo.

    streamlit run app.py

Works without an API key (offline rules + heuristics). Add ANTHROPIC_API_KEY (env or sidebar) to
enable the Claude reviewer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
import yaml

from bitreview.diff_parser import parse_unified_diff
from bitreview.github_export import to_github_review
from bitreview.llm_reviewer import DEFAULT_MODEL
from bitreview.pipeline import load_spec, review_pr
from bitreview.schemas import Severity

ROOT = Path(__file__).parent
SPECS = {p.stem: p for p in sorted((ROOT / "data/specs").glob("*.yaml"))}
PRS = {p.stem: p for p in sorted((ROOT / "data/prs").glob("*.diff"))}
LABELS = yaml.safe_load((ROOT / "data/prs/labels.yaml").read_text(encoding="utf-8"))
SEV_ICON = {Severity.BLOCKER: "🛑", Severity.MAJOR: "⚠️", Severity.MINOR: "💡", Severity.PRAISE: "✅"}
LANE = {"fast_track": ("🟢 Fast track", "success"), "standard": ("🟡 Standard review", "warning"),
        "senior_escalation": ("🔴 Senior escalation", "error")}

st.set_page_config(page_title="BitReview - AI PR co-reviewer", page_icon="🧑‍🏫", layout="wide")
st.title("BitReview · AI co-reviewer for BitElevate daily PRs")
st.caption("Cynbit AI/ML intern assessment PoC. The AI drafts the review, a mentor approves it. Nothing is posted automatically.")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Input")
    source = st.radio("PR source", ["Sample PR", "Paste / upload diff"], horizontal=True)
    if source == "Sample PR":
        pr_name = st.selectbox("Sample PR", list(PRS), format_func=lambda s: s.replace("_", " "))
        diff_text = PRS[pr_name].read_text(encoding="utf-8")
        spec_name = LABELS.get(pr_name, {}).get("spec", next(iter(SPECS)))
        st.caption(f"Labelled verdict: **{LABELS[pr_name]['verdict']}**, {len(LABELS[pr_name]['issues'])} planted issue(s)")
    else:
        pr_name = "custom"
        up = st.file_uploader("Unified diff (`gh pr diff 42 > pr.diff`)", type=["diff", "patch", "txt"])
        diff_text = up.read().decode("utf-8") if up else st.text_area("…or paste a diff", height=200)
        spec_name = None
    spec_name = st.selectbox("Daily spec", list(SPECS), index=list(SPECS).index(spec_name) if spec_name else 0)

    st.header("Reviewer")
    key = st.text_input("Anthropic API key (optional, kept in this session only)", type="password")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    use_llm = st.toggle("Use Claude reviewer", value=has_key, disabled=not has_key,
                        help="Without a key the offline rules + heuristics reviewer runs.")
    model = st.text_input("Model", DEFAULT_MODEL, disabled=not use_llm)
    run = st.button("Review PR", type="primary", use_container_width=True)

spec = load_spec(SPECS[spec_name])
tab_review, tab_spec, tab_impact = st.tabs(["Review", "Daily spec", "Cohort impact calculator"])

with tab_spec:
    st.subheader(f"Day {spec.day}: {spec.title}")
    st.write(spec.objective)
    st.markdown("**Acceptance criteria**\n" + "\n".join(f"- {c}" for c in spec.acceptance_criteria))
    st.markdown("**Rubric**\n" + "\n".join(f"- **{c.name}** (×{c.weight}): {c.description}" for c in spec.rubric))

# ------------------------------------------------------------------ review
with tab_review:
    if run and diff_text.strip():
        with st.spinner("Reviewing…"):
            st.session_state.result = review_pr(spec, diff_text, pr_id=pr_name, use_llm=use_llm, model=model)
            st.session_state.diff_text = diff_text

    res = st.session_state.get("result")
    if res is None:
        st.info("Pick a sample PR (try `pr02_jwt_insecure` or `pr03_jwt_injection`) and click **Review PR**.")
    else:
        label, kind = LANE[res.routing.lane]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Verdict", res.verdict.replace("_", " ").title())
        c2.metric("Rubric score", f"{res.weighted_score}/100")
        c3.metric("Confidence", f"{res.confidence:.0%}")
        c4.metric("Est. mentor time", f"{res.routing.est_mentor_minutes:.0f} min")
        getattr(st, kind)(f"**{label}**: " + " · ".join(res.routing.reasons))
        if res.stats.get("llm_error"):
            st.caption(f"⚠️ {res.stats['llm_error']}")

        left, right = st.columns([3, 2])
        with right:
            st.subheader("Rubric")
            names = {c.id: c.name for c in spec.rubric}
            for s in res.criterion_scores:
                st.progress(s.score / 4, text=f"{names.get(s.criterion_id, s.criterion_id)}: {s.score}/4")
                st.caption(s.evidence)
            st.subheader("Draft summary to intern")
            st.write(res.summary)
            st.markdown(f"**Learn next:** {res.learning_tip}")
            st.caption(f"Reviewer: `{res.reviewer}` · {res.stats['files_changed']} files, "
                       f"+{res.stats['added_lines']}/-{res.stats['removed_lines']} lines"
                       + (f" · {res.stats['input_tokens']} in / {res.stats['output_tokens']} out tokens"
                          if "input_tokens" in res.stats else ""))

        with left:
            st.subheader(f"Inline comments ({len(res.findings)})")
            st.caption("Mentor: untick anything you disagree with. Only ticked comments are exported.")
            keep = []
            for i, f in enumerate(res.findings):
                with st.container(border=True):
                    ok = st.checkbox(f"{SEV_ICON[f.severity]} **{f.title}** · `{f.file}:{f.line}` · {f.category} · _{f.source}_",
                                     value=True, key=f"keep_{i}_{res.pr_id}")
                    st.write(f.comment)
                    if f.suggestion:
                        st.markdown(f"**Fix:** {f.suggestion}")
                    if ok:
                        keep.append(f)
            if res.dropped_findings:
                with st.expander(f"{len(res.dropped_findings)} AI comment(s) dropped by grounding guard"):
                    for f in res.dropped_findings:
                        st.write(f"`{f.file}:{f.line}` {f.title}")

            approved = res.model_copy(update={"findings": keep})
            st.download_button("⬇️ Export GitHub review payload (JSON)",
                               json.dumps(to_github_review(approved), indent=2),
                               file_name=f"{res.pr_id}_review.json", mime="application/json")

        with st.expander("Diff with comment markers"):
            diff = parse_unified_diff(st.session_state.diff_text)
            marks = {(f.file, f.line): f for f in res.findings}
            for fd in diff.files:
                st.markdown(f"**{fd.path}**")
                lines = []
                for no, text in sorted({**fd.context, **fd.added}.items()):
                    m = marks.get((fd.path, no))
                    tag = f"   ◀ {SEV_ICON[m.severity]} {m.title}" if m else ""
                    lines.append(f"{'+' if no in fd.added else ' '}{no:>4} {text}{tag}")
                st.code("\n".join(lines), language="diff")

# ------------------------------------------------------------------ impact calculator
with tab_impact:
    st.subheader("What does this save at BitElevate scale?")
    st.caption("All inputs are assumptions you can change. Cynbit publicly reports 1,000+ BitElevate graduates and a 60-day, one-PR-per-day format.")
    a, b = st.columns(2)
    with a:
        interns = st.slider("Active interns in a cohort", 50, 2000, 300, 50)
        prs_per_intern = st.slider("PRs per intern per program", 20, 60, 50)
        manual_min = st.slider("Manual review minutes per PR (today)", 3, 30, 10)
        hourly = st.number_input("Mentor cost (₹/hour)", 100, 3000, 500, 50)
    with b:
        fast = st.slider("% PRs fast-tracked (2 min)", 0, 80, 35)
        senior = st.slider("% PRs escalated (12 min)", 0, 60, 15)
        standard = max(0, 100 - fast - senior)
        st.write(f"% standard (5 min): **{standard}%**")
        cost_per_review = st.number_input("LLM cost per review (₹)", 0.0, 20.0, 3.0, 0.5,
                                          help="See docs/02_Technical_Approach.md for the token math.")
    total_prs = interns * prs_per_intern
    manual_h = total_prs * manual_min / 60
    ai_h = total_prs * (fast * 2 + standard * 5 + senior * 12) / 100 / 60
    llm_cost = total_prs * cost_per_review
    saved = (manual_h - ai_h) * hourly - llm_cost
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("PRs per cohort", f"{total_prs:,}")
    m2.metric("Mentor hours", f"{ai_h:,.0f}", f"{ai_h - manual_h:,.0f} h vs manual", delta_color="inverse")
    m3.metric("LLM spend", f"₹{llm_cost:,.0f}")
    m4.metric("Net saving", f"₹{saved:,.0f}")
    st.caption(f"Feedback turnaround also drops from the mentor queue (hours) to under a minute for the draft, "
               f"which matters for a programme whose promise is daily PR feedback.")
