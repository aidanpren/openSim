"""Building resolver replies for tests.

The resolver emits three JSON objects back to back: the opening, the scene,
then the ledger. Tests build them with `reply()` so the shape lives in one
place, and keyword arguments are routed to whichever object owns them.
"""

from __future__ import annotations

import json

OPENING_DEFAULTS = {
    "intent": "do the thing",
    "duration_minutes": 5,
    "player_location": "place:spawn-city",
}
SCENE_DEFAULTS = {"summary": "something happens"}

# Owned only by the scene. Everything else a test passes goes to the opening,
# which is where perception is decided and so where most tests need it.
SCENE_ONLY = frozenset({"summary", "you_said_aloud", "dialogue", "scene_change"})


def reply(ledger: dict | None = None, scene: dict | None = None, **fields) -> str:
    """One resolver reply. Keyword arguments override the defaults.

    `scene={...}` puts facts or entities in the *second* object, for tests that
    care about the two narration beats rather than just the committed truth.
    """
    opening = dict(OPENING_DEFAULTS)
    second = dict(SCENE_DEFAULTS)
    for key, value in fields.items():
        (second if key in SCENE_ONLY else opening)[key] = value
    second.update(scene or {})
    return json.dumps(opening) + json.dumps(second) + json.dumps(ledger or {})
