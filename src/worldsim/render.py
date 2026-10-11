"""The narrator's view: only what the player character knows (spec 2.3, 2.4).

Rule 5 is enforced here, in code, not asked for in the prompt. The context is
built up from the player's knowledge rows, so a hidden fact they never learned
has no path into the narrator's prompt.

Created by __main__.py and called by turn.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from .llm import _BaseLLM
from .resolve import PLAYER_ID, Line
from .store import Store

# Prompts have not held this line, so it is checked in code. Speech and
# interior states only: "you feel the cold" is a sensation the world caused and
# is allowed, and so is "you walk in", which is the world confirming an action.
PUTS_WORDS_IN_YOUR_MOUTH = re.compile(
    r"\byou(?:'re|'ve)?\s+(?:"
    r"say|says|said|tell|tells|told|ask|asks|asked|reply|replies|replied|"
    r"answer|answers|answered|shout|shouts|shouted|yell|yells|yelled|"
    r"mutter|mutters|muttered|whisper|whispers|whispered|explain|explains|"
    r"explained|admit|admits|admitted|insist|insists|insisted|"
    r"decide|decides|decided|think|thinks|thought|realize|realizes|realized|"
    r"wonder|wonders|wondered|remember|remembers|remembered|hope|hopes|hoped|"
    r"want|wants|wanted|choose|chooses|chose|believe|believes|believed"
    r")\b",
    re.IGNORECASE,
)
CANNOT_BELIEVE = re.compile(r"\byou\s+can'?t\s+believe\b", re.IGNORECASE)

# Kept out of the opening narration, which otherwise recites them as a stat
# block. They stay on /sheet, where a player can go looking for them.
BIOGRAPHY = frozenset(
    {"name", "age", "job", "history", "backstory", "cash_usd", "bank_usd", "home"}
)

CORRECTION = (
    "\n\nThat narration broke the contract: {found}. Never narrate the "
    "player's words, choices, thoughts, or feelings. Say only what happens "
    "around them and what they perceive. Write it again."
)


def narrated_the_player(text: str) -> list[str]:
    """Phrases that put words or thoughts in the player's mouth."""
    found = [match.group(0) for match in PUTS_WORDS_IN_YOUR_MOUTH.finditer(text)]
    found += [match.group(0) for match in CANNOT_BELIEVE.finditer(text)]
    return found


class Narrator:
    def __init__(self, llm: _BaseLLM, store: Store, prompts_dir: Path) -> None:
        self.llm = llm
        self.store = store
        self.prompts_dir = prompts_dir
        # Contract breaches that survived a regeneration, for the dev view. A
        # repeat offence used to be accepted silently.
        self.breaches: list[str] = []

    def context(
        self,
        player_said: str,
        dialogue: list[Line],
        noticed_fact_ids: list[int],
        said_aloud: str | None = None,
        omit_keys: frozenset[str] = frozenset(),
    ) -> dict:
        """What the narrator is allowed to see. Nothing else reaches it."""
        known = [k for k in self.store.known(PLAYER_ID) if k.fact.key not in omit_keys]

        player = self.store.entity(PLAYER_ID)
        here = self.store.entity(player.location_id) if player.location_id else None

        # Who and what is here. Being in the room is itself something the
        # character perceives, so names are allowed; their facts are not,
        # unless those facts are in the character's knowledge below.
        present = [
            {"id": e.id, "name": e.name, "kind": e.kind}
            for e in (self.store.contents(here.id) if here else [])
            if e.id != PLAYER_ID
        ]

        noticed = set(noticed_fact_ids)
        return {
            "you_said": player_said,
            # Already decided by the resolver, so the narrator never invents it.
            "you_said_aloud": said_aloud,
            "you_are_in": {"id": here.id, "name": here.name} if here else None,
            "present": present,
            "you_carry": [
                {"id": e.id, "name": e.name} for e in self.store.contents(PLAYER_ID)
            ],
            # Beliefs, not truth: Known.value prefers the belief when one is held.
            "your_knowledge": [
                {
                    "entity_id": k.fact.entity_id,
                    "key": k.fact.key,
                    "value": k.value,
                    "how_you_know": k.how,
                }
                for k in known
            ],
            "noticed_just_now": [
                {"entity_id": k.fact.entity_id, "key": k.fact.key, "value": k.value}
                for k in known
                if k.fact.id in noticed
            ],
            "dialogue": [
                {"speaker": self._name(line.speaker_id), "says": line.gist}
                for line in dialogue
            ],
        }

    def _name(self, entity_id: str) -> str:
        entity = self.store.entity(entity_id)
        return entity.name if entity else entity_id

    def narrate(
        self,
        player_said: str,
        dialogue: list[Line],
        noticed_fact_ids: list[int],
        said_aloud: str | None = None,
        prompt_name: str = "narrator.md",
        omit_keys: frozenset[str] = frozenset(),
    ) -> str:
        """Narrate, and regenerate once if it narrated the player (defect 2)."""
        import json

        system = (self.prompts_dir / prompt_name).read_text()
        user = json.dumps(
            self.context(player_said, dialogue, noticed_fact_ids, said_aloud, omit_keys),
            indent=2,
        )

        first = self.llm.text("narrator", system, user)
        broke = narrated_the_player(first)
        if not broke:
            return first

        # One regeneration only: a still-imperfect line beats no narration.
        second = self.llm.text(
            "narrator",
            system,
            user + CORRECTION.format(found=", ".join(sorted(set(broke)))),
        )
        still = narrated_the_player(second)
        if still:
            self.breaches.append(
                f"{', '.join(sorted(set(still)))} survived a regeneration"
            )
        return second
