"""The character sheet: a view over the store, never a copy (spec 5.5).

Sections follow the spec. Condition is whatever words the world used for it;
nothing here scores anything, so there are no health bars.

Created by __main__.py and called by the /sheet command.
"""

from __future__ import annotations

from .resolve import PLAYER_ID
from .store import Store

# Known keys, grouped. Anything the world settles under another key still shows,
# under "Also true of you", so the sheet never silently hides a fact.
SECTIONS = (
    ("Who you are", ("name", "age", "job", "history", "backstory")),
    ("On you", ("wearing", "carrying")),
    ("Yours elsewhere", ("home", "vehicle", "property", "stash", "account")),
    ("Money", ("cash_usd", "bank_usd")),
    ("Condition", ("condition", "injuries", "illness", "fatigue", "hunger", "intoxication")),
)

THREAD_HINTS = ("debt", "owes", "owed", "deadline", "promise", "rent", "due")


def character_sheet(store: Store) -> str:
    """The sheet as spoken-plain text, for /sheet."""
    facts = {f.key: f for f in store.live_facts(PLAYER_ID)}
    used: set[str] = set()
    lines: list[str] = []

    for heading, keys in SECTIONS:
        rows = []
        for key in keys:
            fact = facts.get(key)
            if fact is None:
                continue
            used.add(key)
            rows.append(f"  {key.replace('_', ' ')}: {_show(fact.value)}")
        if heading == "On you":
            carried = store.contents(PLAYER_ID)
            if carried:
                rows.append("  carrying: " + ", ".join(e.name for e in carried))
        if rows:
            lines.append(heading)
            lines.extend(rows)
            lines.append("")

    threads = [
        f"  {f.key.replace('_', ' ')}: {_show(f.value)}"
        for key, f in facts.items()
        if key not in used and any(hint in key.lower() for hint in THREAD_HINTS)
    ]
    if threads:
        used.update(k for k in facts if any(h in k.lower() for h in THREAD_HINTS))
        lines.append("Open threads")
        lines.extend(threads)
        lines.append("")

    people = _people_you_know(store)
    if people:
        lines.append("People")
        lines.extend(f"  {name}: {detail}" for name, detail in people)
        lines.append("")

    leftover = [f"  {k.replace('_', ' ')}: {_show(f.value)}" for k, f in facts.items() if k not in used]
    if leftover:
        lines.append("Also true of you")
        lines.extend(leftover)
        lines.append("")

    if not lines:
        return "Nothing is settled about you yet."
    return "\n".join(lines).rstrip()


def _people_you_know(store: Store) -> list[tuple[str, str]]:
    """Whom the player knows, from their own knowledge rather than the truth."""
    by_person: dict[str, list[str]] = {}
    for held in store.known(PLAYER_ID):
        entity = store.entity(held.fact.entity_id)
        if entity is None or entity.kind != "person" or entity.id == PLAYER_ID:
            continue
        by_person.setdefault(entity.name, []).append(
            f"{held.fact.key.replace('_', ' ')} {_show(held.value)}"
        )
    return [(name, "; ".join(details[:3])) for name, details in by_person.items()]


def _show(value: str | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
