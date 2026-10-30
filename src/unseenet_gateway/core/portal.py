"""Portals: an Unseenet group with a counterpart on another network (UN-RFC 0023 3.5).

The Unseenet group is the source. Its owner opens a portal by listing it in
the group's manifest (`portals`); the gateway then

1. creates the counterpart on the other network (the adapter's job),
2. exports articles posted in the Unseenet group *after* the portal was
   opened (the authors of older ones were never warned), after a random
   delay, as for any export (section 5). The time an article is due is kept
   in the state, so it survives a restart and one-off passes (import-once),
3. imports what is written in the counterpart into the Unseenet group,
   signed by the gateway key and carrying Origin-* headers (section 4).

When the owner removes the portal from the manifest, the gateway stops both
ways at its next pass. Closed, inbox and local groups never get a portal,
and a moderated group only when the gateway key is one of its moderators.

Everything here is the same for every network; the network's side is a
:class:`~.adapter.Counterpart`.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field

from unseenet.core import crypto
from unseenet.core.article import Article, ArticleError
from unseenet.core.groups import Manifest
from unseenet.nntp import NNTPError, strip_reader_headers

from . import sharing
from .adapter import Counterpart, ExportError, is_text
from .importer import Importer
from .state import State
from .unseenet_side import Node

log = logging.getLogger(__name__)


@dataclass
class PortalStats:
    active: bool = False
    exported: int = 0
    waiting: int = 0
    imported: int = 0
    duplicate: int = 0
    skipped: int = 0
    refused: int = 0
    cancelled: int = 0


@dataclass
class _Pending:
    number: int
    due: float
    art: Article = field(repr=False)


def portal_entry(manifest: dict, network: str, source: str, gateway_key: str) -> dict | None:
    """The manifest's portal entry for this counterpart and gateway, if any."""
    for p in manifest.get("portals") or []:
        if not isinstance(p, dict):
            continue
        gws = p.get("gateway")
        gws = [gws] if isinstance(gws, str) else (gws or [])
        if p.get("network") == network and p.get("source") == source and gateway_key in gws:
            return p
    return None


def may_have_portal(m: Manifest, gateway_pk: bytes) -> str | None:
    """None if this group may have a portal run by this gateway, else why not."""
    if m.local:
        return "local group"
    if m.type == "open":
        return None
    if m.type == "moderated":
        return None if gateway_pk in m.moderators else "moderated, gateway is no moderator"
    return f"{m.type} group"


def exportable(art: Article, gateway_pk: bytes) -> str | None:
    """None if the article may go out through the portal, else why not (0023 5.4)."""
    if art.control:
        return "control"
    if "Encrypted" in art:
        return "encrypted"
    if art.author_key == gateway_pk or "Origin-Network" in art:
        return "came from another network"
    if sharing.is_notice(art):            # never carried (UN-RFC 0060 section 9)
        return "share notice"
    if not is_text(art):
        return "not text"
    if not art.body.strip():
        return "empty"
    return None


class Portal:
    def __init__(self, *, state: State, node: Node, gateway: crypto.Identity,
                 counterpart: Counterpart, group_id: str, pow_bits: int,
                 delay_max: float = 600, pictures_max: int = 0):
        self.state, self.node, self.gateway = state, node, gateway
        self.counterpart, self.group_id = counterpart, group_id
        self.network, self.source = counterpart.network, counterpart.source
        self.delay_max = delay_max
        self.importer = Importer(feed=counterpart.feed, state=state, node=node,
                                 gateway=gateway, pow_bits=pow_bits,
                                 pictures_max=pictures_max)
        self.label = f"portal {group_id}"
        self.manifest: Manifest | None = None
        self.pending: dict[str, _Pending] = {}
        self._warned = ""
        # One group can have portals to several networks; each keeps its own
        # read cursor, opening point and delays (a portal opened later must not
        # inherit an earlier one's opening, or older posts would go out).
        tag = f"{group_id}|{self.network}|{self.source}"
        self.cursor_key = f"portal:{tag}"
        self.opened_key = f"portal-opened:{tag}"
        self.due_prefix = f"portal-due:{tag}:"
        self._migrate(group_id)

    def _migrate(self, group_id: str) -> None:
        """Before portals were kept apart per network only Nostr had them, under keys
        named by the group alone; a Nostr portal takes those over once."""
        if self.network != "nostr" or self.state.get_cursor("unseenet", self.cursor_key) is not None:
            return
        db = self.state.db
        for old, new in ((f"portal:{group_id}", self.cursor_key),
                         (f"portal-opened:{group_id}", self.opened_key)):
            db.execute("UPDATE cursor SET source=? WHERE network='unseenet' AND source=?",
                       (new, old))
        db.execute("UPDATE cursor SET source=? || substr(source, ?) "
                   "WHERE network='unseenet' AND source LIKE ?",
                   (self.due_prefix, len(f"portal-due:{group_id}:") + 1,
                    f"portal-due:{group_id}:%"))
        db.commit()

    def _sent(self, mid: str) -> bool:
        """Whether this portal's network already has ``mid`` (another portal's doesn't count)."""
        return self.state.db.execute(
            "SELECT 1 FROM ids WHERE network=? AND unseenet_id=?",
            (self.network, mid)).fetchone() is not None

    # --- the delay, kept in the state ---
    def _due_key(self, mid: str) -> str:
        return f"{self.due_prefix}{mid}"

    def _due(self, mid: str, now: float) -> float:
        """When ``mid`` may go out: drawn once, then the same on every pass."""
        key = self._due_key(mid)
        due = self.state.get_cursor("unseenet", key)
        if due is None:
            due = math.floor(now + random.uniform(0, self.delay_max))
            self.state.set_cursor("unseenet", key, due)
        return float(due)

    def _forget_due(self, mid: str | None = None) -> None:
        """Forget one article's due time, or (None) all of this portal's."""
        if mid is None:
            self.state.db.execute("DELETE FROM cursor WHERE network=? AND source LIKE ?",
                                  ("unseenet", f"{self.due_prefix}%"))
        else:
            self.state.db.execute("DELETE FROM cursor WHERE network=? AND source=?",
                                  ("unseenet", self._due_key(mid)))
        self.state.db.commit()

    # --- the Unseenet side ---
    async def _read_group(self) -> list[tuple[int, Article]]:
        """New articles in the Unseenet group; the first pass reads it all, for the manifest."""
        c = await self.node._conn()
        try:
            _, low, high = await c.group(self.group_id)
        except NNTPError as exc:
            log.warning("portal %s: node has no such group: %s", self.group_id, exc)
            return []
        after = self.state.get_cursor("unseenet", self.cursor_key)
        start = low if self.manifest is None or after is None else max(after + 1, low)
        out = []
        for n in range(start, high + 1):
            raw = await c.article(str(n))
            if raw is None:
                continue
            try:
                art = Article.from_bytes(strip_reader_headers(raw))
                art.verify_signature()
            except (ArticleError, ValueError) as exc:
                log.debug("portal %s: #%d unreadable: %s", self.group_id, n, exc)
                continue
            out.append((n, art))
        return out

    def _see_manifest(self, n: int, art: Article) -> None:
        try:
            m = Manifest.from_article(art)
        except ArticleError as exc:
            log.info("portal %s: bad manifest #%d: %s", self.group_id, n, exc)
            return
        if not m.supersedes(self.manifest):
            return
        was = self.active
        self.manifest = m
        now = self.active
        if now and not was and self.state.get_cursor("unseenet", self.opened_key) is None:
            self.state.set_cursor("unseenet", self.opened_key, n)
            log.info("portal %s opened by manifest #%d (serial %d)", self.group_id, n, m.serial)
        elif was and not now:
            self.state.db.execute("DELETE FROM cursor WHERE network=? AND source=?",
                                  ("unseenet", self.opened_key))
            self.state.db.commit()
            self.pending.clear()
            self._forget_due()
            log.info("portal %s closed by manifest #%d", self.group_id, n)

    @property
    def active(self) -> bool:
        m = self.manifest
        if m is None or m.group_id != self.group_id:
            return False
        if portal_entry(m.raw, self.network, self.source, self.gateway.key_text) is None:
            return False
        why = may_have_portal(m, self.gateway.public_key)
        if why and self._warned != m.message_id:
            self._warned = m.message_id
            log.warning("portal %s listed but refused: %s", self.group_id, why)
        return why is None

    # --- one pass ---
    async def run(self) -> PortalStats:
        st = PortalStats()
        arts = await self._read_group()
        high = self.state.get_cursor("unseenet", self.cursor_key) or 0
        for n, art in arts:
            high = max(high, n)
            if art.control == ["manifest"]:
                self._see_manifest(n, art)
        if not self.active:
            self.state.set_cursor("unseenet", self.cursor_key, high)
            return st
        st.active = True
        await self.counterpart.ensure(self.manifest)
        opened = self.state.get_cursor("unseenet", self.opened_key) or 0
        now = time.time()
        for n, art in arts:
            if n <= opened or art.message_id in self.pending:
                continue
            if self._sent(art.message_id):
                continue
            if exportable(art, self.gateway.public_key) is None:
                self.pending[art.message_id] = _Pending(n, self._due(art.message_id, now), art)
        for mid, p in sorted(self.pending.items(), key=lambda kv: kv[1].number):
            if p.due > now:
                continue
            try:
                fid = await self.counterpart.send(p.art)
            except ExportError as exc:
                st.refused += 1
                log.warning("portal %s: %s refused %s: %s", self.group_id, self.network, mid, exc)
                del self.pending[mid]
                self._forget_due(mid)
                continue
            self.state.remember(self.network, fid, mid, self.group_id, "out")
            del self.pending[mid]
            self._forget_due(mid)
            st.exported += 1
            log.info("portal exported %s -> %s", mid, fid[:16])
        st.waiting = len(self.pending)
        # never move the cursor past an article still waiting for its delay
        low_wait = min((p.number for p in self.pending.values()), default=None)
        self.state.set_cursor("unseenet", self.cursor_key,
                              high if low_wait is None else max(low_wait - 1, 0))
        rs = await self.importer.run()
        for f in ("imported", "duplicate", "skipped", "refused", "cancelled"):
            setattr(st, f, getattr(st, f) + getattr(rs, f))
        return st

    async def close(self) -> None:
        await self.counterpart.feed.close()
