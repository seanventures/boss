"""Read a function's CODE, not the prose wrapped around it.

Three separate tests in one session asserted something about an implementation and matched the
COMMENT that explains the bug rather than the code that contains it:

  * `assert src.index("attempt_ceiling") < src.index("not fix_note")` — matched the comment
    describing why `not fix_note` was the wrong guard.
  * `assert 'findall(r"\\band\\b"' not in src` — matched the docstring quoting the old regex.
  * `assert "(agent reached its step limit...)" not in src` — matched the comment quoting the
    old message.

Every one of them failed loudly, which is the good case. The bad case is the same mistake in a
test that PASSES: an assertion satisfied by a comment proves the characters exist somewhere near
each other, not that the code does anything. That is I26 — a conclusion checked against the
shape of the text rather than its behaviour — and writing a comment that mentions the old
behaviour is exactly when it bites.

Prefer a behavioural assertion. When only source will do (checking ORDER, or that a call site
was wired), read it through here.
"""

from __future__ import annotations

import ast
import inspect
import io
import tokenize
from typing import Any


def code_only(obj: Any) -> str:
    """Source for `obj` with comments and docstrings removed.

    Falls back to the raw source if the fragment cannot be tokenised — a test that cannot read
    the source at all should fail on its real assertion, not on this helper.
    """
    src = obj if isinstance(obj, str) else inspect.getsource(obj)

    try:
        out, last_end = [], (1, 0)
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            start_row, start_col = tok.start
            if start_row > last_end[0]:
                out.append("\n" * (start_row - last_end[0]))
                last_end = (start_row, 0)
            if start_col > last_end[1]:
                out.append(" " * (start_col - last_end[1]))
            out.append(tok.string)
            last_end = tok.end
        stripped = "".join(out)
    except (tokenize.TokenError, IndentationError):
        stripped = src

    return _without_docstrings(stripped)


def _without_docstrings(src: str) -> str:
    """Drop docstring literals. Parsed rather than regexed: a docstring is a string in
    statement position, which no regex can identify without reimplementing the tokenizer."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        try:
            tree = ast.parse(inspect.cleandoc(src))
        except SyntaxError:
            return src  # a fragment we cannot parse; the caller's assertion still runs

    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spans.append((first.lineno, getattr(first, "end_lineno", first.lineno)))

    if not spans:
        return src
    drop = {n for start, end in spans for n in range(start, end + 1)}
    return "\n".join(
        line for i, line in enumerate(src.splitlines(), 1) if i not in drop
    )


def prompt_text(obj: Any) -> str:
    """Source with adjacent string literals JOINED, as the interpreter would.

    A long system prompt is written as concatenated literals, so a phrase the model plainly
    reads — "this is a BOSS framework bug" — is split across two source lines as
    `"this is a BOSS "` and `"framework bug"` and never appears contiguously in the file. An
    assertion about the PROMPT must therefore be made against the prompt, not the source.

    This is the same mistake as matching a comment (see `code_only`), one level subtler: the
    text is real, the code is right, and the test is reading a representation that never
    reaches the model.
    """
    import re

    src = obj if isinstance(obj, str) else inspect.getsource(obj)
    # `"...."  \n   "...."`  ->  `"........"`  (join implicit concatenation)
    joined = re.sub(r'"\s*\n\s*"', "", src)
    # unescape the quoting used inside those literals
    return joined.replace('\\"', '"').replace("\\n", "\n")
