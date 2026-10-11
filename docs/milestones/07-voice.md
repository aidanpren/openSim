# Milestone 7: Voice, at a desk

**Goal:** play the game by listening and speaking. The narration comes out of the speakers a sentence at a time while the rest of the turn is still being written, and what you say out loud reaches the resolver without you touching the keyboard.

Read first: `docs/vision.md` (the "Imagination is the screen" pillar), then sections 4, 12, 13.1, 13.2, and 14 of `docs/spec.md`.

## Why this one is out of order

Milestones 2 through 6 are not done. This one jumped the queue because the narration contract is a speech contract — "two to four spoken sentences", "no lists or formatting, because it's spoken" — and it has only ever been judged as text on a screen. Lines that read fine can land badly out loud: a sentence that scans at a glance can run too long to follow by ear, and the gap between sentences is invisible on a screen and obvious in a room.

So this milestone is partly a measurement instrument for work already done. Expect it to send findings back into `narrator.md`.

What it does **not** get, because those milestones own it:

- No session summary when you quit, and no scene compaction — milestone 2.
- No skipping time by saying "I sleep until morning" beyond a single duration — milestone 3.
- No one living their own life while you talk — milestone 4.
- Real opening hours and travel times are still model knowledge — milestone 6.
- Sound layers under the narration, and per-person voices, are milestone 8 and phase 3 below.

## What the machine will and won't do

Measured on this Mac on 2026-10-11, because it decided the architecture.

`/usr/bin/say` runs (it is Apple-signed; the endpoint policy that blocks `uv` kills binaries with *broken* signatures, not system ones). An in-process synthesizer would be better and is not available: `pyobjc-framework-AVFoundation` has no wheel for the Python 3.9 this machine is stuck on, so pip builds from source and hits the same unaccepted Xcode license that breaks `git`.

Three facts about `say` that shaped phase 1:

1. It costs a fixed **1.35s** before the first sound, independent of sentence length. Four sentences spawned naively means 5.4s of silence inside one turn.
2. That cost is voice loading, and it does not depend on the text. A process spawned **1.0s or more before** the text is handed to it pays only **0.58s**. Less than 1.0s of warm-up and the saving shrinks; more than 1.0s buys nothing further.
3. It does not speak until stdin closes, so one long-lived process cannot be fed clause by clause. It is one process per sentence.

Hence the warm-process design in phase 1: keep a spawned, unfed `say` standing by, and spawn its replacement the instant the current one is fed. Every sentence then gets at least a sentence's worth of playback as warm-up, which is always more than 1.0s, so every sentence pays 0.58s and no sentence pays 1.35s.

**Against the spec's target.** Spec 12 asks for under 4 seconds to the first narrated word. Text reaches 3.8s. Speech adds 0.58s, so the first *spoken* word lands at about **4.4s** — over the target, and `say` has no more to give. Closing it needs one of: the in-process synthesizer (blocked here, fine on a real 3.12), or streaming cloud TTS (phase 2's decision). Record the real number rather than restating the target.

## Phase 1: voice out

1. **`voice.py`:** a `Voice` with a queue and one worker thread. `say(sentence)` enqueues and returns immediately, so synthesis never blocks the model stream. `warm()` puts a fresh process on standby. `wait()` blocks until the room is quiet — this is the turn-taking boundary, not a performance detail. `silence()` kills the current sentence and drops the queue, for barge-in. The process spawner is injected so tests never make noise.
2. **Wire it to the existing seam.** `__main__.speak()` was built for this: it already receives one sentence at a time, the moment it clears the narration contract. Printing stays — text and speech are the same stream, and the transcript is how playtests get reviewed.
3. **Turn-taking.** `warm()` before the resolver call, so the voice loads during the 3.8s the model is thinking. `wait()` before the `> ` prompt, so the prompt never appears over the narration. Ctrl-C during narration calls `silence()` and hands back the prompt.
4. **Voice is an output device, never a reason to lose a turn.** A failed or missing `say` records the failure and falls silent; the turn still commits and the text still prints. Non-macOS and a missing binary disable it the same way.
5. **Settings in `.env`:** `VOICE_ENABLED`, `VOICE` (empty means the system voice — no voice name in code, for the same reason model names are not in code), and `VOICE_RATE`.
6. **Out-of-fiction text is never spoken.** Commands, `/dev`, errors, and the "Back in it" resume line print only. Only narration reaches the voice.

## Phase 2: voice in

Decide the route first; this phase cannot start until it is chosen.

- **OpenAI Whisper API:** accurate, needs a second API key and a per-minute cost, and sends the player's voice off the machine.
- **macOS `SFSpeechRecognizer`:** free and on-device, needs PyObjC and a microphone permission prompt. Blocked on this Mac for the same license reason as above.
- **`whisper.cpp` locally:** no key, no cloud, needs a compiler — blocked here too.

Then: push-to-talk before always-listening, because an always-open microphone in a room with other people is a different product. End-of-speech detection decides when the turn is submitted, and a mis-heard action must be correctable before it is resolved, since there is no undo (the "Freedom with weight" pillar). Spec 13.1's `/` commands need a spoken equivalent that cannot be mistaken for an in-fiction action.

## Phase 3: voices, not a voice

Deferred until phases 1 and 2 are played. Spec 13.2 wants recurring people to have their own voices and phone calls to sound like phone calls. That needs the narrator's output to distinguish narration from dialogue, which it currently does not — today a person's speech arrives inside the same prose. That is a narration-contract change, so it gets decided after phase 1 shows how dialogue actually sounds out loud.

## The car

Spec 13.3 lists three routes and spec 16 still asks which. Not in this milestone. The desk has to be good first.

## Suggested layout

```
src/worldsim/
  voice.py      # built in __main__, fed by the speak() seam
tests/
  test_voice.py
```

## Not in this milestone

- Speech-to-text, until the route is chosen (phase 2 above)
- Per-person voices and phone-call treatment (phase 3)
- Sound layers and the dashboard: milestone 8
- The car: spec 13.3, route undecided
- Anything milestones 2 through 6 own

## Open question for the player

`/voice` to toggle speech mid-session is not in spec 13.1's command list. Worth adding to the spec, or leave it to `.env`?

## Done when

Phase 1. Built and measured on 2026-10-11; the last three need a live session, which costs money and has to be judged by ear.

- [x] `./.venv/bin/python -m pytest` passes, with no test spawning `say`. *(197 tests, 17 of them new. The spawner is injected, so the suite stays silent.)*
- [x] `VOICE_ENABLED=false` plays exactly as it does today. *(No process is spawned, `wait()` returns at once, and the text still prints.)*
- [x] Killing or removing `say` loses the voice and not the turn. *(A spawner that raises disables the voice, records the failure, and the turn finishes; a process killed mid-sentence is not recorded as a failure, because that is `silence()` doing its job.)*
- [x] The warm-process design delivers what the measurements promised. *(Three sentences through the real `say`: **0.57s** of overhead per sentence against the predicted 0.58s, where cold would have been 1.35s. Handing all three over took **1ms**, so the model stream never waits for the speakers. Barge-in cuts the audio in **0ms** and the queued sentences are never spoken.)*
- [ ] A turn's narration comes out of the speakers a sentence at a time, and the first sentence is spoken before the last one has been written. *(Verified standalone, not yet through a live turn.)*
- [ ] The `> ` prompt never appears while the voice is still talking, and Ctrl-C cuts the narration short and returns the prompt. *(The mechanism is verified; the interaction needs a live session.)*
- [ ] Time to the first *spoken* word is measured over a real session and written down here, next to the 4-second target. *(Predicted 3.8s + 0.58s ≈ 4.4s.)*
- [ ] A playtest of at least 10 turns played **by ear**, with the screen ignored, in `logs/playtests/`. Judged on what text cannot show: whether sentences are followable by ear, whether the gaps land as pauses or as dead air, and whether dialogue inside narration is distinguishable from narration.

Phase 2:

- [ ] The route is chosen and written down here with the reason.
- [ ] A 10-turn session played without touching the keyboard, except to correct a mis-hearing.
- [ ] A mis-heard action can be corrected before it resolves, never after.
