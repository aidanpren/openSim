"""The world store: the only source of truth (spec section 2).

One SQLite file per world, under saves/<world>/world.db. Models propose; this
module validates and commits. It enforces, in code:

- Facts are never edited or deleted. A change is a new fact that supersedes the
  old one and cites its cause, and the old row stays as history.
- A second live fact for the same key on the same entity is rejected unless it
  comes through supersede().
- Transient state is the other half of that split (spec 2.1) and lives in its
  own table: current state only, overwritten freely, never refused as a
  duplicate and never kept as history.
- Numeric facts change only through apply_delta(), so nothing can write a total.
- The clock never moves backward.
- World tables and life tables are separate, so ending a life touches no fact.

Created by __main__.py and passed to resolve.py, turn.py, render.py, and
sheet.py.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SOURCES = frozenset({"real-data", "world-knowledge", "invented"})
VISIBILITIES = frozenset({"obvious", "noticeable", "hidden"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    location_id TEXT REFERENCES entities(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY,
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    key           TEXT NOT NULL,
    value_text    TEXT,
    value_num     REAL,
    source        TEXT NOT NULL,
    visibility    TEXT NOT NULL,
    cause         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    superseded_by INTEGER REFERENCES facts(id),
    CHECK ((value_text IS NULL) != (value_num IS NULL))
);

CREATE INDEX IF NOT EXISTS facts_live ON facts (entity_id, key, superseded_by);

-- Transient state (spec 2.1). Posture, position within a place, what is in
-- hand right now: true of this instant and not of tomorrow. One row per
-- entity and key, overwritten in place, so there is no history and no
-- supersession. The turn log keeps what changed and when.
CREATE TABLE IF NOT EXISTS state (
    entity_id TEXT NOT NULL REFERENCES entities(id),
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    set_at    TEXT NOT NULL,
    cause     TEXT NOT NULL,
    PRIMARY KEY (entity_id, key)
);

-- The timeline (spec 6.2), in its minimal form: dated events that fire when
-- the clock passes them. Spawning schedules one to three; the resolver is told
-- about each as it comes due. Milestone 3 builds chains and catch-up on top.
CREATE TABLE IF NOT EXISTS timeline (
    id             INTEGER PRIMARY KEY,
    at             TEXT NOT NULL,
    what           TEXT NOT NULL,
    cause          TEXT NOT NULL,
    reaches_player INTEGER NOT NULL DEFAULT 0,
    fired_at       TEXT
);

CREATE INDEX IF NOT EXISTS timeline_due ON timeline (fired_at, at);

CREATE TABLE IF NOT EXISTS knowledge (
    id          INTEGER PRIMARY KEY,
    mind_id     TEXT NOT NULL REFERENCES entities(id),
    fact_id     INTEGER NOT NULL REFERENCES facts(id),
    learned_at  TEXT NOT NULL,
    how         TEXT NOT NULL,
    belief_text TEXT,
    belief_num  REAL,
    UNIQUE (mind_id, fact_id)
);

CREATE TABLE IF NOT EXISTS clock (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    now TEXT NOT NULL
);

-- Life tables. Nothing here cascades into the world tables above.
CREATE TABLE IF NOT EXISTS lives (
    id           INTEGER PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES entities(id),
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    ending       TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY,
    life_id     INTEGER NOT NULL REFERENCES lives(id),
    world_time  TEXT NOT NULL,
    player_said TEXT NOT NULL,
    intent      TEXT,
    roll_json   TEXT,
    narration   TEXT,
    mode        TEXT,
    state_json  TEXT
);
"""

# Columns added after worlds already existed. A world under saves/ is somebody's
# life, so the schema moves forward around it rather than asking them to start
# again. `CREATE TABLE IF NOT EXISTS` never alters a table that is already
# there, which is why these have to be applied by hand.
ADDED_COLUMNS = (
    ("turns", "mode", "TEXT"),
    ("turns", "state_json", "TEXT"),
)


class StoreError(Exception):
    """A write that would break one of the rules."""


class FactConflict(StoreError):
    """A live fact already holds this key on this entity."""


class ClockWentBackward(StoreError):
    """Rule: the world clock never moves backward."""


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    name: str
    location_id: str | None


@dataclass(frozen=True)
class Fact:
    id: int
    entity_id: str
    key: str
    value: str | float
    source: str
    visibility: str
    cause: str
    created_at: datetime
    superseded_by: int | None


@dataclass(frozen=True)
class Known:
    """A fact as one mind holds it. A belief that differs is how lies live."""

    fact: Fact
    learned_at: datetime
    how: str
    belief: str | float | None = None

    @property
    def value(self) -> str | float:
        return self.fact.value if self.belief is None else self.belief


@dataclass(frozen=True)
class State:
    """One transient value as it stands right now (spec 2.1)."""

    entity_id: str
    key: str
    value: str
    set_at: datetime
    cause: str


@dataclass(frozen=True)
class Scheduled:
    """A timeline entry (spec 6.2). `fired_at` is None until the clock passes it."""

    id: int
    at: datetime
    what: str
    cause: str
    reaches_player: bool
    fired_at: datetime | None


@dataclass(frozen=True)
class Life:
    id: int
    character_id: str
    started_at: datetime
    ended_at: datetime | None
    ending: str | None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        entity_id=row["entity_id"],
        key=row["key"],
        value=row["value_text"] if row["value_num"] is None else row["value_num"],
        source=row["source"],
        visibility=row["visibility"],
        cause=row["cause"],
        created_at=_parse(row["created_at"]),
        superseded_by=row["superseded_by"],
    )


def _row_to_scheduled(row: sqlite3.Row) -> Scheduled:
    return Scheduled(
        id=row["id"],
        at=_parse(row["at"]),
        what=row["what"],
        cause=row["cause"],
        reaches_player=bool(row["reaches_player"]),
        fired_at=_parse(row["fired_at"]),
    )


class Store:
    def __init__(self, db_path: Path, starts_at: datetime | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # A turn writes a few dozen rows and each one used to be its own fsync.
        # WAL plus synchronous=NORMAL keeps a crash from tearing a committed
        # turn while taking that cost off the path the player waits on.
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self._open_transactions = 0
        if self.db.execute("SELECT 1 FROM clock WHERE id = 1").fetchone() is None:
            start = starts_at or datetime.now(timezone.utc)
            self.db.execute("INSERT INTO clock (id, now) VALUES (1, ?)", (_iso(start),))
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns that worlds created before them are missing."""
        for table, column, kind in ADDED_COLUMNS:
            have = {
                row["name"]
                for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit everything written inside as one unit, or none of it.

        Spec 1: every turn is resolve, commit, render. Without this a turn was
        a few dozen separate commits, so a crash or a refused write partway
        through could leave the clock advanced over a half-written world. Nests,
        so the store's own methods still work when called directly.
        """
        if self._open_transactions:
            self._open_transactions += 1
            try:
                yield
            finally:
                self._open_transactions -= 1
            return
        self._open_transactions = 1
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self._open_transactions = 0

    def _commit(self) -> None:
        """Commit, unless a transaction() is holding the writes together."""
        if not self._open_transactions:
            self.db.commit()

    # ---- the clock (spec 6.1) ----

    def now(self) -> datetime:
        return _parse(self.db.execute("SELECT now FROM clock WHERE id = 1").fetchone()[0])

    def advance(self, minutes: float) -> datetime:
        """Move the clock by an in-world duration. Never backward."""
        if minutes < 0:
            raise ClockWentBackward(f"cannot advance the clock by {minutes} minutes")
        moment = self.now() + timedelta(minutes=minutes)
        self.db.execute("UPDATE clock SET now = ? WHERE id = 1", (_iso(moment),))
        self._commit()
        return moment

    # ---- entities (spec 2.1) ----

    def add_entity(
        self, entity_id: str, kind: str, name: str, location_id: str | None = None
    ) -> Entity:
        if location_id is not None and self.entity(location_id) is None:
            raise StoreError(
                f"cannot put {entity_id!r} in {location_id!r}, which does not exist"
            )
        try:
            self.db.execute(
                "INSERT INTO entities (id, kind, name, location_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (entity_id, kind, name, location_id, _iso(self.now())),
            )
        except sqlite3.IntegrityError as exc:
            # Everything the store refuses is a StoreError, so callers that are
            # draining model output only ever have to catch one thing.
            raise StoreError(f"cannot add entity {entity_id!r}: {exc}") from exc
        self._commit()
        return Entity(entity_id, kind, name, location_id)

    def entity(self, entity_id: str) -> Entity | None:
        row = self.db.execute(
            "SELECT id, kind, name, location_id FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            return None
        return Entity(row["id"], row["kind"], row["name"], row["location_id"])

    def move(self, entity_id: str, location_id: str | None) -> None:
        if self.entity(entity_id) is None:
            raise StoreError(f"no entity {entity_id!r} to move")
        if location_id is not None and self.entity(location_id) is None:
            raise StoreError(f"no place {location_id!r} to move {entity_id!r} into")
        if location_id == entity_id:
            raise StoreError(f"{entity_id!r} cannot be inside itself")
        self.db.execute(
            "UPDATE entities SET location_id = ? WHERE id = ?", (location_id, entity_id)
        )
        self._commit()

    def contents(self, location_id: str) -> list[Entity]:
        """What is here. Used to decide who is present and what is carried."""
        rows = self.db.execute(
            "SELECT id, kind, name, location_id FROM entities WHERE location_id = ?"
            " ORDER BY id",
            (location_id,),
        ).fetchall()
        return [Entity(r["id"], r["kind"], r["name"], r["location_id"]) for r in rows]

    # ---- facts (spec 2.1, 2.2) ----

    def live_fact(self, entity_id: str, key: str) -> Fact | None:
        row = self.db.execute(
            "SELECT * FROM facts WHERE entity_id = ? AND key = ? AND superseded_by IS NULL",
            (entity_id, key),
        ).fetchone()
        return _row_to_fact(row) if row else None

    def live_facts(self, entity_id: str) -> list[Fact]:
        rows = self.db.execute(
            "SELECT * FROM facts WHERE entity_id = ? AND superseded_by IS NULL"
            " ORDER BY id",
            (entity_id,),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]

    def history(self, entity_id: str, key: str) -> list[Fact]:
        """Every fact ever written for this key, superseded ones included."""
        rows = self.db.execute(
            "SELECT * FROM facts WHERE entity_id = ? AND key = ? ORDER BY id",
            (entity_id, key),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]

    def add_fact(
        self,
        entity_id: str,
        key: str,
        value: str | float,
        source: str,
        visibility: str,
        cause: str,
    ) -> Fact:
        """Settle a fact for the first time. Rejects a second live fact."""
        if source not in SOURCES:
            raise StoreError(f"unknown source {source!r}")
        if visibility not in VISIBILITIES:
            raise StoreError(f"unknown visibility {visibility!r}")
        if not cause:
            raise StoreError("every fact must cite a cause")
        if self.entity(entity_id) is None:
            raise StoreError(f"no entity {entity_id!r} to hold a fact")
        existing = self.live_fact(entity_id, key)
        if existing is not None:
            raise FactConflict(
                f"{entity_id} already has a live {key!r} "
                f"({existing.value!r}); supersede it instead"
            )
        return self._insert(entity_id, key, value, source, visibility, cause)

    def supersede(
        self,
        entity_id: str,
        key: str,
        value: str,
        source: str,
        visibility: str,
        cause: str,
    ) -> Fact:
        """Replace a text fact with a new one that cites its cause."""
        old = self.live_fact(entity_id, key)
        if old is None:
            raise StoreError(f"{entity_id} has no live {key!r} to supersede")
        if isinstance(old.value, (int, float)):
            raise StoreError(
                f"{key!r} is a quantity; change it with apply_delta so no total "
                "is ever written directly"
            )
        if not cause:
            raise StoreError("a supersession must cite its cause")
        new = self._insert(entity_id, key, value, source, visibility, cause)
        self.db.execute(
            "UPDATE facts SET superseded_by = ? WHERE id = ?", (new.id, old.id)
        )
        self._carry_self_knowledge(old, new)
        self._commit()
        return new

    def apply_delta(self, entity_id: str, key: str, delta: float, cause: str) -> float:
        """The only way a quantity changes. Returns the new total."""
        old = self.live_fact(entity_id, key)
        if old is None:
            raise StoreError(f"{entity_id} has no live {key!r} to change")
        if not isinstance(old.value, (int, float)):
            raise StoreError(f"{key!r} is not a quantity")
        if not cause:
            raise StoreError("a delta must cite its cause")
        total = old.value + delta
        new = self._insert(
            entity_id, key, total, old.source, old.visibility, cause
        )
        self.db.execute(
            "UPDATE facts SET superseded_by = ? WHERE id = ?", (new.id, old.id)
        )
        self._carry_self_knowledge(old, new)
        self._commit()
        return total

    def _carry_self_knowledge(self, old: Fact, new: Fact) -> None:
        """Move the subject's own knowledge onto the fact that replaced it.

        A knowledge row used to point at a fact forever, so a player who had
        learned `cash_usd = 41` still believed 41 after spending six of it: the
        narrator and the sheet were handed a superseded number, which undoes
        what rule 4 is for.

        Only the subject's own row moves, and only when it holds no divergent
        belief. The narrowness is the point. You are present for every change to
        your own cash and your own injuries, so you know them; you are not
        present when your landlord changes the lock, and spec 6.5 wants you to
        find that out by putting a key in it. A row that holds a belief is a
        snapshot of what someone was told, so it never moves — that is where
        lies and stale rumours live.
        """
        self.db.execute(
            "INSERT OR IGNORE INTO knowledge (mind_id, fact_id, learned_at, how)"
            " SELECT mind_id, ?, ?, how FROM knowledge"
            " WHERE fact_id = ? AND mind_id = ?"
            "   AND belief_text IS NULL AND belief_num IS NULL",
            (new.id, _iso(self.now()), old.id, old.entity_id),
        )

    def _insert(
        self,
        entity_id: str,
        key: str,
        value: str | float,
        source: str,
        visibility: str,
        cause: str,
    ) -> Fact:
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        cursor = self.db.execute(
            "INSERT INTO facts (entity_id, key, value_text, value_num, source,"
            " visibility, cause, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                key,
                None if numeric else str(value),
                float(value) if numeric else None,
                source,
                visibility,
                cause,
                _iso(self.now()),
            ),
        )
        self._commit()
        return self.fact(cursor.lastrowid)

    def fact(self, fact_id: int) -> Fact:
        row = self.db.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if row is None:
            raise StoreError(f"no fact {fact_id}")
        return _row_to_fact(row)

    # ---- transient state (spec 2.1) ----

    def set_state(self, entity_id: str, key: str, value: str, cause: str) -> State:
        """Write what is true of this instant. Overwrites, never refuses.

        The opposite of add_fact on purpose. A fact is permanent and a second
        live one is a conflict; transient state is current state, so the only
        sensible answer to a new value is to take it. A playtest settled
        `posture = "sitting on a bench"` as a fact and the refusal of every
        correction after it meant the character was still sitting on that bench
        at home in their kitchen.
        """
        if self.entity(entity_id) is None:
            raise StoreError(f"no entity {entity_id!r} to hold state")
        if not cause:
            raise StoreError("every state change must cite a cause")
        now = _iso(self.now())
        self.db.execute(
            "INSERT INTO state (entity_id, key, value, set_at, cause)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (entity_id, key) DO UPDATE SET"
            "   value = excluded.value, set_at = excluded.set_at,"
            "   cause = excluded.cause",
            (entity_id, key, str(value), now, cause),
        )
        self._commit()
        return State(entity_id, key, str(value), _parse(now), cause)

    def state(self, entity_id: str) -> list[State]:
        """Everything transient about this entity right now, oldest key first."""
        rows = self.db.execute(
            "SELECT * FROM state WHERE entity_id = ? ORDER BY key", (entity_id,)
        ).fetchall()
        return [
            State(
                entity_id=r["entity_id"],
                key=r["key"],
                value=r["value"],
                set_at=_parse(r["set_at"]),
                cause=r["cause"],
            )
            for r in rows
        ]

    def state_of(self, entity_id: str, key: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM state WHERE entity_id = ? AND key = ?",
            (entity_id, key),
        ).fetchone()
        return row["value"] if row else None

    def clear_state(self, entity_id: str, keys: frozenset[str] | None = None) -> list[str]:
        """Drop transient state. Returns the keys that were actually dropped.

        Nothing is lost by this, which is what makes it safe: transient state
        is never history. Used when a move makes a value meaningless — you
        cannot still be sitting on a bench you have walked away from.
        """
        held = [s.key for s in self.state(entity_id)]
        going = held if keys is None else [k for k in held if k in keys]
        for key in going:
            self.db.execute(
                "DELETE FROM state WHERE entity_id = ? AND key = ?", (entity_id, key)
            )
        self._commit()
        return going

    # ---- the timeline (spec 6.2) ----

    def schedule(
        self, at: datetime, what: str, cause: str, reaches_player: bool = False
    ) -> Scheduled:
        """Put something on the clock. It fires when the world time passes it."""
        if not cause:
            raise StoreError("every timeline entry must cite a cause")
        cursor = self.db.execute(
            "INSERT INTO timeline (at, what, cause, reaches_player) VALUES (?, ?, ?, ?)",
            (_iso(at), what, cause, 1 if reaches_player else 0),
        )
        self._commit()
        return self.scheduled(cursor.lastrowid)

    def scheduled(self, event_id: int) -> Scheduled:
        row = self.db.execute(
            "SELECT * FROM timeline WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"no timeline entry {event_id}")
        return _row_to_scheduled(row)

    def due(self, moment: datetime | None = None) -> list[Scheduled]:
        """Unfired entries the clock has reached, oldest first."""
        rows = self.db.execute(
            "SELECT * FROM timeline WHERE fired_at IS NULL AND at <= ? ORDER BY at, id",
            (_iso(moment or self.now()),),
        ).fetchall()
        return [_row_to_scheduled(r) for r in rows]

    def pending(self) -> list[Scheduled]:
        """Everything still ahead, for the dev view."""
        rows = self.db.execute(
            "SELECT * FROM timeline WHERE fired_at IS NULL ORDER BY at, id"
        ).fetchall()
        return [_row_to_scheduled(r) for r in rows]

    def mark_fired(self, event_id: int) -> None:
        self.db.execute(
            "UPDATE timeline SET fired_at = ? WHERE id = ?",
            (_iso(self.now()), event_id),
        )
        self._commit()

    # ---- knowledge (spec 2.3, 2.4) ----

    def learn(
        self,
        mind_id: str,
        fact_id: int,
        how: str,
        belief: str | float | None = None,
    ) -> None:
        """Record that a mind perceived or was told a fact, truly or otherwise."""
        numeric = isinstance(belief, (int, float)) and not isinstance(belief, bool)
        self.db.execute(
            "INSERT OR IGNORE INTO knowledge (mind_id, fact_id, learned_at, how,"
            " belief_text, belief_num) VALUES (?, ?, ?, ?, ?, ?)",
            (
                mind_id,
                fact_id,
                _iso(self.now()),
                how,
                None if belief is None or numeric else str(belief),
                float(belief) if numeric else None,
            ),
        )
        self._commit()

    def known(self, mind_id: str) -> list[Known]:
        """Everything this mind holds, one belief per fact key (rule 5).

        A mind can end up holding two rows for the same key — it learned the
        old fact and then learned the one that superseded it — and the latest
        is the one it believes. Without the dedupe the narrator was handed both
        the old cash and the new. A row pointing at a superseded fact survives
        on its own, though: that is a mind still believing the lock is the lock
        it last saw.
        """
        rows = self.db.execute(
            "SELECT k.learned_at, k.how, k.belief_text, k.belief_num, f.*"
            " FROM knowledge k JOIN facts f ON f.id = k.fact_id"
            " WHERE k.mind_id = ? AND k.id = ("
            "     SELECT MAX(k2.id) FROM knowledge k2"
            "     JOIN facts f2 ON f2.id = k2.fact_id"
            "     WHERE k2.mind_id = k.mind_id"
            "       AND f2.entity_id = f.entity_id AND f2.key = f.key"
            " ) ORDER BY k.id",
            (mind_id,),
        ).fetchall()
        known = []
        for row in rows:
            belief = row["belief_text"] if row["belief_num"] is None else row["belief_num"]
            known.append(
                Known(
                    fact=_row_to_fact(row),
                    learned_at=_parse(row["learned_at"]),
                    how=row["how"],
                    belief=belief,
                )
            )
        return known

    # ---- lives (spec 9) ----

    def start_life(self, character_id: str) -> Life:
        cursor = self.db.execute(
            "INSERT INTO lives (character_id, started_at) VALUES (?, ?)",
            (character_id, _iso(self.now())),
        )
        self._commit()
        return self.life(cursor.lastrowid)

    def end_life(self, life_id: int, ending: str) -> None:
        """Ends a life. Touches no world fact (rule 8)."""
        self.db.execute(
            "UPDATE lives SET ended_at = ?, ending = ? WHERE id = ?",
            (_iso(self.now()), ending, life_id),
        )
        self._commit()

    def life(self, life_id: int) -> Life:
        row = self.db.execute("SELECT * FROM lives WHERE id = ?", (life_id,)).fetchone()
        if row is None:
            raise StoreError(f"no life {life_id}")
        return Life(
            id=row["id"],
            character_id=row["character_id"],
            started_at=_parse(row["started_at"]),
            ended_at=_parse(row["ended_at"]),
            ending=row["ending"],
        )

    def current_life(self) -> Life | None:
        row = self.db.execute(
            "SELECT id FROM lives WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self.life(row["id"]) if row else None

    # ---- the turn log (spec 13.5, and resuming) ----

    def log_turn(
        self,
        life_id: int,
        player_said: str,
        intent: str | None,
        roll_json: str | None,
        narration: str | None,
        mode: str | None = None,
        state_json: str | None = None,
    ) -> int:
        """Record the turn. `state_json` is every transient change it made.

        Transient state keeps no history of its own, so this is the history:
        spec 2.1 asks that a change still be readable afterwards, and the dev
        view and any later audit read it from here.
        """
        cursor = self.db.execute(
            "INSERT INTO turns (life_id, world_time, player_said, intent, roll_json,"
            " narration, mode, state_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                life_id, _iso(self.now()), player_said, intent, roll_json,
                narration, mode, state_json,
            ),
        )
        self._commit()
        return cursor.lastrowid

    def recent_turns(self, life_id: int, limit: int) -> list[sqlite3.Row]:
        """Most recent turns, oldest first, for resuming (build step 9)."""
        rows = self.db.execute(
            "SELECT * FROM turns WHERE life_id = ? ORDER BY id DESC LIMIT ?",
            (life_id, limit),
        ).fetchall()
        return list(reversed(rows))
