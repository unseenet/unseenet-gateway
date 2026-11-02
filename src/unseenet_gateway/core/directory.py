"""The gateway directory (UN-RFC 0038 section 6): this gateway announces itself.

An announcement says who runs the gateway, what to invite on each network, and
which groups it serves. It is posted when its content changes and at least every
7 days; clients hide announcements older than 30 days, and a gatewayed group is
active only while one of its gateways announces it (section 7).

When the project's directory moves to a list council, its last manifest names the new
one in ``moved_to`` (section 6.1, UN-RFC 0020 section 10): the gateway announces there
from then on, asking again once a day. A directory set in the config is used as it is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from unseenet.core import crypto
from unseenet.core.article import new_article
from unseenet.nntp import NNTPError

from .adapter import Adapter
from .state import State
from .unseenet_side import Node

log = logging.getLogger(__name__)
DEFAULT_DIRECTORY = "unseenet.gateways@ph5fc5hop5232qsz"
RENEW = 7 * 86400
LOOK_AGAIN = 86400           # how often to ask whether the project's directory moved


def announcement(cfg: dict, ads: list[Adapter]) -> dict:
    gw = cfg["gateway"]
    networks, groups = [], []
    portals = {p.get("network") for p in cfg.get("portals", [])}
    for ad in ads:
        carried = ad.carried()
        groups += [c.group_id for c in carried]
        both = ad.section.get("direction", "import") == "both"
        networks.append({
            "network": ad.network, "account": ad.host(), "import": True, "export": both,
            "portals": ad.network in portals,
            "requests": bool(ad.desk is not None and gw.get("operator", "").startswith("ed25519:")),
        })
    body = {"v": 1, "name": str(gw.get("name") or gw.get("host") or "Unseenet gateway")[:64],
            "operator": gw["operator"], "contact": gw["contact"], "networks": networks,
            "groups": sorted(set(groups))}
    if gw.get("policy"):
        body["policy"] = str(gw["policy"])[:1024]
    return body


class Announcer:
    """A task in the gateway's loop: announce when needed, otherwise do nothing."""

    def __init__(self, *, cfg: dict, ads: list[Adapter], state: State, node: Node,
                 gateway: crypto.Identity, pow_bits: int):
        self.cfg, self.ads, self.state, self.node = cfg, ads, state, node
        self.gateway, self.pow_bits = gateway, pow_bits
        self.configured = cfg["gateway"].get("directory", DEFAULT_DIRECTORY)
        self.directory = self.configured
        self.label = "directory"
        self._looked = 0.0

    async def _where(self) -> str:
        """The directory to announce in: the configured one, or - for the project's -
        the one its manifest says it moved to."""
        ask = getattr(self.node, "manifest", None)
        if (ask is None or self.configured != DEFAULT_DIRECTORY
                or time.time() - self._looked < LOOK_AGAIN):
            return self.directory
        try:
            m = await ask(DEFAULT_DIRECTORY)
        except (NNTPError, OSError, ConnectionError) as exc:
            log.info("could not read the directory's manifest: %s", exc)
            return self.directory                # ask again next time
        self._looked = time.time()
        moved = (m.raw.get("moved_to") if m is not None else None)
        where = moved if isinstance(moved, str) and "@" in moved and len(moved) < 200 \
            else DEFAULT_DIRECTORY
        if where != self.directory:
            log.info("the gateway directory is %s", where)
        self.directory = where
        return where

    async def run(self) -> str:
        await self._where()
        body = json.dumps(announcement(self.cfg, self.ads), sort_keys=True, ensure_ascii=False)
        digest = int(hashlib.sha256(body.encode()).hexdigest()[:12], 16)
        last_at = self.state.get_cursor("unseenet", f"announced-at:{self.directory}") or 0
        last = self.state.get_cursor("unseenet", f"announced:{self.directory}")
        if last == digest and time.time() - last_at < RENEW:
            return "announcement current"
        d = json.loads(body)
        art = new_article(self.directory, f"Gateway: {d['name']}", body + "\n",
                          from_name=d["name"],
                          extra=[("Content-Type", "application/json; charset=utf-8")])
        art.finalize(self.gateway, self.pow_bits)
        try:
            await self.node.post(art)
        except NNTPError as exc:                 # e.g. the directory is not on this node yet
            return f"not announced in {self.directory}: {exc}"
        self.state.set_cursor("unseenet", f"announced:{self.directory}", digest)
        self.state.set_cursor("unseenet", f"announced-at:{self.directory}", int(time.time()))
        log.info("announced in %s: %d group(s)", self.directory, len(d["groups"]))
        return f"announced {art.message_id}"
