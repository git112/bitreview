from pathlib import Path
from types import SimpleNamespace

import pytest

from bitreview import llm_reviewer
from bitreview.diff_parser import parse_unified_diff
from bitreview.github_export import to_github_review
from bitreview.pipeline import load_spec, review_pr
from bitreview.schemas import CriterionScore, Finding, LLMReview, Severity

ROOT = Path(__file__).resolve().parents[1]
JWT_SPEC = load_spec(ROOT / "data/specs/day12_jwt_auth.yaml")
PAGE_SPEC = load_spec(ROOT / "data/specs/day09_pagination_bugfix.yaml")


def read(name):
    return (ROOT / "data/prs" / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ diff parser
def test_parser_maps_new_file_line_numbers():
    d = parse_unified_diff(read("pr02_jwt_insecure.diff"))
    sec = d.file("app/auth/security.py")
    assert sec.is_new
    assert sec.added[8].startswith("SECRET_KEY")
    assert d.is_commentable("app/auth/security.py", 26)
    assert not d.is_commentable("app/auth/security.py", 999)


def test_parser_handles_modified_hunks():
    d = parse_unified_diff(read("pr08_pagination_subtle.diff"))
    crud = d.file("app/crud/services.py")
    assert "limit(size - 1)" in crud.added[17]
    assert 15 in crud.context  # unchanged `def list_services` line


# ------------------------------------------------------------------ offline pipeline
def test_insecure_pr_is_blocked_and_escalated():
    r = review_pr(JWT_SPEC, read("pr02_jwt_insecure.diff"), use_llm=False)
    titles = {f.title for f in r.findings}
    assert {"Hard-coded secret", "Password compared or stored in plain text", "No tests added for new behaviour"} <= titles
    assert r.verdict == "request_changes"
    assert r.routing.lane == "senior_escalation"


def test_clean_pr_has_no_blockers():
    r = review_pr(JWT_SPEC, read("pr01_jwt_good.diff"), use_llm=False)
    assert not [f for f in r.findings if f.severity in (Severity.BLOCKER, Severity.MAJOR)]
    assert r.verdict == "approve"


def test_prompt_injection_is_flagged_and_escalated():
    r = review_pr(JWT_SPEC, read("pr03_jwt_injection.diff"), use_llm=False)
    assert r.stats["injection_suspected"]
    assert any("injection" in f.title.lower() for f in r.findings)
    assert r.routing.lane == "senior_escalation"


def test_github_payload_only_uses_diff_lines():
    r = review_pr(JWT_SPEC, read("pr02_jwt_insecure.diff"), use_llm=False)
    payload = to_github_review(r)
    d = parse_unified_diff(read("pr02_jwt_insecure.diff"))
    assert payload["event"] == "REQUEST_CHANGES"
    assert all(d.is_commentable(c["path"], c["line"]) for c in payload["comments"])


# ------------------------------------------------------------------ LLM path with a fake client
class FakeMessages:
    def __init__(self, review, stop_reason="end_turn"):
        self.review, self.stop_reason, self.calls = review, stop_reason, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=5000, output_tokens=900, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(parsed_output=self.review, stop_reason=self.stop_reason,
                               usage=usage, model=kwargs["model"])


def fake_client(review, stop_reason="end_turn"):
    msgs = FakeMessages(review, stop_reason)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def llm_review(findings, verdict="approve", confidence=0.9):
    return LLMReview(
        summary="ok", verdict=verdict, confidence=confidence, learning_tip="tip", findings=findings,
        criterion_scores=[CriterionScore(criterion_id=c.id, score=3, evidence="x") for c in PAGE_SPEC.rubric],
    )


@pytest.fixture
def use_fake(monkeypatch):
    def install(review, stop_reason="end_turn"):
        client, msgs = fake_client(review, stop_reason)
        real = llm_reviewer.review_with_claude
        monkeypatch.setattr("bitreview.pipeline.review_with_claude",
                            lambda *a, **k: real(*a, client=client, **k))
        return msgs
    return install


def test_llm_request_shape(use_fake):
    msgs = use_fake(llm_review([]))
    review_pr(PAGE_SPEC, read("pr08_pagination_subtle.diff"), use_llm=True)
    call = msgs.calls[0]
    assert call["output_format"] is LLMReview
    assert call["thinking"] == {"type": "adaptive"}
    assert call["fallbacks"] == "default"
    first_block = call["messages"][0]["content"][0]
    assert first_block["cache_control"] == {"type": "ephemeral"}   # spec is the cached prefix
    assert "<daily_spec>" in first_block["text"]
    assert "+  17| " in call["messages"][0]["content"][1]["text"]  # line-numbered diff


def test_grounding_guard_drops_hallucinated_lines(use_fake):
    good = Finding(file="app/crud/services.py", line=17, severity=Severity.BLOCKER, category="correctness",
                   title="limit(size - 1) returns one row too few", comment="...")
    ghost = Finding(file="app/crud/services.py", line=240, severity=Severity.MINOR, category="quality",
                    title="ghost", comment="...")
    use_fake(llm_review([good, ghost], verdict="request_changes", confidence=0.9))
    r = review_pr(PAGE_SPEC, read("pr08_pagination_subtle.diff"), use_llm=True)
    assert [f.title for f in r.dropped_findings] == ["ghost"]
    assert any(f.line == 17 and f.source == "llm" for f in r.findings)
    assert r.verdict == "request_changes"


def test_blocker_overrides_llm_approval(use_fake):
    use_fake(llm_review([], verdict="approve", confidence=0.95))
    r = review_pr(PAGE_SPEC, read("pr07_pagination_sqli.diff"), use_llm=True)
    assert r.verdict == "request_changes"  # static SQL-injection blocker wins
    assert r.routing.lane == "senior_escalation"
    assert any("Disagreement" in why for why in r.routing.reasons)


def test_refusal_falls_back_to_offline_and_escalates(use_fake):
    use_fake(None, stop_reason="refusal")
    r = review_pr(PAGE_SPEC, read("pr06_pagination_good.diff"), use_llm=True)
    assert r.reviewer == "heuristic-offline"
    assert r.routing.lane == "senior_escalation"


def test_schema_is_accepted_by_sdk():
    from anthropic.lib._parse._transform import transform_schema  # noqa: PLC0415
    schema = transform_schema(LLMReview)
    assert schema["type"] == "object" and "findings" in schema["properties"]
