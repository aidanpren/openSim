"""Building resolver replies for tests.

The resolver emits two JSON objects back to back: the scene, then the ledger.
Tests build them with `reply()` so the shape lives in one place.
"""

from __future__ import annotations

import json

SCENE_DEFAULTS = {
    "intent": "do the thing",
    "duration_minutes": 5,
    "player_location": "place:spawn-city",
    "summary": "something happens",
}


def reply(ledger: dict | None = None, **scene) -> str:
    """One resolver reply. Keyword arguments override the scene defaults."""
    body = dict(SCENE_DEFAULTS)
    body.update(scene)
    return json.dumps(body) + json.dumps(ledger or {})
