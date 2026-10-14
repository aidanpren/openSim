"""Which channel the player is speaking on (spec 3.1).

Every line used to be resolved as an in-world action, so "What am I doing?"
became the character saying those words out loud, and the world answered
honestly: "Your voice echoes off the walls to no return in answer." The engine
was faithful to the wrong channel.

Four channels:

- `command` — a `/` line, handled by code outside the fiction. `__main__.py`
  takes those before anything here runs.
- `question` — put to the narrator, not to the room. Answered from what this
  character knows and perceives: no roll, no time, no new facts.
- `look` — a question about the world that nobody present can answer, so
  something has to be settled before there is an answer. Resolved as a look.
- `speech` — said out loud to somebody who is here.
- `action` — everything else, which is the fallback, so a misread is the
  behaviour the engine had before this module existed.

Classification is code rather than a model call. It is deterministic, it costs
nothing, and for a question about the character themselves it saves the whole
resolver call: those turns settle nothing by definition, so there is nothing
for a resolver to do.

Created nowhere — these are functions. Called by turn.py, which uses the mode
to decide whether to resolve at all.
"""

from __future__ import annotations

import re

ACTION = "action"
SPEECH = "speech"
QUESTION = "question"
LOOK = "look"
COMMAND = "command"

# An interrogative opener. "Do I" and "am I" matter as much as the wh-words:
# "Am I bleeding?" is a question about the character, not an attempt to bleed.
ASKS = re.compile(
    r"^\s*(?:"
    r"what|where|when|who|whom|whose|which|why|how|"
    r"am|are|is|do|does|did|can|could|will|would|should|have|has|had"
    r")\b",
    re.IGNORECASE,
)

# The character asking about themselves: their body, their pockets, their
# place, what they are in the middle of. Those are answerable from what they
# already know, which is the whole of what a question to the narrator is.
ABOUT_ME = re.compile(r"\b(?:i|me|my|mine|myself|i'm|im)\b", re.IGNORECASE)

# A declared action that happens to contain a question word. "I ask the
# bartender what time it is" is an action; "I look around and see what's here"
# is an action. Both start with a pronoun, not an interrogative, so ASKS
# already rejects them — this catches the ones that start with a verb.
DECLARES = re.compile(
    r"^\s*(?:i\b|let's|lets|go\b|walk|run|look|take|grab|open|say|tell|ask|"
    r"give|put|sit|stand|wait|sleep|drive|call|knock|push|pull|hit|check)",
    re.IGNORECASE,
)

# Said out loud, beyond doubt: the player put it in quotes.
QUOTED = re.compile(r'^\s*["“\'](.+)["”\']\s*$', re.DOTALL)

# Long enough and it is a plan, not a question, whatever it starts with:
# "How about I head down to the bar and see if Gary's there" is an action.
TOO_LONG_TO_BE_A_QUESTION = 14


def classify(said: str, someone_present: bool) -> str:
    """Which channel this line is on. `action` whenever it is not sure."""
    text = said.strip()
    if not text:
        return ACTION
    if text.startswith("/"):
        return COMMAND

    if QUOTED.match(text):
        # Unambiguous, and the only form of "it could be speech" worth reading
        # in code: a bare imperative like "jump on the table" is just as likely
        # to be an action, and the resolver settles that from `input_mode`.
        return SPEECH

    interrogative = bool(ASKS.match(text)) or (
        text.endswith("?") and not DECLARES.match(text)
    )
    if not interrogative:
        return ACTION
    if len(text.split()) > TOO_LONG_TO_BE_A_QUESTION:
        return ACTION

    if ABOUT_ME.search(text):
        # About this character, so it is theirs to know and nobody else's to
        # answer. True even in a crowded room: "What am I doing?" is never a
        # line spoken to the bartender.
        return QUESTION
    if someone_present:
        # The starting rule from the brief: if somebody is here and it could be
        # speech, it is speech. "How much for a beer?" in an empty room is a
        # look at the menu; across a bar it is a question to the bartender.
        return SPEECH
    return LOOK


def takes_time(mode: str) -> bool:
    """Whether the clock may move. A question to the narrator costs nothing."""
    return mode not in (QUESTION, COMMAND)
