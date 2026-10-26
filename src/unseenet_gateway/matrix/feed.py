"""Reading Matrix rooms through one bot account (UN-RFC 0037 sections 4 and 6).

Before each pass over a room the feed checks the other side's consent and
the room's openness (UN-RFC 0023 sections 4.1 and 6.1):

- the gateway is a member of the room, because the room's moderators
  invited it. It joins only rooms it is invited to and that are in its
  configuration, and stops importing as soon as it is kicked or banned;
- the room's join rule is `public`;
- the room is not encrypted (`m.room.encryption` absent).

The timeline is read newest first with /messages until the cursor (the
newest `origin_server_ts` imported, in milliseconds) and imported oldest
first. Redactions seen on the way become cancels.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from unseenet.core.article import Article

from ..core.adapter import Feed, Incoming, Retraction, Source
from ..core.state import State
from .client import Matrix, MatrixError
from .convert import ENCRYPTED, GATEWAY_NOTICE_KEY, MESSAGE, NETWORK, REACTION, REDACTION, convert

log = logging.getLogger(__name__)
PAGE = 100


class RoomMoved(Exception):
    """A configured alias names a different room than at the first import."""


def openness(state: list[dict]) -> str | None:
    """Why a room may not be gatewayed, from its state events; None if it may."""
    by_type = {e.get("type"): e.get("content") or {} for e in state}
    if by_type.get("m.room.create", {}).get("type") == "m.space":
        return "a space, not a room"            # spaces hold rooms; nobody chats in them
    if "m.room.encryption" in by_type:
        return "encrypted"
    rule = by_type.get("m.room.join_rules", {}).get("join_rule")
    if rule is None:
        return "of unknown join rule"
    if rule != "public":
        return f"not public (join rule {rule!r})"
    return None


MEDIA_LIMIT = 8 * 1024 * 1024        # never download more for one picture


class MatrixFeed(Feed):
    network = NETWORK

    def __init__(self, *, state: State, matrix: Matrix, carried: dict[str, str],
                 start: str = "new", max_pages: int = 10, announce: dict[str, str] | None = None,
                 label: str | None = None):
        self.state, self.matrix, self.carried = state, matrix, carried   # room -> group id
        self.start, self.max_pages = start, max_pages
        self.announce = announce or {}         # room -> notice posted once on joining
        self.label = label or f"matrix {matrix.base}"
        self.room_ids: dict[str, str] = {}     # alias or id -> room id
        self.names: dict[tuple[str, str], str | None] = {}
        self._redactions: dict[str, list[tuple[str, str]]] = {}   # room -> (target, sender)
        self._invites: dict[str, list[dict]] | None = None

    def sources(self) -> list[Source]:
        self._invites = None                   # called once per pass: look again
        return [Source(r, gid) for r, gid in self.carried.items()]

    # --- consent and openness ---
    async def room_id(self, room: str) -> str:
        """The room an alias names, pinned at first use (UN-RFC 0037 section 3).

        An alias can be moved to another room. The gateway keeps carrying the
        room it first found; if the alias now names another, it stops, and the
        operator decides.
        """
        if room not in self.room_ids:
            rid = await self.matrix.resolve(room)
            db = self.state.db
            db.execute("CREATE TABLE IF NOT EXISTS matrix_rooms "
                       "(source TEXT PRIMARY KEY, room_id TEXT NOT NULL)")
            row = db.execute("SELECT room_id FROM matrix_rooms WHERE source=?", (room,)).fetchone()
            if row is None:
                db.execute("INSERT INTO matrix_rooms VALUES (?,?)", (room, rid))
                db.commit()
            elif row[0] != rid:
                raise RoomMoved(f"{room} now names {rid}, not {row[0]}")
            self.room_ids[room] = rid
        return self.room_ids[room]

    async def ensure_joined(self, room: str) -> str | None:
        """The room id if the gateway is (or now becomes) a member, else None.

        An invitation is accepted only if the invite state shows a public,
        unencrypted room; otherwise it is declined, so the room's moderators
        see that the gateway will not carry it.
        """
        rid = await self.room_id(room)
        membership = await self.matrix.membership(rid)
        if membership == "join":
            return rid
        if membership in ("leave", "ban"):
            log.warning("%s: the gateway is not in the room (%s: kicked, banned or declined); "
                        "not gatewayed until invited again", room, membership)
            return None
        if self._invites is None:
            self._invites = await self.matrix.invites()
        if rid not in self._invites:
            log.warning("%s: not invited; ask the room's moderators to invite %s",
                        room, await self.matrix.whoami())
            return None
        refusal = openness(self._invites[rid])
        if refusal:
            log.warning("%s: invitation declined: %s", room, refusal)
            await self.matrix.leave(rid, "Unseenet gateways carry only public, unencrypted "
                                         f"rooms: this one is {refusal}.")
            return None
        await self.matrix.join(rid)
        log.info("%s: joined on invitation", room)
        if self.state.get_cursor(NETWORK, rid) is not None:
            # Back after a kick: what was written while the gateway was out of
            # the room was written without its consent, and stays out.
            self.state.set_cursor(NETWORK, rid, int(time.time() * 1000))
        if room in self.announce:
            try:
                await self.matrix.send(rid, {"msgtype": "m.notice", "body": self.announce[room],
                                             GATEWAY_NOTICE_KEY: True},
                                       txn_id=f"unseenet-announce-{int(time.time())}")
            except MatrixError as exc:     # e.g. a room where only admins may write
                log.warning("%s: could not post the notice that the room is gatewayed (%s); "
                            "importing anyway, but export needs the right to write", room, exc)
        return rid

    async def room_is_public(self, room: str, rid: str) -> bool:
        """Checked every pass: a room can become private or encrypted later."""
        state = [{"type": "m.room.join_rules",
                  "content": await self.matrix.state(rid, "m.room.join_rules") or {}},
                 {"type": "m.room.create",
                  "content": await self.matrix.state(rid, "m.room.create") or {}}]
        enc = await self.matrix.state(rid, "m.room.encryption")
        if enc is not None:
            state.append({"type": "m.room.encryption", "content": enc})
        refusal = openness(state)
        if refusal:
            log.warning("%s: %s; not gatewayed", room, refusal)
            return False
        return True

    # --- lookups ---
    async def _fill_names(self, rid: str, events: list[dict]) -> None:
        for sender in {e.get("sender", "") for e in events}:
            if (rid, sender) in self.names:
                continue
            try:
                m = await self.matrix.state(rid, "m.room.member", sender) or {}
            except MatrixError:
                m = {}
            name = m.get("displayname")
            self.names[(rid, sender)] = " ".join(name.split())[:64] \
                if isinstance(name, str) and name.strip() else None

    # --- import ---
    async def fetch(self, source: Source) -> AsyncIterator[Incoming]:
        room = source.foreign
        self._redactions[room] = []
        try:
            rid = await self.ensure_joined(room)
        except RoomMoved as exc:
            log.warning("%s: the alias was moved to another room (%s); not gatewayed. "
                        "Gateway the new room under a new name, or remove the old entry "
                        "from the state's matrix_rooms table if the move is legitimate",
                        room, exc)
            return
        if rid is None or not await self.room_is_public(room, rid):
            return
        since = self.state.get_cursor(NETWORK, rid)
        if since is None and self.start == "new":
            self.state.set_cursor(NETWORK, rid, int(time.time() * 1000))
            return
        events: list[dict] = []
        token, done = None, False
        for _ in range(self.max_pages):
            page = await self.matrix.messages(rid, start=token, limit=PAGE,
                                              types=[MESSAGE, REDACTION, ENCRYPTED, REACTION])
            for ev in page.get("chunk", []):
                if since is not None and ev.get("origin_server_ts", 0) < since:
                    done = True
                    break
                ev.setdefault("room_id", rid)
                events.append(ev)
            token = page.get("end")
            if done or not token or not page.get("chunk"):
                break
        events.reverse()                                     # oldest first
        for ev in events:
            if ev.get("type") == REDACTION:
                target = ev.get("redacts") or (ev.get("content") or {}).get("redacts")
                if target:
                    self._redactions[room].append((target, ev.get("sender", "")))
        msgs = [e for e in events if e.get("type") != REDACTION]
        await self._fill_names(rid, msgs)
        newest = since or 0
        for ev in msgs:
            newest = max(newest, ev.get("origin_server_ts", 0))
            ts = ev.get("origin_server_ts")
            yield Incoming(ev.get("event_id"), ev, label=ev.get("event_id", "")[:20],
                           when=ts / 1000 if isinstance(ts, int) else None)
        if newest:
            self.state.set_cursor(NETWORK, rid, newest)

    def convert(self, source: Source, item: Incoming) -> Article:
        rid = item.payload.get("room_id", "")
        return convert(item.payload, group_id=source.group_id,
                       id_map=lambda eid: self.state.lookup(NETWORK, eid),
                       name_of=lambda uid: self.names.get((rid, uid)),
                       gateway_user=self.matrix.user_id)

    async def media(self, source: Source, item: Incoming) -> list:
        """The picture of an ``m.image`` message, downloaded (gateway#26)."""
        from ..core.files import Media
        content = (item.payload or {}).get("content") or {}
        url, info = content.get("url"), content.get("info") or {}
        if content.get("msgtype") != "m.image" or not isinstance(url, str):
            return []
        size = info.get("size") if isinstance(info, dict) else None
        if isinstance(size, int) and size > MEDIA_LIMIT:
            return []
        name = content.get("filename") or content.get("body") or "picture"
        alt = content.get("body", "") if content.get("filename") else ""
        octets = await self.matrix.download(url, MEDIA_LIMIT)
        return [Media(str(name)[:128], octets, str(alt)[:512])]

    def hold(self, source: Source, when: float) -> None:
        """Messages from ``when`` on wait for this gateway's turn: read them again."""
        rid = self.room_ids.get(source.foreign)
        if rid is None:
            return
        held = int(when * 1000) - 1
        cur = self.state.get_cursor(NETWORK, rid)
        if cur is None or held < cur:
            self.state.set_cursor(NETWORK, rid, held)

    def imported(self, item: Incoming, art: Article) -> None:
        self.state.remember_author(NETWORK, item.foreign_id, item.payload.get("sender", ""))

    async def retractions(self, source: Source) -> list[Retraction]:
        """Redactions in the room of messages that were imported.

        The room's homeserver accepted the redaction, so it came from the
        author or from someone with the power to redact: the room's own
        authority (UN-RFC 0023 section 4.5).
        """
        out = []
        for target, sender in self._redactions.pop(source.foreign, []):
            mid = self.state.lookup(NETWORK, target)
            if not mid:
                continue
            reason = ("Removed by its author on Matrix."
                      if sender and sender == self.state.author_of(NETWORK, target)
                      else "Removed by the Matrix room's moderators.")
            out.append(Retraction(target, mid, reason))
        return out

    async def close(self) -> None:
        self._invites = None
        await self.matrix.close()
