"""Building resolver replies for tests.

The resolver emits three JSON objects back to back: the opening, the scene,
then the ledger. Tests build them with `reply()` so the shape lives in one
place, and keyword arguments are routed to whichever object owns them.
"""

from __future__ import annotations

import json

# A life opens in the middle of something, so a spawn that settles nothing
# hidden and puts nothing on the clock is refused (spec 9, resolve.check_spawn).
# Every test spawn carries these, because every real one has to.
IN_MOTION_SECRETS = [
    {
        "entity_id": "person:you",
        "key": "unserved_warrant",
        "value": "a bench warrant from a missed court date in Topeka",
        "source": "invented",
    },
    {
        "entity_id": "person:you",
        "key": "landlord_decided",
        "value": "the landlord has already promised the unit to his nephew",
        "source": "invented",
    },
]
IN_MOTION_SCHEDULE = [
    [40, "the shift ends and the keys have to go back", False],
    [180, "Dory comes by for his money", True],
]

OPENING_DEFAULTS = {
    "intent": "do the thing",
    "duration_minutes": 5,
    "player_location": "place:spawn-city",
}
SCENE_DEFAULTS = {"summary": "something happens"}

# Owned only by the scene. Everything else a test passes goes to the opening,
# which is where perception is decided and so where most tests need it.
SCENE_ONLY = frozenset({"summary", "you_said_aloud", "dialogue", "scene_change"})


FILLER = "Nothing else moves."


def scripts(resolver, narrator, room: int = 8) -> dict:
    """FakeLLM scripts, with room for the beat every turn opens on.

    A turn makes two narrator calls: the attempt, spoken before the resolver is
    called at all, and the outcome. Tests care about the wording of one or the
    other, rarely both, so the queue is padded rather than every test spelling
    out a line of boilerplate. Running the queue dry is still an assertion
    failure in the resolver, where it means something real.
    """
    return {
        "resolver": list(resolver),
        "narrator": list(narrator) + [FILLER] * room,
    }


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
