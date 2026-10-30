"""Reading NIP-29 Nostr groups on one relay (UN-RFC 0026 section 4)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from collections.abc import AsyncIterator

from unseenet.core.article import Article

from ..core import sharing
from ..core.adapter import Feed, Incoming, Retraction, Source
from ..core.state import State
from .convert import COMMENT, DELETION, IMPORTED_KINDS, NETWORK, convert
from .event import Event
from .relay import Relay

log = logging.getLogger(__name__)
METADATA, ADMINS = 39000, 39001
VANISHED_PASSES = 2
# Relays that collect profiles (kind 0). NIP-29 relays often refuse queries
# outside a group, so names are looked up here as well.
PROFILE_RELAYS = ["wss://purplepag.es", "wss://relay.damus.io", "wss://nos.lol",
                  "wss://relay.primal.net"]
PROFILE_RETRY = 6 * 3600          # look again for an author without a profile


def relay_pubkey(url: str, timeout: float = 20.0) -> str | None:
    """The relay's own key from its NIP-11 document (the `self` or `pubkey` field)."""
    http = url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    req = urllib.request.Request(http, headers={"Accept": "application/nostr+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        info = json.loads(resp.read().decode())
    return info.get("self") or info.get("pubkey")


def source_name(relay_url: str, group: str) -> str:
    """How a source is written in the manifest and the state: <relay>'<group-id>."""
    return f"{relay_url}'{group}"


PICTURE_LIMIT = 8 * 1024 * 1024      # never fetch more for one picture


class NostrFeed(Feed):
    network = NETWORK

    def __init__(self, *, state: State, relay: Relay, carried: dict[str, str],
                 start: str = "new", lookback: int = 7 * 86400,
                 profile_relays: list[str] | None = None, proxy: str | None = None,
                 label: str | None = None, socks_port: int | None = None, fetch=None):
        self.state, self.relay, self.carried = state, relay, carried
        self.start, self.lookback = start, lookback
        self.label = label or f"nostr {relay.url}"
        self.titles: dict[str, str] = {}
        self.names: dict[str, str | None] = {}
        self.looked: dict[str, float] = {}      # pubkey -> when we last looked
        self.relay_key: str | None = None
        self.profile_relays = PROFILE_RELAYS if profile_relays is None else profile_relays
        self.proxy = proxy
        self._read: set[str] = set()            # groups read to the end in this pass
        # pictures are fetched only through the gateway's Tor (gateway#26)
        self.socks_port = socks_port
        self._fetch = fetch

    def sources(self) -> list[Source]:
        return [Source(g, gid) for g, gid in self.carried.items()]

    # --- checks ---
    async def group_is_public(self, group: str) -> bool:
        """The group's metadata must be signed by the relay itself and not private."""
        if self.relay_key is None:
            self.relay_key = relay_pubkey(self.relay.url)
        meta = [e for e in await self.relay.query({"kinds": [METADATA], "#d": [group]})
                if e.pubkey == self.relay_key]
        if not meta:
            log.warning("%s: no metadata signed by the relay", group)
            return False
        if any(t and t[0] == "private" for t in meta[-1].tags):
            log.warning("%s: private group, not gatewayed", group)
            return False
        return True

    # --- lookups ---
    async def _fill_titles(self, events: list[Event]) -> None:
        roots = {e.tag("E") for e in events if e.kind == COMMENT and e.tag("E")}
        for e in events:
            if e.kind == 11:
                self.titles[e.id] = e.tag("title") or e.content.splitlines()[0][:80]
        missing = [r for r in roots if r not in self.titles]
        if missing:
            for e in await self.relay.query({"ids": missing}):
                self.titles[e.id] = e.tag("title") or (e.content.splitlines() or [""])[0][:80]

    async def _fill_names(self, events: list[Event]) -> None:
        now = time.time()
        missing = sorted(pk for pk in {e.pubkey for e in events}
                         if self.names.get(pk) is None
                         and now - self.looked.get(pk, 0) > PROFILE_RETRY)
        if not missing:
            return
        for pk in missing:
            self.looked[pk] = now
        newest: dict[str, int] = {}
        sources = [self.relay] + [Relay(u, proxy=self.proxy) for u in self.profile_relays
                                  if u.rstrip("/") != self.relay.url.rstrip("/")]
        for relay in sources:
            todo = [pk for pk in missing if pk not in newest]
            if not todo:
                break
            try:
                found = await asyncio.wait_for(
                    relay.query({"kinds": [0], "authors": todo}), 20)
            except Exception as exc:  # noqa: BLE001  (relay may refuse or be down)
                log.debug("no profiles from %s: %s", relay.url, exc)
                found = []
            finally:
                if relay is not self.relay:
                    try:
                        await relay.close()
                    except Exception:  # noqa: BLE001
                        pass
            for e in found:                       # signature already checked by Relay
                if e.pubkey not in missing or e.created_at <= newest.get(e.pubkey, -1):
                    continue
                try:
                    meta = json.loads(e.content)
                    name = meta.get("display_name") or meta.get("name") or None
                except (ValueError, AttributeError):
                    continue
                if isinstance(name, str) and name.strip():
                    newest[e.pubkey] = e.created_at
                    self.names[e.pubkey] = " ".join(name.split())[:64]

    # --- import ---
    async def fetch(self, source: Source) -> AsyncIterator[Incoming]:
        group = source.foreign
        self._read.discard(group)
        src = source_name(self.relay.url, group)
        if not await self.group_is_public(group):
            return
        since = self.state.get_cursor(NETWORK, src)
        flt = {"#h": [group], "kinds": list(IMPORTED_KINDS)}
        if since is not None:
            flt["since"] = since
        elif self.start == "new":
            self.state.set_cursor(NETWORK, src, int(time.time()))
            return
        events = await self.relay.query(flt)
        await self._fill_titles(events)
        await self._fill_names(events)
        newest = since or 0
        for ev in events:
            newest = max(newest, ev.created_at)
            yield Incoming(ev.id, ev, label=ev.id[:16])
        # Same second may hold more events: step back one second, duplicates are skipped.
        self.state.set_cursor(NETWORK, src, max(newest - 1, 0) if newest else since or 0)
        self._read.add(group)

    def convert(self, source: Source, item: Incoming) -> Article:
        return convert(item.payload, group_id=source.group_id,
                       id_map=lambda eid: self.state.lookup(NETWORK, eid),
                       title_of=self.titles.get, name_of=self.names.get, takes=source.takes,
                       group_of=self.state.group_for,
                       shares=source.shares or sharing.NONE)

    async def media(self, source: Source, item: Incoming) -> list:
        """The pictures a note in the group links to, fetched over Tor (gateway#26):
        NIP-29 groups are moderated by their relay's admins, as Matrix rooms are."""
        from ..core.files import Media, fetch_over_tor
        from .convert import pictures_of
        fetch = self._fetch or (lambda url, limit: fetch_over_tor(url, self.socks_port, limit))
        out = []
        for url, alt in pictures_of(item.payload):
            try:
                octets = await fetch(url, PICTURE_LIMIT)
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                log.info("picture %s not fetched: %s", url[:80], exc)
                continue
            name = url.split("?", 1)[0].rsplit("/", 1)[-1][:128] or "picture"
            out.append(Media(name, octets, alt))
        return out

    def imported(self, item: Incoming, art: Article) -> None:
        ev: Event = item.payload
        self.state.remember_author(NETWORK, ev.id, ev.pubkey)
        if ev.kind == 11:
            self.titles[ev.id] = art["Subject"]

    async def retractions(self, source: Source) -> list[Retraction]:
        """Messages deleted in the group, to be cancelled in Unseenet.

        Two signals count. An author-signed deletion request (NIP-09) for an
        imported event, when the relay still serves it. And, since a NIP-29
        relay is the group's authority and often drops both the event and the
        request: an imported event the relay no longer serves, for
        VANISHED_PASSES passes in a row, while it still serves others from
        the same group. A relay that is down or empty serves none, so that
        never cancels anything.
        """
        if source.foreign not in self._read:     # not public, or a first "new" pass
            return []
        rows = self.state.db.execute(
            "SELECT foreign_id, unseenet_id FROM ids WHERE network=? AND group_id=? "
            "AND direction='in' AND at > strftime('%s','now') - ?",
            (NETWORK, source.group_id, self.lookback)).fetchall()
        imported = {fid: mid for fid, mid in rows if not fid.startswith("cancel:")}
        if not imported:
            return []
        served: dict[str, Event] = {}
        ids = list(imported)
        for i in range(0, len(ids), 200):
            for e in await self.relay.query({"ids": ids[i:i + 200]}):
                served[e.id] = e
        to_cancel: dict[str, str] = {}
        for d in await self.relay.query({"kinds": [DELETION], "#e": ids}):
            for eid in d.tag_values("e"):
                if eid in imported and (served.get(eid) or None) and served[eid].pubkey == d.pubkey:
                    to_cancel[eid] = "Deleted by its author on Nostr."
                elif eid in imported and self.state.author_of(NETWORK, eid) == d.pubkey:
                    to_cancel[eid] = "Deleted by its author on Nostr."
        if served:                       # the relay answers for this group at all
            for eid in ids:
                if eid in served:
                    self.state.seen_present(NETWORK, eid)
                elif self.state.seen_missing(NETWORK, eid) >= VANISHED_PASSES:
                    to_cancel.setdefault(eid, "Deleted in the Nostr group.")
            self.state.db.commit()
        return [Retraction(eid, imported[eid], reason) for eid, reason in to_cancel.items()]

    async def close(self) -> None:
        await self.relay.close()
