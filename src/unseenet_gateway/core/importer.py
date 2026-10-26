"""Import into Unseenet, the same for every network (UN-RFC 0023 section 4).

A feed reads the other network and converts its messages; this loop does the
rest: skips what was imported before, signs with the gateway key, pays the
postage, posts to the node, remembers the mapping for threads, and cancels
imported messages the other network has since removed (section 4.5).
"""

from __future__ import annotations

import logging
import dataclasses
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone

from unseenet.core import crypto
from unseenet.core.article import Article, format_date, new_article
from unseenet.nntp import NNTPError

from . import files as fl
from .adapter import Feed, Skip, Source
from .state import State
from .unseenet_side import Node

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    imported: int = 0
    duplicate: int = 0
    skipped: int = 0
    refused: int = 0
    cancelled: int = 0
    waiting: int = 0

    def add(self, other: "RunStats") -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name, 0))


def cancel_article(group_id: str, target_mid: str, reason: str, *,
                   now: datetime | None = None) -> Article:
    """An Unseenet cancel (UN-RFC 0004 section 7) for an imported article."""
    return new_article(group_id, None, reason + "\n",
                       extra=[("Control", f"cancel {target_mid}")], now=now)


class Importer:
    def __init__(self, *, feed: Feed, state: State, node: Node, gateway: crypto.Identity,
                 pow_bits: int, delay_for=None, pictures_max: int = 0):
        self.feed, self.state, self.node = feed, state, node
        # pictures on imported messages, up to this size (0: none) - gateway#26
        self.pictures_max = pictures_max if fl.core_posts_files() else 0
        self.gateway, self.pow_bits = gateway, pow_bits
        # group id -> seconds to wait before importing (rank x stagger), or None when this
        # gateway may not write there (UN-RFC 0038); None for the whole callable: no waiting
        self.delay_for = delay_for

    @property
    def label(self) -> str:
        return self.feed.label or self.feed.network

    async def run(self) -> RunStats:
        total = RunStats()
        for source in self.feed.sources():
            total.add(await self.run_source(source))
        return total

    async def _pictures(self, source: Source, item, art: Article) -> tuple[list, int]:
        """The message's pictures on the article, or a line saying they were left out."""
        if not art.get("Gateway-Media"):
            return [], 0
        art.remove("Gateway-Media")          # only a mark from convert, never posted
        media = []
        if self.pictures_max:
            try:
                media = await self.feed.media(source, item)
            except Exception as exc:  # noqa: BLE001 - the text still comes in
                log.info("%s: picture not fetched: %s", item.label, exc)
        blobs, bits, notes = fl.attach(art, media, self.pictures_max) if media else ([], 0, [])
        if not blobs:
            line = "(a picture was left out" + (": " + "; ".join(notes) if notes else
                                                 " - this gateway carries no pictures") + ")"
            art.body = (art.body.rstrip("\n") + "\n\n" + line + "\n").lstrip("\n")
        return blobs, bits

    async def _reaction_set(self, group_id: str):
        """The emoji the gatewayed group takes, from its manifest, kept an hour (#45)."""
        from unseenet.core import reactions as rx
        cache = self.__dict__.setdefault("_sets", {})
        hit = cache.get(group_id)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        takes = None
        try:
            m = await self.node.manifest(group_id)
        except (OSError, ConnectionError, ValueError):
            m = None
        if m is not None:
            takes = rx.reaction_set(dict(m.raw)).takes
        cache[group_id] = (time.time(), takes)
        return takes

    async def run_source(self, source: Source) -> RunStats:
        if source.takes is None and hasattr(self.node, "manifest"):
            source = dataclasses.replace(source, takes=await self._reaction_set(source.group_id))
        st, net = RunStats(), self.feed.network
        delay = self.delay_for(source.group_id) if self.delay_for else 0.0
        now, hold_from = time.time(), None
        async for item in self.feed.fetch(source):
            if item.foreign_id and self.state.lookup(net, item.foreign_id):
                st.duplicate += 1                    # before paying postage (or a peer's)
                continue
            if delay is None or (delay and item.when and now < item.when + delay):
                st.waiting += 1                      # not this gateway's turn (yet)
                hold_from = item.when if hold_from is None else min(hold_from, item.when or now)
                continue
            try:
                art = self.feed.convert(source, item)
            except Skip as s:
                st.skipped += 1
                self.state.count_skip(net, s.reason)
                log.info("%s %s skipped: %s", source.foreign, item.label, s)
                continue
            blobs, extra_bits = await self._pictures(source, item, art)
            # UN-RFC 0049 section 3.1: Date is the time of posting - fetching pictures may
            # have taken a while - and the other network's date is in Origin-Date
            art.set("Date", format_date(datetime.now(timezone.utc)))
            art.finalize(self.gateway, self.pow_bits)
            try:
                result = await (self.node.post(art, blobs=blobs, extra_bits=extra_bits)
                                if blobs else self.node.post(art))
            except NNTPError as exc:
                st.refused += 1
                self.state.count_skip(net, f"node-{exc.text.split(':')[0]}")
                log.warning("node refused %s: %s", item.foreign_id, exc)
                continue
            self.state.remember(net, item.foreign_id, art.message_id, art.group)
            self.feed.imported(item, art)
            if result == "duplicate":
                st.duplicate += 1
            else:
                st.imported += 1
                log.info("imported %s -> %s %s", item.foreign_id, art.message_id, art.group)
        if hold_from is not None:
            self.feed.hold(source, hold_from)
        for r in await self.feed.retractions(source):
            key = f"cancel:{r.foreign_id}"
            if self.state.lookup(net, key):
                continue
            art = cancel_article(source.group_id, r.unseenet_id, r.reason)
            art.finalize(self.gateway, self.pow_bits)
            await self.node.post(art)
            self.state.remember(net, key, art.message_id, source.group_id, "cancel")
            st.cancelled += 1
            log.info("cancelled %s (%s)", r.unseenet_id, r.reason)
        return st
