"""Shared regex-title-matching helper used by both app/rules.py (drop
patterns) and app/normalizer.py (commentary/SDH/original/dubbed patterns) —
same "first pattern that matches a track title" mechanism in both systems.
"""

from __future__ import annotations

import re


def matches_any_pattern(title: str | None, patterns: list[str]) -> str | None:
    if not title:
        return None
    for pattern in patterns:
        try:
            if re.search(pattern, title, re.IGNORECASE):
                return pattern
        except re.error:
            continue
    return None
