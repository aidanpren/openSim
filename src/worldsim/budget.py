"""Keeping a call's input inside its budget (spec 5.2).

Spec 5.2's rule is that every call's input stays within its budget whether it
is turn 10 or turn 500. The priority-ordered assembly the spec describes belongs
with the memory tiers in milestone 2. What could not wait is the part that made
the rule simply false: the narrator was handed every fact the character had ever
learned, and the resolver every entity nested in wherever the player happened to
be standing, so both prompts grew for the whole length of a campaign.

Functions only, no state. Called by render.py and resolve.py.
"""

from __future__ import annotations

import json
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")

# The call log already prices a call at four characters to the token, so the
# trimming agrees with the cost it exists to bound. It is an estimate either
# way: the real count comes back from the provider after the fact.
CHARS_PER_TOKEN = 4

# How a context is written on the wire. Pretty-printing a context cost 39% more
# input tokens than this for nothing a model reads, and it also meant a budget
# measured something narrower than what was actually sent.
COMPACT = (",", ":")


def as_prompt(value: object) -> str:
    """A context as it is sent. The one place that decides the wire form."""
    return json.dumps(value, separators=COMPACT, default=str)


def estimate_tokens(value: object) -> int:
    """Roughly what this will cost as prompt text."""
    text = value if isinstance(value, str) else as_prompt(value)
    return len(text) // CHARS_PER_TOKEN


def take_within(
    items: Sequence[T],
    budget_tokens: int,
    size: Callable[[T], int] = estimate_tokens,
) -> list[T]:
    """As many leading items as fit. Callers put the ones that matter first.

    One item always survives, even an oversized one. Returning nothing would
    hand a model an empty slice of the world and let it settle what it had
    already settled, which is worse than one long entry.
    """
    kept: list[T] = []
    spent = 0
    for item in items:
        cost = size(item)
        if kept and spent + cost > budget_tokens:
            break
        kept.append(item)
        spent += cost
    return kept
