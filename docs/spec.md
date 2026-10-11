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

### 3.1 Resolver output

The resolver returns JSON, validated with pydantic, containing:

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

**Rule:** No model sees a roll before both outcomes exist. Every roll is logged with its odds, both outcomes, and the result.

**Rule:** Narration never mentions rolls, odds, or the numbers behind them. They appear only in the dev view (section 13.5).

### 3.3 When a model call fails

Invalid JSON gets one retry with the validation error attached. If the retry also fails, nothing commits. The player hears one short out-of-character line asking them to say it again, and the failure is logged. Truth is never committed from a guess.

## 4. Narration

The narrator turns committed results into speech. It never decides anything.

**Rule (the narration contract):**

- Use second person, present tense, and plain spoken English. Two to four sentences, unless the player asks for more.
- Say only what the player character perceives or knows. Choose what's salient: what changed, what's new, what this person would notice, and the answer to any question.
- Never narrate the player's choices, words, thoughts, or feelings. Sensations the world causes, like cold, pain, or a racing heart, are fine.
- Never use game language: dice, rolls, odds, quests, objectives, stats, levels, the director.
- No lists or formatting, because it's spoken.
- Don't end with a prompt like "What do you do?" Stop when the world stops.
- People talk in their own voice, from their own knowledge, lies included.
- Narrate outcomes, not procedures. Picking a lock, hotwiring a car, or building something is told as what happens and how it goes, never as real-world step-by-step instructions.

Questions about the surroundings ("What's the first store I see?") are answered from what the character can perceive. If the answer isn't settled yet, it's resolved first.

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

**Rule:** Every model call logs its role, model, input and output tokens, cost, and latency to `logs/`. Targets: an average under 2¢ per turn, and under 4 seconds to the first narrated word.

Narration streams, so speech can start before the sentence ends. Compaction and the director run after the narration is delivered, never before. Catch-up runs during commit, because the narration depends on it.

## 13. Interfaces

### 13.1 Text

A terminal loop: the player types and the narration prints. Commands start with `/` and are handled by code, outside the fiction:

- `/quit`: save, summarize the session, and exit
- `/sheet`: show the character sheet
- `/time`: show the local date and time where the character is
- `/cost`: show this session's spend
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
  - **Hidden facts:** a `hidden` fact's text never appears in a logged narrator prompt.
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
