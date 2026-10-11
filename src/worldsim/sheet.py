"""The character sheet: a view over the store, never a copy (spec 5.5).

Sections follow the spec. Condition is whatever words the world used for it;
nothing here scores anything, so there are no health bars.

Created by __main__.py and called by the /sheet command.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .resolve import PLAYER_ID, SPAWN_PLACE_ID
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
    """The sheet as spoken-plain text, for /sheet.

    Built from what the character knows, not from the truth. The sheet is
    something the player reads, so it is bound by rule 5 like the narrator is:
    a fact hidden from this character has no business on their own sheet, and
    where they hold a false belief, the belief is what they would tell you.
    """
    facts = {
        held.fact.key: held
        for held in store.known(PLAYER_ID)
        if held.fact.entity_id == PLAYER_ID
    }
    used: set[str] = set()
    lines: list[str] = []

    for heading, keys in SECTIONS:
        rows = []
        for key in keys:
            held = facts.get(key)
            if held is None:
                continue
            used.add(key)
            rows.append(f"  {key.replace('_', ' ')}: {_show(held.value)}")
        if heading == "On you":
            carried = store.contents(PLAYER_ID)
            if carried:
                rows.append("  carrying: " + ", ".join(e.name for e in carried))
        if rows:
            lines.append(heading)
            lines.extend(rows)
            lines.append("")

    threads = [
        f"  {key.replace('_', ' ')}: {_show(held.value)}"
        for key, held in facts.items()
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

    leftover = [
        f"  {key.replace('_', ' ')}: {_show(held.value)}"
        for key, held in facts.items()
        if key not in used
    ]
    if leftover:
        lines.append("Also true of you")
        lines.extend(leftover)
        lines.append("")

    if not lines:
        return "Nothing is settled about you yet."
    return "\n".join(lines).rstrip()


def _people_you_know(store: Store) -> list[tuple[str, str]]:
    """Whom the player knows, from their own knowledge rather than the truth.

    Keyed by id, not name: a world this size will have two people called Gary,
    and grouping by name merged them into one.
    """
    by_person: dict[str, tuple[str, list[str]]] = {}
    for held in store.known(PLAYER_ID):
        entity = store.entity(held.fact.entity_id)
        if entity is None or entity.kind != "person" or entity.id == PLAYER_ID:
            continue
        name, details = by_person.setdefault(entity.id, (entity.name, []))
        details.append(f"{held.fact.key.replace('_', ' ')} {_show(held.value)}")
    return [(name, "; ".join(details[:3])) for name, details in by_person.values()]


def _show(value: str | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def timezone_here(store: Store) -> str | None:
    """The IANA zone of wherever the character is standing, or None.

    Spec 6.1 shows the clock in local time where the character is, so a drive to
    Denver has to change the hour. The zone is settled on whichever place holds
    it — a city, usually, not a kitchen — so this walks outward through the
    containment chain and falls back to the spawn city for a place the model
    left unparented. `seen` guards against a containment cycle, which the store
    permits: it only refuses an entity inside itself.
    """
    entity = store.entity(PLAYER_ID)
    seen: set[str] = set()
    while entity is not None and entity.id not in seen:
        seen.add(entity.id)
        fact = store.live_fact(entity.id, "timezone")
        if fact is not None:
            return str(fact.value)
        entity = store.entity(entity.location_id) if entity.location_id else None
    fallback = store.live_fact(SPAWN_PLACE_ID, "timezone")
    return str(fallback.value) if fallback else None


def local_time(store: Store) -> str:
    """The clock in the local time of wherever the character is (spec 6.1)."""
    now = store.now()
    zone = timezone_here(store)
    if zone:
        try:
            return now.astimezone(ZoneInfo(zone)).strftime(
                "%A, %B %-d, %Y, %-I:%M %p "
            ) + zone
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return now.strftime("%A, %B %-d, %Y, %-I:%M %p UTC")


def time_of_day(store: Store) -> str:
    """The hour in plain words, for the narrator (spec 6.1).

    The narrator was never told what time it was, so it invented one: a live
    spawn opened on "the morning light spills gold" at 1:31 in the afternoon.
    Telling the model to convert UTC itself did not hold — it is arithmetic,
    and code already knows the answer.
    """
    zone = timezone_here(store)
    now = store.now()
    if zone:
        try:
            now = now.astimezone(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    hour = now.hour
    if hour < 5:
        part = "the small hours"
    elif hour < 8:
        part = "early morning"
    elif hour < 12:
        part = "morning"
    elif hour < 14:
        part = "the middle of the day"
    elif hour < 18:
        part = "afternoon"
    elif hour < 21:
        part = "evening"
    else:
        part = "night"
    return f"{now.strftime('%A')} {part}, {now.strftime('%-I:%M %p').lower()}"
