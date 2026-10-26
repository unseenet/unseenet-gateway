"""#45: imported reactions take the gatewayed group's reaction set (UN-RFC 0048 section 9)."""
import asyncio
from types import SimpleNamespace

from unseenet.core import crypto
from unseenet_gateway.core.adapter import Source
from unseenet_gateway.core.importer import Importer
from unseenet_gateway.core.state import State


class Node:
    def __init__(self):
        self.asked = 0

    async def manifest(self, gid):
        self.asked += 1
        return SimpleNamespace(raw={"type": "moderated", "view": "chat", "reactions": ["👍", "❤️"]})

    async def post(self, art, **kw):
        return "ok"


class Feed:
    network = "matrix"

    def __init__(self):
        self.seen = []

    async def fetch(self, source):
        self.seen.append(source)
        return
        yield

    async def retractions(self, source):
        return []


def test_the_set_comes_from_the_manifest_once(tmp_path):
    node, feed = Node(), Feed()
    imp = Importer(feed=feed, state=State(tmp_path / "gw.db"), node=node,
                   gateway=crypto.Identity.generate(), pow_bits=0)
    src = Source("!room:x", "matrix.x.room@abcdabcdabcdabcd")
    asyncio.run(imp.run_source(src))
    asyncio.run(imp.run_source(src))
    takes = feed.seen[0].takes
    assert takes("👍") and not takes("🦔") and node.asked == 1          # kept an hour
    assert feed.seen[0] == src                                          # the same source otherwise
