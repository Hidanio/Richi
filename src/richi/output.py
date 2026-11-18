"""CLI JSON presentation shared by stdout and response-budget accounting.

The default stays pretty for API/server callers. A CLI invocation selects its
own context, so nested calls and concurrent requests cannot change each other's
format. Stored artifacts and internal work limits use an explicit format.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import shlex


_compact_json = ContextVar("richi_compact_json", default=False)


@contextmanager
def json_mode(*, compact=False):
    token = _compact_json.set(compact)
    try:
        yield
    finally:
        _compact_json.reset(token)


def shell_word(value):
    """One word of a command an answer suggests, for a POSIX shell or zsh.

    zsh also expands a word that begins with "=", which shlex leaves bare.
    """
    quoted = shlex.quote(value)
    return "'" + value + "'" if quoted == value and value.startswith("=") else quoted


def rendered(value, *, compact=None):
    """Return the exact JSON stdout, including its single final newline."""
    if compact is None:
        compact = _compact_json.get()
    if compact:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
