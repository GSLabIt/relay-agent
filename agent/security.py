"""Small security helpers shared by command and transport layers."""

from __future__ import annotations

import re

_SECRET_RE = re.compile(
    r"(?i)(bearer\s+|"
    r"(?:token|password|passwd|secret|api[_-]?key)[\"']?\s*[=:]\s*[\"']?)"
    r"[^\s\"',}]+"
)


def redact(text: str) -> str:
    """Best-effort scrub of common secret-bearing fragments."""
    return _SECRET_RE.sub(r"\1***", text)
