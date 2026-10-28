"""Portals to Matrix: a room for an Unseenet group (UN-RFC 0037 section 7).

What is the same for every network (the manifest, the delay, what may be
exported) is in ``core/portal.py``. This is only the Matrix side: create the
room on the gateway's own homeserver, keep its name and topic current, send
and read.

The room is public and unencrypted, listed in the server's room directory,
and created by the gateway's account, which keeps the power to change its
name and topic. People named in the portal's `admins` are invited and get
the same power, so the room can be moderated by people, not only by the bot.
"""

from __future__ import annotations

import logging

from unseenet.core.article import Article

from ..core.adapter import Counterpart, ExportError
from ..core.state import State
from .client import Matrix, MatrixError, q
from .convert import NETWORK
from .exporter import MatrixSender
from .feed import MatrixFeed, openness

log = logging.getLogger(__name__)


def portal_topic(name: str, group_id: str) -> str:
    return (f"Portal to the Unseenet group {name} ({group_id}). Messages are copied both "
            "ways by an Unseenet gateway: what you write here appears in Unseenet, signed by "
            "the gateway, and Unseenet posts appear here with their author's name. Copies "
            "arrive seconds to minutes late, on purpose. Images and files are not carried. "
            "https://unseenet.org")


class MatrixCounterpart(Counterpart):
    network = NETWORK

    def __init__(self, *, state: State, matrix: Matrix, alias: str, group_id: str,
                 admins: list[str] | None = None, max_pages: int = 10):
        if not alias.startswith("#") or ":" not in alias:
            raise ValueError(f"a Matrix portal needs a room alias, not {alias!r}")
        self.state, self.matrix, self.alias, self.group_id = state, matrix, alias, group_id
        self.source = alias
        self.admins = list(admins or [])
        self.feed = MatrixFeed(state=state, matrix=matrix, carried={alias: group_id},
                               start="new", max_pages=max_pages, label=f"portal {group_id}")
        self.sender = MatrixSender(matrix=matrix, state=state, rooms={group_id: alias},
                                   room_id_of=self.feed.room_id)
        self._meta_for = ""

    async def _create(self, name: str, topic: str) -> str:
        me = await self.matrix.whoami()
        local, _, server = self.alias[1:].partition(":")
        if me.partition(":")[2] != server:
            raise ExportError(f"{self.alias} is not on the gateway's own server; "
                              "a portal room is created there")
        # the creator has full power already (and room v12 forbids listing it)
        power = {"users": {a: 100 for a in self.admins}}
        rid = await self.matrix.create_room({
            "room_alias_name": local, "name": name, "topic": topic,
            "preset": "public_chat", "visibility": "public",
            "invite": self.admins, "power_level_content_override": power})
        log.info("portal room %s created as %s", self.alias, rid)
        return rid

    async def ensure(self, manifest) -> None:
        """Create the room if needed; bring its name and topic up to date per manifest."""
        m = manifest
        if m is not None and self._meta_for == m.message_id:
            return
        name = f"{m.name if m else self.group_id} (Unseenet)"
        topic = portal_topic(m.name if m else self.group_id, self.group_id)
        try:
            rid = await self.matrix.resolve(self.alias)
        except MatrixError as exc:
            if exc.status != 404:
                raise
            rid = await self._create(name, topic)
        rid = await self.feed.room_id(self.alias)       # pins the alias to this room
        if await self.matrix.membership(rid) != "join":
            await self.matrix.join(rid)                 # our own public room: no invitation
        rules = await self.matrix.state(rid, "m.room.join_rules") or {}
        state = [{"type": "m.room.join_rules", "content": rules}]
        if await self.matrix.state(rid, "m.room.encryption") is not None:
            state.append({"type": "m.room.encryption", "content": {}})
        why = openness(state)
        if why:
            raise ExportError(f"portal room {self.alias} is {why}")
        for etype, content in (("m.room.name", {"name": name}), ("m.room.topic", {"topic": topic})):
            if (await self.matrix.state(rid, etype) or {}) != content:
                try:
                    await self.matrix.put_state(rid, etype, content)
                except MatrixError as exc:
                    log.warning("portal %s: cannot set %s: %s", self.alias, etype, exc)
        await self._ensure_admins(rid)
        self._meta_for = m.message_id if m else ""
        log.info("portal counterpart %s ready", self.alias)

    async def _ensure_admins(self, rid: str) -> None:
        """Admins named in the configuration get full power and an invitation, also
        when they are added after the room was created. Nobody is ever demoted here."""
        if not self.admins:
            return
        pl = await self.matrix.state(rid, "m.room.power_levels") or {}
        users = dict(pl.get("users") or {})
        missing = [a for a in self.admins if users.get(a, 0) < 100]
        if missing:
            users.update({a: 100 for a in missing})
            try:
                await self.matrix.put_state(rid, "m.room.power_levels", {**pl, "users": users})
            except MatrixError as exc:
                log.warning("portal %s: cannot make %s admins: %s", self.alias, missing, exc)
        for a in self.admins:
            m = await self.matrix.state(rid, "m.room.member", a)
            if (m or {}).get("membership") not in ("join", "invite"):
                try:
                    await self.matrix.call("POST", f"/rooms/{q(rid)}/invite", body={"user_id": a})
                except MatrixError as exc:
                    log.warning("portal %s: cannot invite %s: %s", self.alias, a, exc)

    async def send(self, art: Article, files=()) -> str:
        return await self.sender.send(art, files)
