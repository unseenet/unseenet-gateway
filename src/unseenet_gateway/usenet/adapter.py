"""The Usenet adapter (UN-RFC 0024).

The gateway does not talk to Usenet itself: a local news server (INN) does
the peering, history, Path and spam filtering, and this adapter reads new
articles from it as a newsreader would.

Configuration, section [usenet]::

    server      = "127.0.0.1:119"   # the gateway's own INN
    prefix      = "usenet"
    direction   = "import"
    start       = "new"             # first run: "new", "all" or "last:N"
    max_per_run = 200
    groups      = ["comp.misc"]
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from unseenet.core import crypto
from unseenet.core.article import Article

from ..core.adapter import Adapter, Capabilities, Carried, Feed, Incoming, Skip, Source
from ..core.state import State
from .convert import NETWORK, clean, convert, parse, unseenet_name
from .source import NewsServer

log = logging.getLogger(__name__)


class UsenetFeed(Feed):
    network = NETWORK
    label = "usenet"

    def __init__(self, *, state: State, news: NewsServer, carried: dict[str, str],
                 start: str = "new", max_per_run: int = 200):
        self.state, self.news, self.carried = state, news, carried
        self.start, self.max_per_run = start, max_per_run

    def sources(self) -> list[Source]:
        return [Source(g, gid) for g, gid in self.carried.items()]

    async def _first_cursor(self, group: str) -> int:
        low, high = await self.news.range(group)
        if self.start == "all":
            return low - 1
        if self.start.startswith("last:"):
            return max(low - 1, high - int(self.start[5:]))
        return high                                     # "new"

    resumable = True        # the cursor moves per article: a pass may stop anywhere (#51)

    async def fetch(self, source: Source) -> AsyncIterator[Incoming]:
        group = source.foreign
        cursor = self.state.get_cursor(NETWORK, group)
        if cursor is None:
            cursor = await self._first_cursor(group)
            self.state.set_cursor(NETWORK, group, cursor)
        done = 0
        async for n, raw in self.news.articles(group, cursor):
            if done >= self.max_per_run:
                break
            done += 1
            mid = clean(parse(raw).get("Message-ID", "")) if raw is not None else None
            yield Incoming(mid or None, raw, label=f"#{n}")
            self.state.set_cursor(NETWORK, group, n)   # also after a skip or a refusal

    def convert(self, source: Source, item: Incoming) -> Article:
        if item.payload is None:
            raise Skip("size", "unreadable or too large")
        conv = convert(item.payload, carried=self.carried,
                       id_map=lambda m: self.state.lookup(NETWORK, m))
        return conv.article

    async def close(self) -> None:
        await self.news.close()


class UsenetAdapter(Adapter):
    network = NETWORK
    # Usenet has nothing like a member list: the same article reaches every gateway
    # from the servers, so admission rests on the group owner's approval alone
    consent_note = ("Usenet has no consent of its own: anyone can read a newsgroup, so "
                    "admission rests on your approval alone.")
    capabilities = Capabilities()          # import only; export is not implemented yet

    def carried(self) -> list[Carried]:
        host = self.host()
        out = []
        for entry in self.section["groups"]:
            source = entry["name"] if isinstance(entry, dict) else entry
            joined = entry.get("group") if isinstance(entry, dict) else None
            if joined:                  # another gateway's group (UN-RFC 0038 3.2)
                out.append(Carried(
                    source=source, name=joined.partition("@")[0], group_id=joined,
                    direction=self.section.get("direction", "import"),
                    title=f"Usenet: {source}", description="", view="threads", joined=True))
                continue
            name = unseenet_name(source, self.prefix)
            if name is None:
                log.warning("%s cannot be gatewayed (name)", source)
                continue
            out.append(Carried(
                source=source, name=name, group_id=self.group_id(name),
                direction=self.section.get("direction", "import"),
                title=f"Usenet: {source}",
                view="threads",       # Usenet is threads, and stays plain text (gateway#26)
                description=(f"Gatewayed from the Usenet group {source} by {host}. "
                             "Posts are signed by the gateway, not by their authors.")))
        return out

    def feeds(self, *, state, node, gateway: crypto.Identity, pow_bits: int) -> list[Feed]:
        return [UsenetFeed(
            state=state, news=NewsServer(self.section.get("server", "127.0.0.1:119")),
            carried={c.source: c.group_id for c in self.carried()},
            start=self.section.get("start", "new"),
            max_per_run=int(self.section.get("max_per_run", 200)))]
