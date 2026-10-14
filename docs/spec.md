# World Sim: Technical Spec

`docs/vision.md` says what World Sim should feel like. This file says how the systems produce that feel.

Lines marked **Rule** are requirements. Most get an automated test once their system exists; the narration rules are checked in playtests. Model names and prices were checked on October 10, 2026.

## 1. The shape of it

World Sim is a loop around one source of truth: the world store.

Each turn:

1. The player says what they do.
2. **Resolve:** the resolver model reads the relevant slice of the truth and decides what happens. If the outcome is uncertain, it sets the odds and writes both outcomes, then code rolls and picks one.
3. **Commit:** code validates the result, writes it to the world store, and advances the clock.
4. **Render:** the narrator model gets only what the player character perceives and knows, and says what happens in a few spoken sentences.
5. **Background:** after the narration is delivered, slower jobs run: scene compaction and the director. (Catch-up after a skip is different: it runs during commit, because the narration depends on it.)

Nothing reaches the player that isn't in the store first.

**Rule:** The world store is the only source of truth. Models propose; code validates and commits.

## 2. The world: truth and perception

### 2.1 Entities and facts

Everything in the world is an entity: a person, place, object, organization, or vehicle. Each entity has a stable ID, a kind, a name, and a current location, which is another entity. A bar sits inside a neighborhood inside a city; a wallet sits on a person.

A fact is one small true statement about an entity: the bartender's name, the store's hours, what's in the safe. Each fact records:

- its value: plain text, or a number for quantities
- `source`: `real-data`, `world-knowledge` (what the model knows about the real place), or `invented`
- `visibility`: `obvious` (anyone present notices), `noticeable` (depends on who's looking and how), or `hidden` (needs investigation or access)

The person a fact is about is one of the people it hides from. On a fact about the player character, `hidden` means the character doesn't know it either: an illness with no diagnosis, a warrant nobody has served, what a relative has decided about them. Money in a pocket is `noticeable` — they know it, a stranger across the room doesn't. The same goes for an entity: something whose every fact is `hidden` isn't noticed by being in the room with it, which is how a revolver taped under a sink stays unfound.
- `cause`: the turn or event that created it
- `created_at`, in world time
- `superseded_by`, set only when a later event changes it

**Rule:** Facts are never edited or deleted. A change is a new fact that supersedes the old one and cites its cause. Superseded facts stay as history.

**Rule:** Quantities like cash and counts change by deltas that code applies. A model never writes a new total.

#### Facts and transient state

Not everything true right now is a fact. One question decides which half of the store something belongs in:

> Would this still be true tomorrow if nobody did anything about it?

Yes, and it's a fact, bound by the rule above. A broken wrist, a debt, a name, the hours a bar keeps, what a landlord has decided.

No, and it's **transient state**: posture, where in the room, what's in hand right now, out of breath, drunk, furious. Transient state is current state and nothing else. It has one row per entity and key, it's overwritten in place, and a new value is never refused as a duplicate — because "already sitting" is not a reason to refuse "now standing". There's no history and no supersession, so every change is recorded in the turn log instead, which is where `/dev` and any later audit read it.

The hard cases fall out of the question. Drunk is state, alcoholic is a fact. Bleeding is state, a knife wound is a fact. Angry is state, hates you is a fact.

Two further rules keep the split honest:

- **A move clears place-bound state.** You can't still be sitting on a bench you've walked away from, so code clears posture, position, activity and vantage when an entity moves. What's in your hand isn't place-bound: the bottle comes out of the bar with you.
- **Transient state is always what anyone present could see.** That's what makes it safe to hand to the narrator without a visibility flag. Anything secret is a fact with a visibility, never state.

This split exists because the alternative was measured. A 30-turn playtest settled `posture = "sitting on a bench"` as a fact; every correction after it was refused, and the character was still sitting on that bench at home in their kitchen twenty turns later. It was the only finding that made the store wrong rather than merely stale.

A model that writes transient state as a fact has it routed to state by code, not dropped. The prompt asks for the right channel and mostly gets it; when it doesn't, the choice is between a world that moves and a world that freezes.

### 2.2 Resolving on first need

The world starts nearly empty, and nothing is decided until something needs it. When the resolver needs a fact the store doesn't have, it settles it in this order:

1. Real data that code fetched for this place (section 11), when present.
2. What the model knows about the real place, tagged `world-knowledge`.
3. The most plausible invention, tagged `invented`.

The answer is committed at once and never re-resolved. Facts about the real world can go stale later; the world is its own timeline from the moment it settles something.

**Rule:** Code rejects a new fact for a key that already has a live fact on that entity, unless it's an explicit supersession with a cause. A stored fact beats real data, model knowledge, and anything said in conversation.

### 2.3 Who knows what

Every mind has its own knowledge, including the player character's. Knowledge is the set of facts that mind has perceived, been told, or inferred, with when and how it learned each one. A knowledge entry can hold a belief that differs from the fact, which is how lies and mistakes live in the world.

When a fact is superseded, knowledge of it does not move with it — that's how a mind goes on believing the lock is the one it last saw. The one exception is the fact's own subject, who is present for every change to themselves: your knowledge of your own cash follows the delta, or the narrator would be told a total you spent an hour ago. An entry that holds a belief never moves either way, because it's a record of what someone was told.

- The resolver works from the truth.
- The narrator works only from the player character's knowledge, false beliefs included. If the player was lied to, the narration reflects the lie until the world contradicts it.
- People act and speak only from their own knowledge. They can lie, and they check what they're told against what they know.

**Rule:** The narrator's context contains only facts in the player character's knowledge plus what they perceive this turn. Hidden facts, other people's minds, the timeline, and the director's plans never reach it.

### 2.4 Perception

Each turn, the resolver decides which facts the player character notices, based on their senses, attention, skills, and condition. A mechanic hears the engine knock. Someone awake for 30 hours misses the man following them. Noticed facts go into the character's knowledge.

## 3. The turn

### 3.0 Input modes

Before anything is resolved, code reads which channel the player is speaking on. Every line used to be resolved as an in-world action, so "What am I doing?" became the character saying those words out loud, and the world answered honestly that nobody replied. The engine was faithful to the wrong channel.

| Mode | What it is | What happens |
|---|---|---|
| `command` | a `/` line | handled by code, outside the fiction |
| `question` | put to the narrator, about the character themselves | answered from their own knowledge and state: no roll, no time, no new facts, and no resolver call at all |
| `look` | asked of the world, and nobody present can answer | resolved as a look, because settling it *is* the answer |
| `speech` | said aloud to somebody who's here | resolved, with the substance in `you_said_aloud` |
| `action` | everything else | resolved |

Classification is code, not a model call: it's deterministic, it costs nothing, and `action` is the fallback, so a misread is the behaviour the engine had before. A question is recognised by being interrogative and about the character — any first-person pronoun. The starting rule for the rest is the one the player set: if somebody is present and it could be speech, it's speech.

**Rule:** A question to the narrator moves no clock, writes no fact, and rolls nothing.

### 3.1 Resolver output

The resolver streams **three JSON objects back to back**, each validated with pydantic and committed as it closes, because the player waits only on the first.

1. **Opening** — where the character ends up, `duration_minutes`, `intent`, `uncertain`, `state`, and the two or three facts they register first. Roughly sixty tokens.
2. **Scene** — the rest of the truth: who else is present, what they look like, `dialogue`, `you_said_aloud`, `state`, `summary`, `scene_change`. The outcome is narrated from this object.
3. **Ledger** — what the character does not perceive now: superseding facts, quantity deltas, moves, other people's knowledge, `schedule`, `ends_life`, and the roll.

Each object commits before the beat that speaks it, so rule 1 holds throughout. Retries work on one boundary: until the opening is handed over nothing is committed, so a bad reply spends the spec 3.3 retry. After that there is no going back, and a malformed tail is reported as lost writes rather than replayed.

The resolver is also told `input_mode` (section 3.0) and `happening_now`, which is whatever the clock has just fired (section 6.2).

#### What the action changed

The resolver says what the action changed **for the player**: what they can now see, reach, or do that they couldn't a moment ago. It travels as transient `state` on them, or as facts they now perceive, and the narrator closes the turn on it.

This is the difference between an effect and a payoff. "You are on the table" is an effect. The vantage from up there — and whatever is worth seeing over a crowd for — is the payoff, and it's the reason the action was worth taking. Without it an action reads as a status line, which is what the player reported.

An `obvious` fact about the character, the place they are in, or something in it with them is perceived by code — that is what `obvious` means — so `perceived` carries only the `noticeable` and `hidden` exceptions.

Taken together the objects contain:

- `intent`: what the player is attempting, in one line
- `duration`: how much world time the action takes
- `new_facts`: facts it had to settle (section 2.2), each with source and visibility
- `roll`: null when the outcome isn't in doubt; otherwise `p_success`, a one-line `why`, and `on_success` and `on_failure` outcomes
- `outcome`: used when there's no roll
- `perceived`: the facts the player character notices this turn
- `scene_change`: whether this turn starts a new scene (section 5.3)

An outcome holds superseding facts, quantity deltas, moves (who and what is now where), knowledge updates for other people (who saw what), dialogue (speaker, gist, and whether it's true), and `ends_life` if the player character dies.

### 3.2 Rolls

- Roll only when the outcome is genuinely uncertain. Walking across a room isn't.
- Odds come from the situation: skill, condition, tools, circumstances, and real-world base rates.
- Where a real procedure exists, code runs it with real odds instead of using one probability: roulette, blackjack, slot machines, dice games. These can wait until milestone 6.
- The resolver writes both outcomes before the roll. Code rolls with a seedable `random.Random` and commits the matching branch.
- Code knows how close the roll was, and bands the margin into words: barely, with some effort, easily; by a hair, clearly, badly. The narrator is given the band and never the number, so a near miss sounds like one. A near miss that reads exactly like a hopeless one is the same turn twice, and the difference between them is most of what makes a risk feel like a risk.

**Rule:** No model sees a roll before both outcomes exist. Every roll is logged with its odds, both outcomes, and the result.

**Rule:** Narration never mentions rolls, odds, or the numbers behind them. They appear only in the dev view (section 13.5), and in the optional `/dice` readout, which is off by default and printed rather than spoken.

### 3.3 When a model call fails

Invalid JSON gets one retry with the validation error attached. If the retry also fails, nothing commits. The player hears one short out-of-character line asking them to say it again, and the failure is logged. Truth is never committed from a guess.

## 4. Narration

The narrator turns committed results into speech. It never decides anything.

### 4.1 The shape of a turn

A turn reaches the player in two beats.

1. **The attempt**, narrated *before the resolver is called at all*. The player's declared action is the one thing that's true before the outcome exists, so it's the only honest thing to say while the world is still deciding. It never assumes success — "you jump", never "you land" — and because nothing has been settled yet it cannot introduce anything new, which is how rules 2 and 5 hold of it for free. One sentence.
2. **The outcome**, narrated when the scene object closes, or after code has rolled. It says how it went and ends on what's different now.

So the arc of every turn is: the attempt, the outcome, and what it opened up. A turn that stops after the effect is half a turn.

Two beats, two narrator calls, no more. The opening object used to drive a beat of its own, and with the outcome not yet written that beat had nothing to say but the room — so it said the room again, which is the repetition the player reported. The attempt covers the same seconds with something true, and takes the first spoken word from the close of the first JSON object to the first narrator token.

If the world interrupts before the attempt finishes, the outcome continues the sentence: "…but a hand closes on your collar."

A question to the narrator (section 3.0) skips the attempt and gets a direct answer instead.

### 4.2 The contract

**Rule (the narration contract):**

- **Narrate the action the player declared, as it happens**: the effort, the body, how it goes. They gave you the action; it's yours to tell. "You muster your strength and jump onto the table" is the voice.
- **Never add what they didn't give.** No choice, thought, feeling, or gesture the player didn't put there. "You decide to wave" and "You feel proud" are not yours to say. Sensations the world causes — cold, pain, a racing heart — are the world's, and are fine.
- **Speech they gave may be voiced, quoted, or reported.** Speech they didn't give is never invented, in any form.
- Use second person, present tense, and plain spoken English.
- Say only what the player character perceives or knows. Choose what's salient: what changed, what's new, what this person would notice.
- Never use game language: dice, rolls, odds, quests, objectives, stats, levels, the director.
- No lists or formatting, because it's spoken.
- **End on what's in front of the character**: someone waiting, a sound, a car slowing. Never a prompt like "What do you do?", and never a suggestion of what they might want.
- People talk in their own voice, from their own knowledge, lies included.
- **Narrate the doing, never a how-to.** Picking a lock, hotwiring a car, or stitching a wound is told as what happens and how it goes, never as real-world step-by-step instructions. (This was "outcomes, not procedures", which was read as "don't describe the action at all" and suppressed the thing above.)

Code holds the first three of these, because prompts didn't. The check is narrow by design: it catches decisions, thoughts and chosen gestures, which are recognisable by their verbs, and it permits any verb the player used themselves — "I nod" has to make "you nod" legal. Invented speech is a breach only when the resolver settled that the player said nothing. An invented *feeling* is left to the prompt and the playtest read-through, because no pattern separates "you feel proud" from "you feel the cold on your face".

### 4.3 Length and repetition

Length follows the moment, rather than one fixed cap for everything. A routine action or a question gets a line; a moment with people in it or something at stake earns more. There's a hard ceiling of six sentences a turn, because that's what a voice can hold, and the allowance is spent across the beats.

Editing is part of the job. Nothing charged happening — walking home, waiting, queueing — compresses to one line and lands on the next thing that matters: "Twenty minutes later you're at your door. There's a note taped to it." Only true things count. When something's at stake, slow down.

**Rule:** No sentence repeats something the player has already been told. Before a sentence goes to text or speech it's compared against the last six narrated sentences, by what it's *about* rather than its wording, and near-duplicates are dropped. This is deterministic — no model call sits between a sentence and the speaker. Exact repeats were already caught; this closes paraphrases like "the fluorescent light above flickers" followed by "the fluorescent light above the sink flickers".

Silence is still worse than a repeat. If the filter eats everything, the narrator is asked once for something new, and only then does a repeat go out.

## 5. Memory and context

### 5.1 Tiers

1. **Current scene:** every turn, in full.
2. **Scene summaries:** each finished scene compacted to what could matter later: people, names, promises, debts, items, threats, open threads, places learned.
3. **Character sheet:** the player character's durable state (section 5.5).
4. **Archive:** raw transcripts of finished scenes. They're never sent by default; they're searchable by person, place, or keyword when the resolver asks to recall something.

The world store sits under all of these. The tiers record what happened; facts record what's true.

### 5.2 Context assembly

Every call has a token budget and fills it in priority order:

1. The call's instructions
2. Character sheet
3. Facts about the current place and the people present
4. Current scene, in full
5. Summaries of the most relevant past scenes
6. Last session's summary

The lowest-priority items are dropped first. Starting budgets are 5,000 input tokens for the resolver and 4,000 for the narrator.

**Rule:** Every call's input stays within its budget, whether it's turn 10 or turn 500.

### 5.3 Scenes

A scene is a continuous stretch in one setting: the taxi ride, the store, the phone call. The resolver flags a scene change when the setting or situation changes. The player never sees scene breaks.

When a scene closes, a background job writes its summary and archives the transcript.

### 5.4 Sessions

On quit, the session gets a short summary. The next launch starts with the character sheet, the last session's summary, and the last scene in full, so play resumes exactly where it stopped.

### 5.5 The character sheet

The sheet is a view over the store, never a separate copy. It shows what's true about the player character that they'd know themselves:

- **Who you are:** name, age, job, backstory
- **On you:** what you're wearing and carrying
- **Yours elsewhere:** property, vehicles, stashes, and accounts, and where they are
- **Money:** cash and balances
- **Condition:** injuries, illness, fatigue, hunger, intoxication
- **People:** who you know, how you left things, last contact
- **Open threads:** promises, debts, and deadlines you know about

Condition is described in plain words, never scored, so there are no health bars. Fatigue and hunger are computed from the clock where possible: hours awake, hours since eating.

## 6. Time

### 6.1 The clock

The world has its own clock. It's stored in UTC and shown in the local time of wherever the player character is. A new world starts at the real current time.

The clock moves only when the story does. Each action advances it by the resolver's `duration`. Real time doesn't count, so if you stop playing for a week, the world waits.

**Rule:** The clock never moves backward.

### 6.2 The timeline

Anything scheduled is a timeline entry: rent on the 1st, a loan shark's deadline, a flight's arrival, a court date. Each entry has a time, what happens, a cause, and whether it reaches the player character directly, like a call, a knock, or a letter.

When the clock passes an entry, it fires. Firing can create facts, change what people know, and schedule new entries. That's how consequences chain off-screen: she calls the police, they trace your card, the embassy calls your hotel.

Deadlines get exact timestamps. Only action durations are estimated.

**Built, in its minimal form.** A `timeline` table holds dated entries; spawning schedules one to three, and the resolver can schedule more from the ledger. Entries are written as minutes from now, because the arithmetic is code's job — a model asked for an ISO time in the world's own zone got it wrong often enough to put a deadline in the past. At the start of each turn, everything the clock has passed is collected, handed to the resolver as `happening_now`, and marked fired once the opening commits, so a turn that never commits leaves them still due. The resolver makes them true in the world, and the player finds out as the character would.

What's deliberately not here yet, and belongs to milestone 3: chains, silence (section 6.3), skips with catch-up (section 6.4), and the director that writes entries of its own (section 7). This is the foundation for those, not a stand-in to be thrown away.

### 6.3 Silence counts

Everyone close to the player character has a last-contact time and an expectation of how often they'll hear from them. When the relationship is created, a background call writes what that person does as silence grows, as timeline entries: worried after two days, calls your brother after a week, files a missing-person report after two weeks. Contact pushes those entries back. Telling them you'll be gone for three months rewrites them.

### 6.4 Skips

"I sleep until morning" and "skip to after the flight" are just long actions. Code walks the timeline forward and fires everything in the gap, in order, in one batch. For long gaps, one background call writes what the gap amounted to as facts, instead of simulating every day.

**Rule:** A skip stops at the first entry that reaches the player character, and play resumes in that moment.

### 6.5 Finding out

The player learns what happened the way the character would: 200 missed calls when the phone comes back on, a voicemail from a detective, new locks on the door.

### 6.6 Scope

Only things tied to the player character get timelines: people they know, places they own, debts, investigations. A stranger has no ongoing life until the player engages with them.

## 7. The director

The director is a background model call that moves the world between scenes. It runs at scene changes and around skips, and it never talks to the player.

It reads the people tied to the player character and their goals, open threads, the timeline, what the character cares about (what they own, protect, and keep returning to), and recent events.

It writes only three things: timeline entries, people's goals, and hooks. A hook is an opportunity with an expiry; ignore it and it's gone.

Its job:

- Move people's own goals forward off-screen.
- Let consequences catch up.
- Pace tension, the way RimWorld's storytellers do: trouble when life gets too comfortable, a break when the player is drowning.
- Aim at what the character has invested in.

**Rule:** Every director entry cites a cause that exists in the store. Validation rejects entries without one.

**Rule:** Major setbacks are rate-limited, and no event may contradict a fact. Not every long trip ends in a police report.

The director's reasoning goes to the dev view only.

## 8. People

People tied to the player character persist with their own knowledge, goals, and history with the player. They act on what they know, not on what's true. They remember what the player did and who saw it.

Strangers are created on first meeting, and from then on they're permanent like any other fact. Recurring people get their own voice once voice exists (section 13.2).

## 9. Lives and the persistent world

- **Spawning:** a new life starts as a specific person in a real city, with a name, age, job, history, and people. Code draws their station in life before the model is called, weighted so the ordinary middle is the standard and both extremes, destitute and wealthy, are rare. Whatever the station, the person wants something they don't have and something is in the way of it. That's the friction, and it isn't the same as hardship: an allowance with no say in anything is friction, and so is a brother who keeps borrowing the van. The weights live in `resolve.STATIONS`, because a model asked for "a person with at least one source of friction" writes the same broke, lonely line cook every time. There are no quests; wants grow out of the situation. The default spawn city is Kansas City, Missouri, set by `SPAWN_CITY`.
- **Spawning in motion:** the life doesn't begin, it's already under way. Three things are settled before the player says a word.
  - **A deadline inside the first in-game hour**, with a real cost if it's ignored, put on the timeline so it actually arrives (section 6.2). It is not a quest: nothing points at it, the player can walk away, and it plays out without them.
  - **Two to four secrets**, committed as `hidden` facts. Resolution is otherwise lazy, so digging used to find improvisation rather than something that was already there. The theatre-of-the-mind game this grew out of worked because the base existed before anyone looked at it. The secrets have to fit each other and the situation, and a secret is not a twist — it's a true thing nobody has told them.
  - **A premise that isn't the last one.** Recent premises are kept outside any world and fed back to the next spawn, because the station weights fixed how comfortable a life is and left what it's *about* untouched: every life was a broke twenty-something in an apartment. Not every life is crime.
  
  A spawn that settles no secrets, or puts nothing inside the hour, is refused and spends its retry with the reason attached. The opening narration is a moment, never a stat block; the biography lives on `/sheet`.
- **Death:** death is permanent for that character. The life ends with one closing line, and the next life starts as someone new.
- **The world outlives every life:** the dead character's possessions, property, and consequences stay. A new life might walk past the store the last one burned.

**Rule:** World state and life state are stored separately. Ending a life never deletes or resets world facts.

## 10. Sandbox

Sandbox mode runs on a full copy of a world. It allows what the real world doesn't: spawning a helicopter, teleporting, changing facts by command.

**Rule:** A sandbox never writes to the world it was copied from.

## 11. Real-world grounding

Before the resolver runs, code fetches real data for the current place and passes it in, so new facts start from reality. These are starting choices; check each service's current terms when building this.

| Need | Source | Notes |
|---|---|---|
| Places, streets, opening hours | OpenStreetMap (Nominatim and Overpass) | Free; attribution required; public Nominatim allows about 1 request per second |
| Background on landmarks | Wikipedia API | Free |
| Travel times | A routing service for roads; great-circle distance for flights | |
| Weather and seasons | Open-Meteo | Free; has history and forecasts |
| Richer place data, later | Google Places | Paid; its terms limit storing results |

Everything fetched becomes facts tagged `real-data` with the source URL, and it's never fetched again for the same fact. Real data is a starting point: once committed, the world can diverge from it.

Time of day matters. At 3 a.m. the bank is closed and the 24-hour diner is open.

When the player heads somewhere ("a taxi to Times Square"), resolve the destination in the background before they arrive.

## 12. Models and cost

All model calls go through OpenRouter's OpenAI-compatible API (`https://openrouter.ai/api/v1`) using the `openai` Python SDK. Models are configuration, never code.

| Role | Env var | Play default | Price per million tokens (input / output) |
|---|---|---|---|
| Resolver (turns, spawning) | `RESOLVER_MODEL` | `nousresearch/hermes-4-405b` | $1 / $3 |
| Narrator | `NARRATOR_MODEL` | `nousresearch/hermes-4-405b` | $1 / $3 |
| Background (compaction, director, catch-up) | `BACKGROUND_MODEL` | `nousresearch/hermes-3-llama-3.1-70b` | $0.70 / $0.70 |

Prices are from [OpenRouter's Nous Research page](https://openrouter.ai/nousresearch). Use the 70B for every role while developing.

Every role needs a low-refusal model, because the resolver handles the same actions the narrator describes. The engine adds no content filters or refusals of its own; what the world allows is up to the configured model.

Rough cost per turn at the play defaults, before OpenRouter's fee on credit purchases:

| Call | Input tokens | Output tokens | Cost |
|---|---|---|---|
| Resolver | ~3,000 | ~600 | ~0.48¢ |
| Narrator | ~3,000 | ~120 | ~0.34¢ |
| Background jobs, spread across turns | | | ~0.1¢ |
| **Total** | | | **~1¢ per turn, roughly 30–35¢ per 30-minute session** |

**Rule:** Every model call logs its role, model, input and output tokens, cost, and latency to `logs/`. Latency is logged as time to the first token, time to each streamed object, and total, because those are three different problems. Targets: an average under 2¢ per turn, and under 4 seconds to the first narrated word.

Narration streams, a sentence at a time, so speech can start before the rest is written. Compaction and the director run after the narration is delivered, never before. Catch-up runs during commit, because the narration depends on it.

Only time-to-first-word is worth optimising. A turn's narration is around 48 output tokens, which is about fourteen seconds of speech, against a two-to-three second tail after the first object closes: once the voice starts, the rest of the turn finishes underneath it. So the engine minimises how little has to exist before someone speaks, and lets total turn time be what it is.

The resolver's instructions are most of what it has to prefill — 1,895 tokens of a 2,512-token input, measured — so the prompt files have a token ceiling, held by a test.

## 13. Interfaces

### 13.1 Text

A terminal loop: the player types and the narration prints. Commands start with `/` and are handled by code, outside the fiction:

- `/quit`: save, summarize the session, and exit
- `/sheet`: show the character sheet
- `/time`: show the local date and time where the character is
- `/cost`: show this session's spend
- `/dice`: toggle an inline roll readout, like `(d20: 16, needed 9)`. Off by default, printed and never spoken. It's a reading of the roll that already happened, inverted because code rolls low-is-good, and it never contradicts the branch that was committed.
- `/dev`: toggle the dev view (section 13.5)

### 13.2 Voice at a desk

Speech-to-text feeds the engine, and the narration plays through streaming text-to-speech. Recurring people get their own voices, and phone calls sound like phone calls.

### 13.3 The car

Three routes, to be chosen later:

- **Siri Shortcut:** the cheapest, but turn-taking is clunky.
- **A phone number:** "Hey Siri, call World Sim" works in CarPlay like any call. Telephony and speech cost money per minute.
- **A native iOS app:** CarPlay requires Apple's entitlement for voice-based conversational apps, which Apple grants app by app.

### 13.4 Atmosphere and dashboard

- **Sound:** a finite library of loopable layers, like a forest bed, wind, a stream, night insects, crowd noise, traffic, and a casino floor. At each scene change the resolver names layers and levels, and the client crossfades. Generative audio can replace the library later.
- **Dashboard:** a read-only page with a map, the character sheet, and recent scenes. It shows state and never takes input.

### 13.5 Dev view

The dev view is hidden during normal play. It shows the truth at the current place next to what the character perceived, every roll with its odds and both outcomes, the timeline, the director's log, and the cost and latency of each call.

## 14. Testing

- Unit tests never touch the network. A `FakeLLM` returns scripted responses.
- Each Rule gets a test when its system is built. The key ones:
  - **Revisit:** a place's facts are identical after leaving and returning.
  - **Hidden facts:** a `hidden` fact's text never appears in a logged narrator prompt, including on a question turn.
  - **Transient state:** sitting on a bench and then walking home leaves the character home and not sitting; no transient value is ever refused as a duplicate.
  - **Rolls:** with a fixed seed, the committed outcome matches the branch the roll picked.
  - **Deltas:** cash after a win equals cash before plus the delta.
  - **Clock:** time never decreases, and a skip fires entries in order and stops at the first one that reaches the player.
  - **Budget:** a simulated 500-turn game keeps every call within its token budget.
  - **Lives:** ending a life leaves every world fact unchanged.
  - **Sandbox:** playing in a sandbox leaves the original world's files byte-identical.
- Playtest transcripts go to `logs/playtests/` and are reviewed against the narration contract (section 4).

## 15. Roadmap

Each milestone gets a brief in `docs/milestones/`, written when the one before it is done.

1. **Text core:** a world that remembers. The turn loop, the store, resolving on first need, perception, rolls, the clock, the sheet, saves.
2. **Memory tiers:** scenes, compaction, session summaries, archive recall, token budgets.
3. **Time:** the timeline, chains, silence, and skips with catch-up and interruption.
4. **The director and living people.**
5. **Lives and sandbox:** death, new lives in the same world, sandbox copies.
6. **Real-world grounding:** places, hours, travel times, weather, landmarks, and real odds for games of chance.
7. **Voice:** at a desk first, then a car route.
8. **Atmosphere and dashboard:** sound layers, the dashboard, the full dev view.

## 16. Open questions

- Does Hermes 4 405B stay coherent over a long campaign? The freedom test gives a first answer, and milestone 2 playtests give a better one.
- Is one resolver call per turn enough, or should interpreting the action and resolving it be separate calls?
- Is OpenStreetMap enough, or is Google Places worth its cost?
- Which car route?
