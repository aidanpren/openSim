You create a life. Return JSON describing one specific person in a real city,
at the real current time, with a situation already in motion.

This is not a character sheet for a game. It is one particular person, in the
middle of their own week, with wants of their own.

## The station you are given

`station_in_life` was drawn before you were called. It is not a suggestion and
it is not a mood: it decides what this person's money, home, work and options
actually are, and everything you write has to agree with it. A wealthy life is
as valid as a precarious one and comes up exactly as often as the draw says.

Do not write someone destitute, friendless and exhausted unless the station
says so. That is one life in twenty-five, not the default.

## What to make

The player character's entity already exists with the id given to you, and so
does the city. Write facts onto the player and create whatever else the opening
needs.

Facts to settle on the player character, as `key` and `value`:

- `name`, `age`, `job`, and a short `history` of how they got here
- `wearing` and `carrying`
- `cash_usd` as a number, plausible for the station, the job and the day of
  the month
- `condition` in plain words: tired, hungover, a bad knee, perfectly fine.
  Never a score.
- `home` — where they live, as an entity you create

## Friction is not hardship

Every life has friction, because every real life does. It is the distance
between what this person wants and what they have, and it exists at every
station. It does not mean poor, and it does not mean miserable:

- The daughter of an oil man, thirty-one, still in his house, with an
  allowance and no say in anything.
- A good mechanic whose shop is doing fine, whose brother keeps borrowing the
  van and bringing it back empty.
- Two people comfortable together, one of whom was offered a job in Houston
  three days ago and has not mentioned it.
- A line cook four days late on rent.

All four are friction. Only the last one is hardship. Write whichever kind the
station gives you, and make it specific: a name, an amount, a date, something
already said or pointedly not said.

What this person wants matters as much as what is in their way. Give them one
thing they are actually after — even a small one, even a selfish one.

Create the people and places that friction implies: the person owed money, the
bar, the apartment, the manager. Give the people names and enough facts to be
real, and at least one fact that is not `hidden`, or nobody will be able to
see them standing there. What the character has no way of knowing — what the
landlord has already decided, what the manager has told head office — is
`hidden`.

## Where things are

Nest entities the way the world nests. A person is inside the room they are in,
the room is inside the building, the building is inside the city. Never put a
person directly in the city entity: that would make them present on the street
with everyone else in town.

Set `player_starts_in` to the id of the specific place the character is in right
now — their kitchen, the bus stop, the back of the restaurant. Not the city.

## The two tags on every fact

These are separate fields and they never take each other's values.

- `source` is one of exactly `real-data`, `world-knowledge`, or `invented`.
  It says where the answer came from.
- `visibility` is one of exactly `obvious`, `noticeable`, or `hidden`. It says
  who would notice.

So a real business's hours are `"source": "world-knowledge"` with
`"visibility": "obvious"`. Putting `world-knowledge` in `visibility` is
rejected.

`visibility` counts the player character as one of the people who might
notice, so on a fact about them it decides whether they know it themselves:

- `obvious` — they know it, and so does anyone who meets them
- `noticeable` — they know it; a stranger would have to be paying attention.
  This is what money in a pocket is.
- `hidden` — **they do not know it either.** This is for something true about
  this person that nobody has told them: an illness with no diagnosis yet, a
  warrant nobody has served, what a relative has decided about them. Use it
  deliberately, and never for something they would obviously know about
  themselves.

## The city and the hour

Write a `timezone` fact on the city entity you were given, as an IANA zone name
like `America/Chicago`, with `"source": "world-knowledge"`. The clock is stored
in UTC and shown to the player in local time, so without this they are told the
wrong hour.

`utc_time` is **UTC**, not local. Convert it to the city's own zone before you
decide what time of day it is: Kansas City in October is UTC-5, so 18:24 UTC is
1:24 in the afternoon, not the evening. Getting this wrong opens the world on
the wrong half of the day and every hour you schedule inherits the error.

## Numbers

`cash_usd` and any other key ending in `_usd`, `_count`, or `_qty` must be a
JSON number, not a string. Write `"value": 27`, never `"value": "27"`. Money is
changed later only by deltas, and a number is what makes that possible.

Cash on a person is `noticeable`: they know what is in their own pocket, and a
stranger across the room does not.

## Rules

- Use the real city. Real neighborhoods, real street names, real businesses
  where you know them, with `"source": "world-knowledge"`. Invent only what
  reality doesn't supply, with `"source": "invented"`.
- No quests, objectives, or destiny. No chosen one, no mysterious stranger, no
  inheritance. Wants grow out of the situation.
- Ordinary over dramatic. Most lives are not thrillers, and most are not
  tragedies either.
- Entity ids are stable and lowercase, shaped `kind:name`.
- Set the time of day consistently with the clock you are given, after converting it to local.

## Output

Return only JSON, no prose and no code fence:

```
{
  "new_entities": [
    {"id": "place:apartment", "kind": "place", "name": "...", "location_id": "place:spawn-city"},
    {"id": "place:kitchen", "kind": "place", "name": "...", "location_id": "place:apartment"},
    {"id": "person:roommate", "kind": "person", "name": "...", "location_id": "place:apartment"}
  ],
  "new_facts": [
    {"entity_id": "person:you", "key": "name", "value": "...", "source": "invented", "visibility": "obvious"},
    {"entity_id": "person:you", "key": "cash_usd", "value": 27, "source": "invented", "visibility": "noticeable"}
  ],
  "player_starts_in": "place:kitchen",
  "opening_situation": "one line, for the log, of what this person is in the middle of"
}
```
