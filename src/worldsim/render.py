"""The narrator's view: only what the player character knows (spec 2.3, 2.4).

Rule 5 is enforced here, in code, not asked for in the prompt. The context is
built up from the player's knowledge rows, so a hidden fact they never learned
has no path into the narrator's prompt.

Created by __main__.py and called by turn.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from .budget import as_prompt, estimate_tokens, take_within
from .config import Config
from .llm import _BaseLLM
from .resolve import PLAYER_ID, Line
from .store import Entity, Known, Store

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

WORDS = re.compile(r"[A-Za-z']+")

# A "you <verb>" inside someone else's clause is not the narrator speaking for
# the player: "Gary asks what you want" is Gary's line. The bare pattern above
# flagged those too, and each false positive spends a whole regeneration — a
# second narrator call, and a decent chance of trading good narration for worse.
SOMEONE_ELSE_IS_SPEAKING = frozenset(
    {
        # complementizers: the clause belongs to whoever opened it
        "what", "whatever", "that", "whether", "if", "how", "why", "when",
        "where", "which", "who", "whom", "because", "since", "unless",
        "until", "though", "although", "while", "than", "about", "like",
        # someone else reporting on the player
        "asks", "asked", "tells", "told", "says", "said", "knows", "knew",
        "remembers", "remembered", "sees", "saw", "hears", "heard",
        "watches", "watched", "repeats", "repeated", "wonders", "notices",
        "noticed", "thinks", "believes", "understands", "assumes",
        "expects", "means", "reminds", "reminded", "lets", "doubts",
    }
)

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
    found = []
    for pattern in (PUTS_WORDS_IN_YOUR_MOUTH, CANNOT_BELIEVE):
        for match in pattern.finditer(text):
            before = WORDS.findall(text[: match.start()])
            if before and before[-1].lower() in SOMEONE_ELSE_IS_SPEAKING:
                continue
            found.append(match.group(0))
    return found


class Narrator:
    def __init__(
        self, llm: _BaseLLM, store: Store, config: Config, prompts_dir: Path
    ) -> None:
        self.llm = llm
        self.store = store
        self.config = config
        self.prompts_dir = prompts_dir
        # Contract breaches that survived a regeneration, for the dev view. A
        # repeat offence used to be accepted silently.
        self.breaches: list[str] = []

    def context(
        self,
        intent: str,
        dialogue: list[Line],
        noticed_fact_ids: list[int],
        said_aloud: str | None = None,
        omit_keys: frozenset[str] = frozenset(),
        budget_tokens: int | None = None,
    ) -> dict:
        """What the narrator is allowed to see. Nothing else reaches it.

        `budget_tokens` is what is left for the context after the instructions
        (spec 5.2); None means the role's whole budget.
        """
        known = [k for k in self.store.known(PLAYER_ID) if k.fact.key not in omit_keys]

        player = self.store.entity(PLAYER_ID)
        here = self.store.entity(player.location_id) if player.location_id else None

        # Who and what is here, minus whatever the character cannot make out.
        # `known` is the whole of what they hold, before omit_keys narrows it,
        # because a fact hidden from the opening narration is still one they know.
        known_of = {k.fact.entity_id for k in self.store.known(PLAYER_ID)}
        here_now = [
            e
            for e in (self.store.contents(here.id) if here else [])
            if e.id != PLAYER_ID and self._can_make_out(e, known_of)
        ]
        # People before scenery, so a crowded room drops the furniture.
        here_now.sort(key=lambda e: (e.kind != "person", e.id))
        present = [{"id": e.id, "name": e.name, "kind": e.kind} for e in here_now]

        noticed = set(noticed_fact_ids)
        context = {
            # The resolver's one-line reading of the action, not the player's
            # own words. Handing over the raw text is what defect 2 was: the
            # narrator had the phrasing in front of it and read it back. It
            # cannot echo what it was never given, and `intent` still carries
            # any question that needs answering.
            "you_intended": intent,
            # Also already decided by the resolver, so the narrator never has
            # to invent speech for the player.
            "you_said_aloud": said_aloud,
            "you_are_in": {"id": here.id, "name": here.name} if here else None,
            "you_carry": [
                {"id": e.id, "name": e.name} for e in self.store.contents(PLAYER_ID)
            ],
            "dialogue": [
                {"speaker": self._name(line.speaker_id), "says": line.gist}
                for line in dialogue
            ],
            "noticed_just_now": [
                {"entity_id": k.fact.entity_id, "key": k.fact.key, "value": k.value}
                for k in known
                if k.fact.id in noticed
            ],
        }

        # Spec 5.2's priority order, as far as this milestone needs it. Who is
        # in the room comes before what the character knows about them, and
        # both come before anything learned elsewhere; within each group the
        # oldest goes first. Without this the narrator's prompt carried the
        # character's entire history by turn 500 — money, latency, and the
        # salience of what the narration bothers to mention.
        here_ids = {PLAYER_ID} | ({here.id} if here else set()) | {e.id for e in here_now}
        near = [self._held(k) for k in known if k.fact.entity_id in here_ids]
        far = [self._held(k) for k in known if k.fact.entity_id not in here_ids]

        budget = budget_tokens
        if budget is None:
            budget = self.config.token_budgets["narrator"]
        room = budget - estimate_tokens(context)

        # Most relevant first, which is also the order a model attends in: what
        # is here before what is elsewhere, and the newest before the oldest.
        # It means the end of the list is always the next thing worth dropping.
        context["present"] = take_within(present, max(room, 0))
        room -= estimate_tokens(context["present"])
        kept = take_within(list(reversed(near)), max(room, 0))
        room -= estimate_tokens(kept)
        kept += take_within(list(reversed(far)), room) if room > 0 else []
        context["your_knowledge"] = kept

        # The cascade above estimates; this makes it certain. The JSON overhead
        # of the keys and brackets themselves is small but real, and spec 5.2 is
        # a hard bound rather than a target, so trim for sure — least important
        # first, which is the tail of each list.
        while estimate_tokens(context) > budget:
            if context["your_knowledge"]:
                context["your_knowledge"].pop()
            elif context["present"]:
                context["present"].pop()
            else:
                break  # the frame alone is over; there is nothing left to drop
        return context

    @staticmethod
    def _held(known: Known) -> dict:
        """One thing the character holds. The belief, not the truth under it."""
        return {
            "entity_id": known.fact.entity_id,
            "key": known.fact.key,
            "value": known.value,
            "how_you_know": known.how,
        }

    def _can_make_out(self, entity: Entity, known_of: set[str]) -> bool:
        """Whether being in the room is enough to notice this at all (rule 5).

        A name is as revealing as a fact: "a revolver taped under the sink" was
        reaching the narrator intact, because standing in the room was treated
        as perceiving everything in it. The store has no visibility on entities,
        so this reads the visibility of their facts — something with nothing but
        hidden facts, or no settled facts at all, has not been noticed yet and
        waits for the resolver to put it in `perceived`.
        """
        if entity.id in known_of:
            return True
        return any(
            fact.visibility != "hidden" for fact in self.store.live_facts(entity.id)
        )

    def _name(self, entity_id: str) -> str:
        entity = self.store.entity(entity_id)
        return entity.name if entity else entity_id

    def narrate(
        self,
        intent: str,
        dialogue: list[Line],
        noticed_fact_ids: list[int],
        said_aloud: str | None = None,
        prompt_name: str = "narrator.md",
        omit_keys: frozenset[str] = frozenset(),
    ) -> str:
        """Narrate, and regenerate once if it narrated the player (defect 2)."""
        system = (self.prompts_dir / prompt_name).read_text()
        user = as_prompt(
            self.context(
                intent,
                dialogue,
                noticed_fact_ids,
                said_aloud,
                omit_keys,
                # The instructions are the first claim on the budget (spec 5.2).
                budget_tokens=self.config.token_budgets["narrator"]
                - estimate_tokens(system),
            )
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
