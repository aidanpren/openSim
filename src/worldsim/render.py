"""The narrator's view: only what the player character knows (spec 2.3, 2.4).

Rule 5 is enforced here, in code, not asked for in the prompt. The context is
built up from the player's knowledge rows, so a hidden fact they never learned
has no path into the narrator's prompt.

Created by __main__.py and called by turn.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .budget import SEAM, as_prompt, estimate_tokens, take_within
from .config import Config
from .llm import StopNarration, _BaseLLM
from .resolve import PLAYER_ID, Line
from .sheet import time_of_day
from .store import Entity, Known, Store

# Prompts have not held this line, so it is checked in code. What it catches is
# narrowly what the player did not give: words they never said, choices they
# never made, feelings nobody read off them. The action they *did* declare is
# the narrator's job to tell — "you muster your strength and jump onto the
# table" is the voice this is for, not a breach — and a sensation the world
# causes, like cold or pain, was always allowed.
#
# This used to be one pattern, and it flagged every narration of the player's
# speech. So "You say you'll have a beer" was blocked even on a turn where the
# player had said exactly that, and the contract read as "never describe the
# player's action at all".
INVENTED_SPEECH = re.compile(
    r"\byou(?:'re|'ve)?\s+(?:"
    r"say|says|said|tell|tells|told|ask|asks|asked|reply|replies|replied|"
    r"answer|answers|answered|shout|shouts|shouted|yell|yells|yelled|"
    r"mutter|mutters|muttered|whisper|whispers|whispered|explain|explains|"
    r"explained|admit|admits|admitted|insist|insists|insisted"
    r")\b",
    re.IGNORECASE,
)

# Always a breach unless the player's own words asked for it: these are the
# inside of someone else's head, and the one thing the vision reserves for the
# player is what their character decides, thinks, and feels.
MIND_AND_CHOICE = re.compile(
    r"\byou(?:'re|'ve)?\s+(?:"
    r"decide|decides|decided|think|thinks|thought|realize|realizes|realized|"
    r"wonder|wonders|wondered|remember|remembers|remembered|hope|hopes|hoped|"
    r"want|wants|wanted|choose|chooses|chose|believe|believes|believed|"
    # Chosen gestures and read-off feelings. "You sigh" went out live on a turn
    # where the player had said nothing of the kind. A sensation the world
    # causes — shivering, flinching, a racing heart — is still allowed, so
    # those are deliberately not here.
    r"sigh|sighs|sighed|shrug|shrugs|shrugged|nod|nods|nodded|"
    r"smile|smiles|smiled|grin|grins|grinned|frown|frowns|frowned|"
    r"hesitate|hesitates|hesitated"
    r")\b",
    re.IGNORECASE,
)
CANNOT_BELIEVE = re.compile(r"\byou\s+can'?t\s+believe\b", re.IGNORECASE)

WORDS = re.compile(r"[A-Za-z']+")

# Enough of a stemmer to tell whether the player used the verb the narrator
# just used. "I nod at him" has to make "you nod at him" legal, or the gate
# refuses the one thing step 2b asks the narrator to do.
IRREGULAR = {
    "said": "say", "says": "say", "told": "tell", "tells": "tell",
    "thought": "think", "thinks": "think", "chose": "choose",
    "chooses": "choose", "replies": "reply", "replied": "reply",
}


def _stem(word: str) -> str:
    plain = word.lower().strip("'")
    if plain in IRREGULAR:
        return IRREGULAR[plain]
    for suffix in ("ing", "ied", "ed", "es", "s"):
        if plain.endswith(suffix) and len(plain) - len(suffix) >= 3:
            plain = plain[: -len(suffix)]
            break
    if len(plain) > 3 and plain[-1] == plain[-2]:
        plain = plain[:-1]  # nodd -> nod, grinn -> grin
    if len(plain) > 4 and plain.endswith("e"):
        plain = plain[:-1]  # circle -> circl, to meet circling
    return plain


# Words too common to say two sentences are the same thing. Without this the
# near-duplicate score is carried by "the" and "a".
STOPWORDS = frozenset(
    {"a", "an", "the", "of", "in", "on", "at", "to", "into", "from", "with",
     "and", "but", "or", "as", "is", "are", "was", "were", "be", "it", "its",
     "you", "your", "yours", "there", "here", "that", "this", "s", "t"}
)

# Tuned on the pair finding 5 left open: "the fluorescent light above flickers"
# against "the fluorescent light above the sink flickers" scores 0.80, and
# sentences that genuinely say different things about the same room score well
# under half. Exact repeats were already caught; paraphrases were not.
SAME_THING_SAID_TWICE = 0.7
SENTENCES_REMEMBERED = 6

# A sentence that says nothing the earlier one didn't is a repeat however it
# scores. "You turn in the driver's seat and look back through the cab at the
# gurney" followed by "You look back through the cab at the gurney" scores 0.63
# — under the threshold, because the longer sentence dilutes the overlap — and
# went out live, one beat after the other. Three content words is the floor, so
# "the door opens" after a long sentence about the door still counts and "the
# fridge hums" is left alone.
NOTHING_NEW_IN_IT = 3

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

REPEATED = (
    "\n\nEvery sentence of that had already been spoken aloud to this player. "
    "They heard it. Say something else: what is different now, what they have "
    "turned their attention to, or a detail of this place they have not been "
    "told. One or two sentences. Do not describe the room again."
)

# Both of the above are appended to a prompt that was already budgeted, so the
# room they need has to be held back before the context is filled (spec 5.2).
RETRY_ROOM = max(len(CORRECTION), len(REPEATED)) // 4 + 1


# A sentence ends at .?! followed by a space or the end of the text. The decimal
# in "$18.50" and the dot in "Mr." do not end one, so a terminator has to be
# followed by whitespace and then something that starts a sentence.
SENTENCE_END = re.compile(r'(?<=[.!?])["\')\]]*\s+(?=[A-Z"\'(—])')

# "Mr. Brackett" would otherwise be two sentences, which a voice would read with
# a pause in the wrong place.
ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "lt", "sgt", "capt", "no", "vs"}
)


def _split_sentence(buffer: str) -> tuple[str | None, str]:
    """The first complete sentence in `buffer`, and what is left after it.

    Sentence granularity is what TTS wants anyway: a clause carries its own
    prosody, where a word-by-word feed does not. Returns (None, buffer) until a
    whole one has arrived.
    """
    for match in SENTENCE_END.finditer(buffer):
        head = buffer[: match.start()]
        last = WORDS.findall(head)
        if last and head.endswith(".") and last[-1].lower() in ABBREVIATIONS:
            continue
        return head.strip(), buffer[match.end() :]
    return None, buffer


def sentences_in(text: str | None) -> list[str]:
    """Every sentence in `text`, in order."""
    out, buffer = [], text or ""
    while True:
        sentence, buffer = _split_sentence(buffer)
        if sentence is None:
            break
        out.append(sentence)
    if buffer.strip():
        out.append(buffer.strip())
    return out


def _content(sentence: str) -> frozenset[str]:
    """What a sentence is actually about, for comparing it with another.

    Stemmed, because the two sentences that say the same thing rarely inflect
    it the same way. "You step out onto the pavement, circling the vehicle"
    against "You step out onto the pavement and circle the vehicle" differed by
    `circling` against `circle` alone, and went out one beat after the other.
    """
    return frozenset(
        _stem(word)
        for word in WORDS.findall(sentence.lower())
        if word not in STOPWORDS
    )


class Heard:
    """The last few sentences the player has actually been told.

    Exact repeats were already filtered; paraphrases were not, which is how
    "the fluorescent light above flickers" came back as "the fluorescent light
    above the sink flickers" a turn later. This compares what two sentences are
    about rather than their wording, and it is deterministic — no model call
    sits between a sentence and the speaker.
    """

    def __init__(self, spoken: list[str] | tuple[str, ...] = ()) -> None:
        self.said: list[frozenset[str]] = []
        for text in spoken:
            self.remember(text)

    def remember(self, text: str | None) -> None:
        for sentence in sentences_in(text):
            content = _content(sentence)
            if content:
                self.said.append(content)
        self.said = self.said[-SENTENCES_REMEMBERED:]

    def is_repeat(self, sentence: str) -> bool:
        content = _content(sentence)
        if not content:
            return False
        for earlier in self.said:
            overlap = len(content & earlier)
            union = len(content | earlier)
            if union and overlap / union >= SAME_THING_SAID_TWICE:
                return True
            # Or it is wholly contained in something already said, either way
            # round: the longer one said all of it and more.
            if len(content) >= NOTHING_NEW_IN_IT and content <= earlier:
                return True
            if len(earlier) >= NOTHING_NEW_IN_IT and earlier <= content:
                return True
        return False


def narrated_the_player(
    text: str, declared: str | None = None, spoke: bool = False
) -> list[str]:
    """Phrases that put words, choices or feelings in the player's mouth.

    `declared` is what the player actually gave this turn: their own line. A
    verb they used themselves is theirs, so narrating it back is the contract
    rather than a breach — "I nod" has to permit "you nod", or the gate blocks
    the one thing step 2b asks the narrator to do.

    `spoke` is whether the resolver settled that they said something aloud. If
    they did, their speech may be voiced, quoted, or reported, and only the
    *invention* of speech is a breach. If they said nothing, every "you say" is
    an invention.
    """
    gave = {_stem(word) for word in WORDS.findall(declared or "")}
    patterns = [MIND_AND_CHOICE, CANNOT_BELIEVE]
    if not spoke:
        patterns.insert(0, INVENTED_SPEECH)
    found = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            before = WORDS.findall(text[: match.start()])
            if before and before[-1].lower() in SOMEONE_ELSE_IS_SPEAKING:
                continue
            verbs = {_stem(word) for word in WORDS.findall(match.group(0))}
            if verbs & gave:
                continue  # the player used this word; it is theirs to be told
            found.append(match.group(0))
    return found


def _state_of(store: Store, entity_id: str) -> dict:
    """An entity's transient state, as plain key-to-value (spec 2.1)."""
    return {held.key: held.value for held in store.state(entity_id)}


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
        already_said: str | None = None,
        recently_said: str | None = None,
        budget_tokens: int | None = None,
        different_now: list[str] | None = None,
        how_it_went: str | None = None,
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
        # How the people here are standing comes with them. Transient state is
        # only ever what is apparent to anyone present — anything secret is a
        # fact with a visibility — so it needs no filtering of its own beyond
        # `_can_make_out`, which has already dropped whatever the character
        # cannot notice at all.
        present = [
            {
                "id": e.id,
                "name": e.name,
                "kind": e.kind,
                **({"state": state} if (state := _state_of(self.store, e.id)) else {}),
            }
            for e in here_now
        ]

        noticed = set(noticed_fact_ids)
        context = {
            # The clock, in the character's own local time. Without it the
            # narrator invented one, and a live world opened on "the morning
            # light" at half past one in the afternoon.
            "the_time": time_of_day(self.store),
            # The resolver's one-line reading of the action, not the player's
            # own words. Handing over the raw text is what defect 2 was: the
            # narrator had the phrasing in front of it and read it back. It
            # cannot echo what it was never given, and `intent` still carries
            # any question that needs answering.
            "you_intended": intent,
            # A turn lands in two beats: the room, then the person in it. The
            # second narrator call is told what the first already said so it
            # carries on instead of describing the place twice.
            "already_told_you": already_said,
            # And what they were told at the end of the turn before, so the
            # same window does not get described six turns running.
            "told_you_last_time": recently_said,
            # Also already decided by the resolver, so the narrator never has
            # to invent speech for the player.
            "you_said_aloud": said_aloud,
            "you_are_in": {"id": here.id, "name": here.name} if here else None,
            "you_carry": [
                {"id": e.id, "name": e.name} for e in self.store.contents(PLAYER_ID)
            ],
            # The character's own transient state (spec 2.1): standing on the
            # table, out of breath, a bottle in hand. They know their own body,
            # so this reaches them unfiltered — unlike anyone else's, which is
            # only here because being in the room with somebody is enough to
            # see how they are standing.
            "your_state": _state_of(self.store, PLAYER_ID),
            # What the action left different: what they can now see, reach, or
            # do. Code assembles it from what was actually committed, which is
            # what keeps rule 1 true of the one sentence the turn closes on.
            "different_now": different_now or [],
            # How close the roll was, in words. Never the number (spec 3.2).
            "how_it_went": how_it_went,
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
        if any(
            fact.visibility != "hidden" for fact in self.store.live_facts(entity.id)
        ):
            return True
        # Transient state counts, because it is by definition what anyone in
        # the room can see. Without this the fact/state split made people
        # vanish: a roommate whose only settled detail was his mood had that
        # routed to state, was left with no visible fact, and stopped being in
        # the room at all.
        return bool(self.store.state(entity.id))

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
        already_said: str | None = None,
        recently_said: str | None = None,
        max_sentences: int | None = None,
        on_text: Callable[[str], None] | None = None,
        declared: str | None = None,
        different_now: list[str] | None = None,
        how_it_went: str | None = None,
        heard: Heard | None = None,
    ) -> str:
        """Narrate, streamed a sentence at a time, and gated on the contract.

        `on_text` receives each sentence as it is cleared, so speech can start
        before the last one is written. Nothing reaches it unchecked: a breach
        in the first sentence means nothing has gone out yet, so the whole
        narration is regenerated as before (defect 2). A breach later means the
        earlier sentences are already spoken, so the narration simply stops
        there — truncating is allowed, and "stop when the world stops" is the
        contract anyway. Taking words back is not.
        """
        system = (self.prompts_dir / prompt_name).read_text()
        user = as_prompt(
            self.context(
                intent,
                dialogue,
                noticed_fact_ids,
                said_aloud,
                omit_keys,
                already_said,
                recently_said,
                # The instructions are the first claim on the budget (spec 5.2).
                budget_tokens=self.config.token_budgets["narrator"]
                - estimate_tokens(system)
                - RETRY_ROOM
                - SEAM,
                different_now=different_now,
                how_it_went=how_it_went,
            )
        )

        # What this player has already been told, so no beat says it twice.
        # `already_told_you` asks the model not to repeat itself and a live turn
        # repeated the whole paragraph word for word anyway, so the gate holds
        # the line instead of the prompt. The caller owns the window when it has
        # one, because it spans turns.
        said_before = heard if heard is not None else Heard()
        said_before.remember(already_said)
        said_before.remember(recently_said)
        spoke = bool(said_aloud)

        accepted, broke_first, all_repeats = self._speak(
            system, user, on_text, said_before=said_before, limit=max_sentences,
            declared=declared, spoke=spoke,
        )
        if all_repeats:
            # Nothing was delivered, so there is still a choice here. A live
            # turn read the opening narration back word for word because the
            # duplicate filter ate everything and the fallback said it anyway.
            # Ask once for something new; only then settle for the repeat.
            accepted, broke_first, _ = self._speak(
                system, user + REPEATED, on_text,
                said_before=said_before, limit=max_sentences,
                repeat_if_empty=True, declared=declared, spoke=spoke,
            )
        if not broke_first:
            return accepted

        # Nothing was delivered, so one regeneration is still safe. This one is
        # ungated: a still-imperfect line beats no narration at all, and
        # blocking it a second time would leave the player in silence.
        retry = user + CORRECTION.format(found=", ".join(sorted(set(broke_first))))
        accepted, broke_again, _ = self._speak(
            system, retry, on_text, gate=False, said_before=said_before,
            limit=max_sentences, repeat_if_empty=True, declared=declared,
            spoke=spoke,
        )
        if broke_again:
            self.breaches.append(
                f"{', '.join(sorted(set(broke_again)))} survived a regeneration"
            )
        return accepted

    def _speak(
        self,
        system: str,
        user: str,
        on_text: Callable[[str], None] | None,
        gate: bool = True,
        said_before: Heard | None = None,
        limit: int | None = None,
        repeat_if_empty: bool = False,
        declared: str | None = None,
        spoke: bool = False,
    ) -> tuple[str, list[str], bool]:
        """Stream one narration, clearing it a sentence at a time.

        Returns what was accepted, and the breach that stopped it only when it
        happened before anything was delivered — which is the one case the
        caller can still do something about. With `gate=False` every sentence
        goes out and the breach is only reported.
        """
        buffer = ""
        accepted: list[str] = []
        blocked: list[str] = []
        repeats: list[str] = []
        stopped = False
        heard = said_before if said_before is not None else Heard()

        def take(chunk: str) -> None:
            nonlocal buffer, stopped
            buffer += chunk
            while True:
                sentence, rest = _split_sentence(buffer)
                if sentence is None:
                    return
                buffer = rest
                if heard.is_repeat(sentence):
                    repeats.append(sentence)
                    continue  # already said; held in case nothing else lands
                broke = narrated_the_player(sentence, declared, spoke)
                if broke:
                    blocked.extend(broke)
                    if gate:
                        stopped = True
                        raise StopNarration
                accepted.append(sentence)
                heard.remember(sentence)
                if on_text is not None:
                    on_text(sentence)
                if limit is not None and len(accepted) >= limit:
                    # The contract is two to four sentences for the whole turn,
                    # and a turn can take two beats. Stopping here is what keeps
                    # the sum inside it; the rest of the generation is dropped.
                    stopped = True
                    raise StopNarration

        self.llm.stream_text("narrator", system, user, take)
        # A flag rather than catching StopNarration here: stream_text swallows
        # it, so the exception never reaches this frame, and the half-sentence
        # left in the buffer when it fired was being accepted as narration.
        if not stopped:
            # Whatever is left has no terminator; it is still a sentence.
            tail = buffer.strip()
            if tail and heard.is_repeat(tail):
                repeats.append(tail)
                tail = ""
            if tail:
                broke = narrated_the_player(tail, declared, spoke)
                if broke:
                    blocked.extend(broke)
                if not broke or not gate:
                    accepted.append(tail)
                    heard.remember(tail)
                    if on_text is not None:
                        on_text(tail)

        all_repeats = bool(repeats) and not accepted
        if all_repeats and not repeat_if_empty:
            # Hand it back for one more try. Nothing has been delivered, so the
            # caller can still ask for something the player has not heard.
            return "", [], True
        if all_repeats:
            # Out of tries. Silence is worse than a repeat: a live turn asking
            # "how much money have I got?" came back completely blank.
            accepted = repeats[: limit or len(repeats)]
            for sentence in accepted:
                if on_text is not None:
                    on_text(sentence)

        text = " ".join(accepted)
        # A breach once something is already out is recorded, not retried:
        # there is no taking back a sentence the player has heard.
        if gate and blocked and accepted:
            self.breaches.append(
                f"{', '.join(sorted(set(blocked)))} cut the narration short"
            )
            return text, [], False
        return text, blocked, False
