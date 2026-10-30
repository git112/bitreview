"""Minimal unified-diff parser.

We need two things from a PR diff:
  1. the added lines with their line numbers in the NEW file (for static checks + inline comments)
  2. a compact, line-numbered rendering to feed the LLM so it can cite exact lines

Line numbers matter: GitHub's review API rejects comments on lines that are not part of the diff,
and the grounding guard uses this map to drop hallucinated locations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    path: str
    is_new: bool = False
    is_deleted: bool = False
    added: dict[int, str] = field(default_factory=dict)     # new_line_no -> text
    context: dict[int, str] = field(default_factory=dict)   # new_line_no -> text (unchanged lines shown in hunk)
    removed_count: int = 0

    @property
    def is_test(self) -> bool:
        p = self.path.lower()
        return "/tests/" in f"/{p}" or p.split("/")[-1].startswith("test_") or p.endswith("_test.py") or ".spec." in p or ".test." in p


@dataclass
class ParsedDiff:
    files: list[FileDiff]

    def file(self, path: str) -> FileDiff | None:
        return next((f for f in self.files if f.path == path), None)

    def is_commentable(self, path: str, line: int) -> bool:
        f = self.file(path)
        return f is not None and line in f.added

    @property
    def added_lines(self) -> int:
        return sum(len(f.added) for f in self.files)

    @property
    def removed_lines(self) -> int:
        return sum(f.removed_count for f in self.files)

    def render_for_llm(self, max_chars: int = 60_000) -> str:
        """Render as `path` blocks with `+NNN|` (added) and ` NNN|` (context) prefixes."""
        out: list[str] = []
        for f in self.files:
            status = " (new file)" if f.is_new else " (deleted)" if f.is_deleted else ""
            out.append(f"### {f.path}{status}")
            lines = sorted({**f.context, **f.added}.items())
            for no, text in lines:
                marker = "+" if no in f.added else " "
                out.append(f"{marker}{no:>4}| {text}")
            out.append("")
        rendered = "\n".join(out)
        if len(rendered) > max_chars:
            # Never silently truncate: callers check this flag and route the PR to a human.
            raise DiffTooLargeError(len(rendered), max_chars)
        return rendered


class DiffTooLargeError(Exception):
    def __init__(self, size: int, limit: int):
        super().__init__(f"Rendered diff is {size} chars, limit {limit}; route to a human reviewer")
        self.size, self.limit = size, limit


def parse_unified_diff(text: str) -> ParsedDiff:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    new_no = 0
    pending_old = ""

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            cur = None
            continue
        if raw.startswith("--- "):
            pending_old = raw[4:].strip()
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path == "/dev/null":
                path = pending_old[2:] if pending_old.startswith("a/") else pending_old
                cur = FileDiff(path=path, is_deleted=True)
            else:
                path = path[2:] if path.startswith("b/") else path
                cur = FileDiff(path=path, is_new=pending_old == "/dev/null")
            files.append(cur)
            continue
        m = HUNK_RE.match(raw)
        if m and cur is not None:
            new_no = int(m.group(3))
            continue
        if cur is None:
            continue
        if raw.startswith("+"):
            cur.added[new_no] = raw[1:]
            new_no += 1
        elif raw.startswith("-"):
            cur.removed_count += 1
        elif raw.startswith(" ") or raw == "":
            cur.context[new_no] = raw[1:] if raw else ""
            new_no += 1
        # "\ No newline at end of file" and other markers are ignored

    return ParsedDiff(files=files)
