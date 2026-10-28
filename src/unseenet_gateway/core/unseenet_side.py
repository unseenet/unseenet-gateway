"""Everything the gateway does towards Unseenet, shared by all networks.

The gateway talks to Unseenet like any client: over Tor, to one node, with no
special status (UN-RFC 0023, section 6.3).
"""

from __future__ import annotations

import logging
from pathlib import Path

from unseenet.core import crypto
from unseenet.core.article import Article, ArticleError
from unseenet.core.groups import Manifest, make_group_id, manifest_article
from unseenet.nntp import Client, NNTPError

log = logging.getLogger(__name__)


def gateway_manifest(owner: crypto.Identity, gateway: crypto.Identity, name: str, *,
                     network: str, source: str, direction: str, host: str,
                     operator: str, contact: str, abuse: str = "", policy: str = "",
                     title: str = "", description: str = "", serial: int = 1,
                     retention_days: int = 90, submissions: str | None = None,
                     view: str = "", kinds: tuple[str, ...] = (), min_pow: int = 0,
                     peers: list[str] | None = None, requests: str | None = None,
                     attachments: dict | None = None, pow_bits: int = 18) -> Article:
    """Manifest of a gatewayed group (UN-RFC 0023 section 3.2): moderated, gateway key as moderator."""
    if direction not in ("import", "both"):
        raise ValueError("direction must be 'import' or 'both'")
    gw = {"network": network, "source": source, "direction": direction,
          "host": host, "operator": operator, "contact": contact}
    if abuse:
        gw["abuse"] = abuse
    if requests:
        gw["requests"] = requests            # UN-RFC 0038 3.2: where join requests go
    if policy:
        gw["policy"] = policy[:2048]
    data = {"v": 1, "name": name, "type": "moderated", "owner": owner.key_text,
            "serial": serial, "min_pow": min_pow, "retention_days": retention_days,
            "moderators": [gateway.key_text] + [k for k in (peers or [])
                                                 if k != gateway.key_text],
            "gateway": gw}
    if submissions and not peers:
        # with several gateways, clients send submissions to each one's inbox (0038 3.1)
        data["submissions"] = submissions
    if title:
        data["title"] = title[:128]
    if description:
        data["description"] = description[:2048]
    if view:
        data["view"] = view                  # UN-RFC 0008 section 3.1
    if kinds:
        data["kinds"] = list(kinds)          # UN-RFC 0028 section 4.1
    if attachments:
        data["attachments"] = attachments    # pictures it carries (UN-RFC 0027 5.1, #26)
    return manifest_article(owner, make_group_id(name, owner.public_key), data, pow_bits)


class Node:
    """One connection to an Unseenet node, reopened when it drops.

    Posting goes through the core's ``Session`` (unseenet#121, gateway#19): an article
    signed by one of the gateway's keys is posted by that key's session, which ripens
    the key first where the node asks for it, sets ``Key-Ripening``, sends the key's
    chain to a node that lacks it, and pays the postage the key's level needs
    (UN-RFC 0025). ``use_key`` gives the node a key and where its chain is kept."""

    def __init__(self, address: str, socks_port: int | None):
        self.address, self.socks_port = address, socks_port
        self.client: Client | None = None
        self._keys: dict[bytes, tuple[crypto.Identity, Path]] = {}
        self._sessions: dict[bytes, object] = {}

    def use_key(self, key: crypto.Identity, state: str | Path) -> None:
        """Post articles signed by ``key`` through a session for it; ``state`` keeps
        its public ripening chain (nothing secret), e.g. ``<key file>.state.json``."""
        self._keys[key.public_key] = (key, Path(state))

    async def session(self, key: crypto.Identity):
        """The core Session for one of our keys, over the gateway's Tor."""
        from unseenet.client.session import Session
        s = self._sessions.get(key.public_key)
        if s is None:
            _, state = self._keys[key.public_key]
            s = Session.from_key(key, state, name="gateway", display="gateway",
                                 status=lambda text: log.info("%s", text))
            if self.address not in s.nodes:
                s.add_node(self.address)
            if self.socks_port is not None:
                await s.set_tor(f"127.0.0.1:{self.socks_port}")
            self._sessions[key.public_key] = s
        return s

    async def _conn(self) -> Client:
        if self.client is None:
            self.client = await Client.connect(self.address, self.socks_port)
        return self.client

    async def post(self, art: Article, *, blobs=(), extra_bits: int = 0) -> str:
        """Post; returns 'ok', 'duplicate' or raises NNTPError for other refusals.
        ``blobs`` are the article's files (UN-RFC 0027), sent first, through a session."""
        mine = self._keys.get(art.author_key) if art.get("Author-Key") else None
        if mine is not None or self._keys:
            return await self._post_by_session(art, mine[0] if mine else None,
                                               blobs=blobs, extra_bits=extra_bits)
        if blobs:
            raise NNTPError(441, "files are posted through a session, and this node has none")
        for attempt in (1, 2):
            try:
                c = await self._conn()
                code, text = await c.post(art.to_bytes())
                break
            except (ConnectionError, OSError):
                self.client = None
                if attempt == 2:
                    raise
        if code == 240:
            return "ok"
        if code == 441 and text.startswith("duplicate"):
            return "duplicate"
        raise NNTPError(code, text)

    async def _post_by_session(self, art: Article, key: crypto.Identity | None, *,
                               blobs=(), extra_bits: int = 0) -> str:
        """Through the signing key's session; an article by another key (a submission,
        an owner's manifest from elsewhere) goes as it is, through the first one."""
        from unseenet.client.session import SessionError
        s = await self.session(key or next(iter(self._keys.values()))[0])
        try:
            if blobs:
                await s.post_article(art, node=self.address, sign_with=key, blobs=list(blobs),
                                     extra_bits=extra_bits)
            else:
                await s.post_article(art, node=self.address, sign_with=key)
        except SessionError as exc:
            text = str(exc).removeprefix("rejected by node: ")
            if text.startswith("duplicate"):
                return "duplicate"
            code = int(text[:3]) if text[:3].isdigit() else 441
            raise NNTPError(code, text) from exc
        return "ok"

    async def ripen(self, key: crypto.Identity, to: int) -> None:
        """Ripen ``key`` on to level ``to`` in the background, publishing ripe@ at each
        level (the core's ClientRuntime, low priority); 3 needs no surcharge."""
        from unseenet.client.runtime import ClientRuntime
        await ClientRuntime(await self.session(key), ripen_to=to).ripen()

    async def manifest(self, group_id: str) -> Manifest | None:
        """The newest valid manifest the node holds for the group, read with HDR Control."""
        c = await self._conn()
        try:
            _, low, high = await c.group(group_id)
        except NNTPError:
            return None
        best = None
        if high < low:
            return None
        for line in await c.multiline(f"HDR Control {low}-{high}", 225):
            number, _, value = line.partition(" ")
            if value.strip().lower() != "manifest":
                continue
            try:
                m = Manifest.from_article(Article.from_bytes(await c.article(number)))
            except (ArticleError, ValueError, TypeError, NNTPError):
                continue
            if m.group_id == group_id and m.supersedes(best):
                best = m
        return best

    async def close(self) -> None:
        """Drop the connection (it is opened again when needed); the sessions stay."""
        if self.client is not None:
            try:
                await self.client.quit()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    async def shutdown(self) -> None:
        """At the end: the connection and the sessions."""
        await self.close()
        for s in self._sessions.values():
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
