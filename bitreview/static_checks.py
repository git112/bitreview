"""Deterministic checks that run before (and independently of) the LLM.

Why keep a rule layer when we have an LLM?
  * It is free, instant and 100% reproducible - good for the cheap "first pass" on every PR.
  * Security blockers (hard-coded secrets, SQL injection) must never depend on model mood.
  * Disagreement between rules and the LLM is a strong signal to escalate to a senior mentor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch

from .diff_parser import ParsedDiff
from .schemas import Finding, Severity, TaskSpec


@dataclass(frozen=True)
class LineRule:
    id: str
    pattern: re.Pattern
    severity: Severity
    category: str
    title: str
    comment: str
    suggestion: str = ""
    only_ext: tuple[str, ...] = (".py",)
    skip_tests: bool = True


RULES: list[LineRule] = [
    LineRule(
        "hardcoded-secret",
        # `NAME = "literal"` assignments or `"name": "literal"` dict entries for secret-like names.
        # Deliberately not keyword args, so `tokenUrl="/auth/login"` / `token_type="bearer"` don't fire.
        re.compile(
            r"""(?i)(^\s*\w*(secret|passw(or)?d|api_?key|private_key|access_token)\w*\s*(:\s*\w+\s*)?=\s*["'][^"'\s]{6,}["']"""
            r"""|["']\w*(secret|passw(or)?d|api_?key)\w*["']\s*:\s*["'][^"'\s]{6,}["'])"""
        ),
        Severity.BLOCKER, "security", "Hard-coded secret",
        "Secrets committed to git are public forever (even after you delete them). Anyone with repo access can forge tokens.",
        "Read it from configuration, e.g. `SECRET_KEY = os.environ[\"JWT_SECRET\"]` or a pydantic `Settings` class.",
    ),
    LineRule(
        "sql-injection",
        re.compile(r"""(?i)(execute|text)\(\s*f["'].*\b(select|insert|update|delete)\b|["']\s*(select|insert|update|delete)\b.*["']\s*(%|\+|\.format\()"""),
        Severity.BLOCKER, "security", "SQL built with string formatting",
        "Interpolating user input into SQL allows SQL injection.",
        "Use bound parameters: `db.execute(text(\"SELECT ... WHERE email = :email\"), {\"email\": email})` or the ORM query API.",
    ),
    LineRule(
        "plaintext-password",
        re.compile(r"""(?i)(password\s*==\s*\w*\.?password|\.password\s*=\s*\w*\.?password\b(?!_hash))"""),
        Severity.BLOCKER, "security", "Password compared or stored in plain text",
        "Passwords must be hashed (bcrypt/argon2) and compared with a constant-time verify function.",
        "Use `passlib`'s `CryptContext(schemes=[\"bcrypt\"])` - `hash()` on signup, `verify()` on login.",
    ),
    LineRule(
        "jwt-no-verify",
        re.compile(r"""(?i)jwt\.decode\(.*(verify_signature["']?\s*:\s*False|verify\s*=\s*False)"""),
        Severity.BLOCKER, "security", "JWT decoded without signature verification",
        "Skipping signature verification means any client can craft an admin token.",
        "Call `jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])` and handle `JWTError`.",
    ),
    LineRule(
        "jwt-no-exp",
        re.compile(r"""jwt\.encode\(\s*\{(?![^}]*["']exp["'])[^}]*\}"""),
        Severity.MAJOR, "security", "Token issued without expiry",
        "A JWT without `exp` is valid forever; a leaked token can never be revoked.",
        "Add `\"exp\": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` to the payload.",
    ),
    LineRule(
        "bare-except",
        re.compile(r"^\s*except\s*:\s*(#.*)?$|^\s*except\s+Exception\s*:\s*\n?\s*pass"),
        Severity.MAJOR, "correctness", "Bare `except:` swallows every error",
        "This also catches `KeyboardInterrupt`/`SystemExit` and hides real bugs - the opposite of what Week 2 root-cause analysis teaches.",
        "Catch the specific exception you expect (e.g. `except JWTError:`) and log or re-raise.",
    ),
    LineRule(
        "debug-print",
        re.compile(r"^\s*print\("),
        Severity.MINOR, "hygiene", "Leftover `print()`",
        "Debug prints end up in production logs and leak data.",
        "Use the `logging` module (`logger = logging.getLogger(__name__)`) or remove it.",
    ),
    LineRule(
        "todo-left",
        re.compile(r"#\s*(TODO|FIXME|HACK)\b", re.I),
        Severity.MINOR, "hygiene", "TODO left in submitted code",
        "Daily PRs should be mergeable; open a follow-up issue instead of leaving TODOs.",
        skip_tests=False,
    ),
    LineRule(
        "mutable-default",
        re.compile(r"def \w+\(.*=\s*(\[\]|\{\})"),
        Severity.MINOR, "correctness", "Mutable default argument",
        "Default `[]`/`{}` is shared across calls and causes state to leak between requests.",
        "Use `None` as default and create the list/dict inside the function.",
    ),
    LineRule(
        "redis-keys",
        re.compile(r"""\.keys\(\s*["'][^"']*\*"""),
        Severity.MAJOR, "performance", "`KEYS pattern*` blocks Redis",
        "`KEYS` is O(N) and blocks the whole Redis server - fine on a laptop, an outage in production.",
        "Use `scan_iter(match=...)` or keep an index set of keys to invalidate.",
    ),
    LineRule(
        "cache-no-ttl",
        re.compile(r"""\b(r|redis|rds|client|cache|self\.client|self\.redis)\.set\((?!.*\b(ex|px|exat|pxat|keepttl)\s*=)"""),
        Severity.MAJOR, "performance", "Cache entry written without TTL",
        "Entries without expiry grow memory without bound and serve stale data forever.",
        "Pass `ex=` seconds: `redis.set(key, value, ex=CACHE_TTL_SECONDS)`.",
    ),
    LineRule(
        "assert-true",
        re.compile(r"^\s*assert\s+(True|1)\s*$"),
        Severity.MAJOR, "testing", "Test asserts nothing",
        "`assert True` always passes, so the test cannot catch a regression.",
        "Assert on behaviour: status code, response body, side effects.",
        skip_tests=False,
    ),
]

MAX_ADDED_LINES = 400


def run_static_checks(diff: ParsedDiff, spec: TaskSpec) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []

    for f in diff.files:
        if f.is_deleted:
            continue
        for no, text in f.added.items():
            for rule in RULES:
                if rule.only_ext and not f.path.endswith(rule.only_ext):
                    continue
                if rule.skip_tests and f.is_test:
                    continue
                if rule.pattern.search(text):
                    findings.append(Finding(
                        file=f.path, line=no, severity=rule.severity, category=rule.category,
                        title=rule.title, comment=rule.comment, suggestion=rule.suggestion, source="static",
                    ))

    # ---- PR-level checks, anchored on the first added line of the first changed file
    anchor = next(((f.path, min(f.added)) for f in diff.files if f.added), None)
    code_files = [f for f in diff.files if not f.is_test and f.path.endswith(".py") and f.added]
    test_files = [f for f in diff.files if f.is_test and f.added]
    off_target = [
        f.path for f in diff.files
        if spec.file_targets and not any(fnmatch(f.path, pat) for pat in spec.file_targets)
    ]

    if anchor and code_files and not test_files:
        findings.append(Finding(
            file=anchor[0], line=anchor[1], severity=Severity.MAJOR, category="testing",
            title="No tests added for new behaviour",
            comment="Every daily spec ships with test steps. Changing application code without a test means the mentor cannot verify the acceptance criteria.",
            suggestion="Add a pytest case under `tests/` that covers the happy path and one failure path.",
            source="static",
        ))
    if anchor and off_target:
        findings.append(Finding(
            file=anchor[0], line=anchor[1], severity=Severity.MINOR, category="spec",
            title="Files changed outside today's file targets",
            comment=f"Today's spec targets {', '.join(spec.file_targets)}, but this PR also touches: {', '.join(off_target)}. Scope creep makes review slower.",
            suggestion="Move unrelated changes into a separate branch/PR.",
            source="static",
        ))
    if anchor and diff.added_lines > MAX_ADDED_LINES:
        findings.append(Finding(
            file=anchor[0], line=anchor[1], severity=Severity.MINOR, category="hygiene",
            title=f"Large PR ({diff.added_lines} added lines)",
            comment="Daily PRs are meant to be small and reviewable.", source="static",
        ))

    stats = {
        "files_changed": len(diff.files),
        "added_lines": diff.added_lines,
        "removed_lines": diff.removed_lines,
        "test_files_changed": len(test_files),
        "off_target_files": off_target,
    }
    return findings, stats
