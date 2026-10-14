# Milestone 1b: Feel pass

Goal: playing should feel like a good DM running a world that's already in
motion. Milestone 1 made the world true; this milestone makes the telling
alive.

From the design review after the 30-turn playtest
(`logs/playtests/20261011-thirty-turns.md`). The player's report:

1. "It's not as fun as I was hoping. You are just a regular person in a regular
   world with literally anything that you can do. Where is the fun? I don't
   think the fix is simply: have objectives."
2. Asking "What am I doing?" returned "Your voice echoes off the walls to no
   return in answer."
3. "I jump on the table" gets only the effect, with no event. The voice he
   wants, in his words: "You muster your strength and jump onto the table. You
   now stand on the table, looking around. Your view hasn't changed all that
   much, but from here you can see further beyond the crowd around you."
4. Each response opens by repeating the last sentence of the previous one.

Decided: no objectives, quests, markers, or XP. The fix is pressure, secrets,
and editing, all built from true causes.

## Step 0: Verify

- [x] D1 — no channel for talking to the narrator. **Held.** Every line went to
  `resolver.resolve`; nothing classified input, and `__main__` intercepted only
  `/` commands.
- [x] D2 — the narration contract overcorrects. **Held, and worse than the
  brief guessed.** It wasn't only the prompt: `PUTS_WORDS_IN_YOUR_MOUTH` in
  `render.py` blocked every narration of the player's speech in code, so the
  rule was enforced mechanically and could not be prompted around.
- [x] D3 — actions have no payoff. **Held.** The resolver had no field for what
  an action changed, and the one shape it could have used — a `position` or
  `posture` fact — was on the refusal list.
- [x] D4 — the Opening is filler. **Held, different mechanism.** The opening
  beat fired on `noticed or moved`, so a turn that re-settled facts about the
  same room got a beat with nothing new, and the duplicate filter was exact
  match only. Turn 2 of the playtest re-described the same kitchen window with
  new adjectives.
- [x] D5 — nothing is under pressure. **Held.** The spawned rent was due three
  weeks out and no clock existed to deliver it; secrets were optional in
  `spawn.md`; nothing varied premises. One sub-claim was already fixed:
  `opening.md` already forbade the stat-block opening.

No diagnosis was wrong in a way that changed steps 1–3.

## Step 1: Transient state

Transient state isn't a fact. The boundary test, written into spec 2.1 and
`CLAUDE.md` rule 3: **would this still be true tomorrow if nobody did anything
about it?** Yes is a fact; no is state. Drunk is state, alcoholic is a fact.

- [x] Sitting on a bench and then walking home leaves the character home and
  not sitting, in both the narration and `/dev`. Covered by
  `test_sitting_on_a_bench_does_not_follow_you_home`; a move clears
  place-bound state in code rather than waiting for a model to remember.
- [x] Climbing onto a table and getting down both commit and narrate correctly
  (`test_climbing_onto_the_table_and_back_down_both_commit`).
- [x] No transient value is refused as a duplicate
  (`test_no_transient_value_is_ever_refused_as_a_duplicate`).
- [x] Every transient change is in the turn log (`turns.state_json`) and in
  `/dev`.
- [x] Spec 2.1 and `CLAUDE.md` rule 3 amended.

## Step 2: Narration pass

- [x] 2a. Input modes, classified in code before resolving: `action`, `speech`,
  `question`, `look`, `command` (`modes.py`, spec 3.0). A question takes no
  roll, no time, no new facts — and no resolver call at all.
- [x] 2b. The contract rewritten in spec 4, `CLAUDE.md`, and `narrator.md`. The
  code gate split: invented speech is a breach only when the player said
  nothing, and any verb the player used themselves is permitted.
- [x] 2c. The Opening is the attempt, narrated before the resolver runs.
- [x] 2d. Attempt, outcome, what's different now. The payoff rides on
  transient state and perception, both committed, so rule 1 holds of the
  closing sentence.
- [x] 2e. Roll margins banded into words; the narrator gets the band, never the
  number.
- [x] 2f. Near-duplicate filter over the last six narrated sentences, tuned on
  the fluorescent-light pair (scores 0.80 against a 0.7 threshold).
- [x] 2g. Length follows the moment: 2 for a question, 3 for a quiet turn, 6
  as the voice ceiling.
- [x] 2h. End on what's in front of the character, as a positive instruction.
- [x] `/dice`, off by default, text only. It never contradicts the committed
  branch (`test_the_dice_readout_never_contradicts_the_outcome`).

Done when:

- [x] "What am I doing?" in an empty room gets a one-line answer and no time
  passes.
- [x] "I jump on the table" produces the attempt, the outcome, and what's
  different now.
- [x] A 20-turn playtest has no invented player speech, choices, or feelings,
  and declared actions are narrated. One borderline line, noted in the
  transcript: "keeping your tone professional" on a turn where the player
  only said "I call my landlord".
- [x] No sentence in that playtest restates an earlier one, by the filter and
  by reading. The residual case is an outcome beat re-saying the attempt in
  different words, which the filter cannot see.
- [x] Near misses and clean successes read differently.
- [x] Latency and cost reported: first narrated word 3.8s → **1.3s** (p50,
  worst 2.6s), cost 0.70¢ → **0.72¢** a turn. Both inside spec 12's targets.

## Step 3: Life in motion

- [x] 3a. Spawning reworked: a deadline inside the first in-game hour, two to
  four secrets as hidden facts, varied premises recorded in
  `logs/premises.txt`, no stat-block opening. A spawn with no pressure is
  refused and spends its retry with the reason attached.
- [x] 3b. The minimal clock: a `timeline` table, `schedule`/`due`/`mark_fired`,
  fired into the resolver's context as `happening_now`. Built as the foundation
  of milestone 3, not a stand-in (spec 6.2).
- [x] 3c. Cut, linger, thread, in the resolver prompt and the narrator's
  length rule.
- [x] 3d. One life played to 20 turns, with the player's go-ahead, rather than
  three: the brief's own plan is that he plays one himself, so the spend went
  on getting one life right. Four lives were spawned in all — a night-shift
  EMT, a repo driver, a wedding photographer, a florist — which is the premise
  record doing its job.

  | Question | Answer |
  |---|---|
  | Did a want appear within five turns? | Yes, turn 1. She is mid-arrangement with a customer waiting, a delivery due in 40 minutes and an investor in 150. Nothing announced any of it. |
  | A moment of hesitation before a choice? | Once, and not the designed one: after "everything's fine, though there's a slight hesitation in their voice", whether to push the employee. The deadline never created one, because the compound-action bug meant she could not actually leave. |
  | Did digging find a secret, and did it feel planned? | Yes. The employee's interview surfaced as a tell rather than an answer. It felt planned because it was — committed before turn one. |
  | Anything cut that should have been played, or played that should have been cut? | Played that should have been cut: two turns of sifting mail. Cut that should have been played: the drive to the venue, which was skipped by being broken rather than by being compressed. |
  | Did anything feel contrived? | No. The one false note was mechanical, not dramatic: two people's details merged into one. |

  Transcript and the full findings: `logs/playtests/20261011-233212-final20.md`
  (stage 1 in `…-232754-final20.md`, earlier runs in `…-feel-pass-A/B.md`).

## Found live and fixed during the playtest

Five defects the offline tests could not have found. Each has a test now.

- **The spawn was truncated** at `max_tokens=1200`, and the retry had no more
  room than the first try, so a life could not start. Raised to 2400; output
  is billed on what is generated, so the headroom is free.
- **A spawn that fails its own check twice now opens anyway.** A refused spawn
  is not a worse life, it is no game at all.
- **An existing place was being refused for its proposed parent.** The player
  was already in `place:ambulance`; the resolver re-listed it under a
  `place:houston` it never settled, and the turn was refused twice over a
  parent nobody would have used.
- **The model copied the example deadlines verbatim.** Two lives in a row
  opened under "the yard closes" and "Dory comes by for his money" — straight
  out of `spawn.md`. The schedule example is a placeholder now, and the
  premise list is framed as directions rather than options. The next life
  scheduled a wedding delivery and an investor meeting of its own.
- **The attempt beat echoed the previous turn's narration.** Asked for "I
  finish dressing the wound" it wrote back the turn before, word for word. It
  is no longer given `told_you_last_time`: one sentence to write, one thing to
  write about.
- **A supersession of transient state was dropped.** A florist put her shears
  down and the write was refused as "no live 'in_hand' to supersede", leaving
  them in her hand. It routes to state now, like `new_facts` already did.
- **The near-duplicate filter missed two shapes**: a sentence that is a strict
  subset of an earlier one, and `circling` against `circle`. Containment and
  stemming were added, both tuned on the lines that went out live.

## Still open

Findings 2 and 3 of the 30-turn run were out of scope and are unchanged,
except where this work touched them.

**Fixed after the playtest, out of the original scope:** compound actions did
not move the character (finding 4). "I drive the delivery over to the wedding
venue" charged twenty-two minutes and left her at her own work table.
`check_travel` refuses that opening and spends the retry; the resolver prompt
now says the clock and the destination have to agree. Verified live on the
same save, on the same two turns that broke: both arrive, and "Where am I?"
answers with the venue and then the shop. Held by
`test_a_journey_that_takes_time_has_to_arrive` and a parametrised test that
the check stays narrow.

1. **Four of twenty turns sent only the first object** (finding 2), up from
   three of thirty. It degrades better now, because the outcome beat fires
   from whatever committed rather than reading as a recap, but the dialogue in
   those turns is lost.
2. **`contents` on a container is the most-dropped write**, four of eleven.
   Spec 2.1 refuses it as structural, and the resolver keeps wanting it for a
   glovebox or a mail stack where an entity per item would be absurd. This
   needs a decision, not a quiet loosening — see the open questions in the
   report.
3. **Two people merged** in one answer and on the sheet.
