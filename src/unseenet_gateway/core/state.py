"""The gateway's own records (UN-RFC 0023, sections 4.4 and 6.2).

One SQLite file:

- ``ids``: foreign Message-ID <-> Unseenet Message-ID, for threading and for
  never importing the same article twice;
- ``cursor``: how far each source group has been read;
- ``authors``: for networks that sign retractions, who wrote an imported message;
- ``skipped``: counts of articles not imported, by reason (no content kept).

Nothing here says anything about who wrote an article beyond what was public
in the article itself.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ids (
    network     TEXT NOT NULL,
    foreign_id  TEXT NOT NULL,
    unseenet_id TEXT NOT NULL,
    group_id    TEXT NOT NULL,
    direction   TEXT NOT NULL,          -- 'in' or 'out'
    at          INTEGER NOT NULL,
    PRIMARY KEY (network, foreign_id)
);
CREATE INDEX IF NOT EXISTS ids_unseenet ON ids (unseenet_id);
CREATE TABLE IF NOT EXISTS cursor (
    network TEXT NOT NULL,
    source  TEXT NOT NULL,
    high    INTEGER NOT NULL,
    PRIMARY KEY (network, source)
);
CREATE TABLE IF NOT EXISTS authors (
    network    TEXT NOT NULL,
    foreign_id TEXT NOT NULL,
    author     TEXT NOT NULL,          -- the author's id in the other network (public there)
    PRIMARY KEY (network, foreign_id)
);
CREATE TABLE IF NOT EXISTS vanished (
    network    TEXT NOT NULL,
    foreign_id TEXT NOT NULL,
    passes     INTEGER NOT NULL,       -- consecutive passes the source no longer served it
    PRIMARY KEY (network, foreign_id)
);
CREATE TABLE IF NOT EXISTS skipped (
    network TEXT NOT NULL,
    reason  TEXT NOT NULL,
    count   INTEGER NOT NULL,
    PRIMARY KEY (network, reason)
);
"""


class State:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def lookup(self, network: str, foreign_id: str) -> str | None:
        row = self.db.execute("SELECT unseenet_id FROM ids WHERE network=? AND foreign_id=?",
                              (network, foreign_id)).fetchone()
        return row[0] if row else None

    def foreign_for(self, unseenet_id: str) -> tuple[str, str] | None:
        row = self.db.execute("SELECT network, foreign_id FROM ids WHERE unseenet_id=?",
                              (unseenet_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def group_for(self, unseenet_id: str) -> str | None:
        """The Unseenet group a message was imported into or exported from."""
        row = self.db.execute("SELECT group_id FROM ids WHERE unseenet_id=?",
                              (unseenet_id,)).fetchone()
        return row[0] if row else None

    def remember(self, network: str, foreign_id: str, unseenet_id: str, group_id: str,
                 direction: str = "in") -> None:
        self.db.execute("INSERT OR REPLACE INTO ids VALUES (?,?,?,?,?,?)",
                        (network, foreign_id, unseenet_id, group_id, direction, int(time.time())))
        self.db.commit()

    def remember_author(self, network: str, foreign_id: str, author: str) -> None:
        """For networks with signed retractions: who may retract this message."""
        self.db.execute("INSERT OR REPLACE INTO authors VALUES (?,?,?)",
                        (network, foreign_id, author))
        self.db.commit()

    def author_of(self, network: str, foreign_id: str) -> str | None:
        row = self.db.execute("SELECT author FROM authors WHERE network=? AND foreign_id=?",
                              (network, foreign_id)).fetchone()
        return row[0] if row else None

    def seen_missing(self, network: str, foreign_id: str) -> int:
        """Count one more pass in which the source did not serve this message."""
        self.db.execute("INSERT INTO vanished VALUES (?,?,1) ON CONFLICT(network, foreign_id) "
                        "DO UPDATE SET passes = passes + 1", (network, foreign_id))
        self.db.commit()
        return self.db.execute("SELECT passes FROM vanished WHERE network=? AND foreign_id=?",
                               (network, foreign_id)).fetchone()[0]

    def seen_present(self, network: str, foreign_id: str) -> None:
        self.db.execute("DELETE FROM vanished WHERE network=? AND foreign_id=?",
                        (network, foreign_id))

    def get_cursor(self, network: str, source: str) -> int | None:
        row = self.db.execute("SELECT high FROM cursor WHERE network=? AND source=?",
                              (network, source)).fetchone()
        return row[0] if row else None

    def set_cursor(self, network: str, source: str, high: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO cursor VALUES (?,?,?)", (network, source, high))
        self.db.commit()

    def count_skip(self, network: str, reason: str) -> None:
        self.db.execute("INSERT INTO skipped VALUES (?,?,1) ON CONFLICT(network, reason) "
                        "DO UPDATE SET count = count + 1", (network, reason))
        self.db.commit()

    def stats(self) -> dict:
        out = {"imported": dict(self.db.execute(
            "SELECT network, COUNT(*) FROM ids WHERE direction='in' GROUP BY network").fetchall()),
               "skipped": {}}
        for net, reason, n in self.db.execute("SELECT network, reason, count FROM skipped"):
            out["skipped"].setdefault(net, {})[reason] = n
        return out
