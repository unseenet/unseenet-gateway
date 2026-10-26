"""Reactions going out (UN-RFC 0048 section 9, #32).

A gateway speaks as one user of the other network, and those networks count one
reaction per sender, so it shows *that* readers in Unseenet reacted, never how many:
while at least one key in Unseenet gives an emoji to a message that came in from the
other network, the gateway's own reaction with that emoji is on it there; when the last
one takes it back, the gateway takes its own back.

Reactions in a moderated group go straight into it, not through the submission queue
(UN-RFC 0048 section 4.4), so the mirror reads each exporting group's new articles from
the node, keeps the reactions of keys other than the gateway's (its own are imports) in
the gateway's state, counts them with the core's rules (one per key, the latest, a
cancel or a change taking one back), and compares with what it has sent.

The adapters' senders do the sending: ``react(group, target_foreign_id, emoji) ->
foreign id`` and ``unreact(group, foreign_id)``.
"""

from __future__ import annotations

import logging
from email.utils import parsedate_to_datetime

from unseenet.core import reactions as rx
from unseenet.core.article import Article, ArticleError
from unseenet.core.groups import message_kind

log = logging.getLogger(__name__)
PER_PASS = 500                    # articles read per group in one pass
CURSOR = "unseenet-reactions"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reactions_in (
    grp TEXT NOT NULL, mid TEXT PRIMARY KEY, key TEXT NOT NULL, target TEXT NOT NULL,
    emoji TEXT NOT NULL, at INTEGER NOT NULL, supersedes TEXT);
CREATE TABLE IF NOT EXISTS reactions_out (
    grp TEXT NOT NULL, target TEXT NOT NULL, emoji TEXT NOT NULL, foreign_id TEXT NOT NULL,
    PRIMARY KEY (grp, target, emoji));
"""


class ReactionMirror:
    """``targets``: {Unseenet Group ID: (network, send)} as the Exporter has them; a
    group whose sender has no ``react`` is left alone."""

    def __init__(self, *, state, node, gateway, targets: dict):
        self.state, self.node, self.gateway = state, node, gateway
        self.targets = targets
        self.state.db.executescript(SCHEMA)

    label = "reactions out"

    def _sender(self, gid: str):
        network, send = self.targets[gid]
        sender = getattr(send, "__self__", None)
        if sender is None or not hasattr(sender, "react"):
            return network, None
        return network, sender

    # --- reading what Unseenet readers give ---
    async def _read(self, gid: str) -> int:
        client = await self.node._conn()
        _, low, high = await client.group(gid)
        start = max((self.state.get_cursor(CURSOR, gid) or 0) + 1, low)
        end = min(high, start + PER_PASS - 1)
        db, seen = self.state.db, 0
        for n in range(start, end + 1):
            raw = await client.article(str(n))
            self.state.set_cursor(CURSOR, gid, n)
            if raw is None:
                continue
            try:
                art = Article.from_bytes(raw)
                art.verify_signature()
                key = art.get("Author-Key", "").strip()
            except (ArticleError, ValueError):
                continue
            if key == self.gateway.key_text:
                continue                              # imports and the gateway's own
            control = (art.get("Control") or "").split()
            if len(control) == 2 and control[0] == "cancel":
                # the cancelled one and those it replaced, or they would count again (as
                # the core does, UN-RFC 0042 section 4.2) - the author's own only
                mid = control[1]
                while mid:
                    row = db.execute("SELECT supersedes FROM reactions_in WHERE mid=? AND key=?",
                                     (mid, key)).fetchone()
                    db.execute("DELETE FROM reactions_in WHERE mid=? AND key=?", (mid, key))
                    mid = row[0] if row else None
                continue
            if message_kind(art) != rx.KIND_REACTION or art.get("Encrypted"):
                continue
            refs = (art.get("References") or "").split()
            emoji = (art.get("Reaction") or "").strip()
            if not refs or not rx.is_emoji(emoji):
                continue
            try:
                at = int(parsedate_to_datetime(art["Date"]).timestamp())
            except (KeyError, TypeError, ValueError):
                at = 0
            sup = (art.get("Supersedes") or "").split()
            db.execute("INSERT OR REPLACE INTO reactions_in VALUES (?,?,?,?,?,?,?)",
                       (gid, art.message_id, key, refs[-1], emoji, at, sup[0] if sup else None))
            seen += 1
        db.commit()
        return seen

    def _wanted(self, gid: str) -> dict[str, set[str]]:
        """{target Message-ID: the emoji at least one key gives it now}."""
        rows = self.state.db.execute(
            "SELECT mid, key, at, target, emoji, supersedes FROM reactions_in WHERE grp=?",
            (gid,)).fetchall()
        replaced = {r[5] for r in rows if r[5]}
        counts = rx.count((mid, key, at, target, emoji)
                          for mid, key, at, target, emoji, _ in rows if mid not in replaced)
        return {t: set(c.by_emoji) for t, c in counts.items() if c.by_emoji}

    def _came_in(self, network: str, target: str) -> str | None:
        """The foreign id of a message that came *in* from ``network``, else None."""
        row = self.state.db.execute(
            "SELECT foreign_id FROM ids WHERE unseenet_id=? AND network=? AND direction='in'",
            (target, network)).fetchone()
        return row[0] if row else None

    # --- a pass ---
    async def run(self) -> dict:
        stats = {"read": 0, "sent": 0, "taken_back": 0}
        for gid in self.targets:
            network, sender = self._sender(gid)
            if sender is None:
                continue
            try:
                stats["read"] += await self._read(gid)
            except Exception as exc:  # noqa: BLE001  (one group must not stop the others)
                log.warning("reactions in %s not read: %s", gid, exc)
                continue
            wanted = self._wanted(gid)
            have: dict[tuple[str, str], str] = {
                (t, e): f for t, e, f in self.state.db.execute(
                    "SELECT target, emoji, foreign_id FROM reactions_out WHERE grp=?", (gid,))}
            for target, emojis in wanted.items():
                foreign = self._came_in(network, target)
                if foreign is None:
                    continue                          # not from that network: nothing to show
                for emoji in emojis - {e for t, e in have if t == target}:
                    try:
                        fid = await sender.react(gid, foreign, emoji)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("reaction %s to %s not sent: %s", emoji, foreign, exc)
                        continue
                    self.state.db.execute("INSERT OR REPLACE INTO reactions_out VALUES (?,?,?,?)",
                                          (gid, target, emoji, fid))
                    stats["sent"] += 1
            for (target, emoji), fid in have.items():
                if emoji in wanted.get(target, set()):
                    continue
                try:
                    await sender.unreact(gid, fid)
                except Exception as exc:  # noqa: BLE001
                    log.warning("reaction %s not taken back: %s", fid, exc)
                    continue
                self.state.db.execute("DELETE FROM reactions_out WHERE grp=? AND target=? AND emoji=?",
                                      (gid, target, emoji))
                stats["taken_back"] += 1
            self.state.db.commit()
        return stats
