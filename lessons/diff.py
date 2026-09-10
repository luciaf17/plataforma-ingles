"""Word-level diff between what the learner wrote and the corrected text,
rendered server-side (spec 5.4: never trust the model to produce HTML)."""

import difflib
import re

from django.utils.html import escape
from django.utils.safestring import mark_safe

_token_re = re.compile(r"\s+|[^\s\w]|\w+", re.UNICODE)


def tokenize(text):
    return _token_re.findall(text or "")


def diff_html(original, corrected):
    """<del> for removed words, <ins> for added ones, plain text otherwise."""
    a, b = tokenize(original), tokenize(corrected)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(escape("".join(a[i1:i2])))
        elif op == "delete":
            out.append(f"<del>{escape(''.join(a[i1:i2]))}</del>")
        elif op == "insert":
            out.append(f"<ins>{escape(''.join(b[j1:j2]))}</ins>")
        else:  # replace
            out.append(f"<del>{escape(''.join(a[i1:i2]))}</del><ins>{escape(''.join(b[j1:j2]))}</ins>")
    html = "".join(out).replace("\n", "<br>")
    return mark_safe(html)


def change_count(original, corrected):
    a, b = tokenize(original), tokenize(corrected)
    return sum(1 for op, *_ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op != "equal")
