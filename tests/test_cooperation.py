"""Several gateways per group (UN-RFC 0038): admission, turns, the directory."""

import asyncio
import json

from unseenet.core import crypto
from unseenet.core.article import Article
from unseenet.core.groups import make_group_id, personal_inbox_id

from unseenet_gateway.core.cooperation import Watcher
from unseenet_gateway.core.desk import JOIN, Desk
from unseenet_gateway.core.directory import Announcer, announcement
from unseenet_gateway.core.state import State
from unseenet_gateway.core.unseenet_side import gateway_manifest

from test_desk import Inbox

OWNER, GW1, GW2, OP1, OP2 = (crypto.Identity.generate() for _ in range(5))
NAME = "matrix.x.room"
GID = make_group_id(NAME, OWNER.public_key)


def manifest(peers=(), serial=1):
    return gateway_manifest(OWNER, GW1, NAME, network="matrix", source="#room:x",
                            direction="both", host="@gw1:x",
                            submissions=personal_inbox_id(GW1.public_key),
                            operator=OP1.key_text, contact=personal_inbox_id(OP1.public_key),
                            peers=list(peers), requests=personal_inbox_id(GW1.public_key),
                            serial=serial, pow_bits=0)


def watcher(tmp_path, node, me, joined=()):
    return Watcher(state=State(tmp_path / f"{me.key_text[-6:]}.db"), node=node, gateway=me,
                   groups=[GID], joined=list(joined))


def test_manifest_with_peers_lists_every_gateway_and_no_submissions():
    m = manifest(peers=[GW2.key_text])
    raw = json.loads(m.body)
    assert set(raw["moderators"]) == {GW1.key_text, GW2.key_text}
    assert "submissions" not in raw
    assert raw["gateway"]["requests"] == personal_inbox_id(GW1.public_key)


def test_a_joining_gateway_waits_until_it_is_admitted(tmp_path):
    node = Inbox()
    w = watcher(tmp_path, node, GW2, joined=[GID])
    assert w.delay(GID) is None                      # manifest not seen: not yet
    asyncio.run(node.post(manifest()))
    asyncio.run(w.run())
    assert not w.admitted(GID) and w.delay(GID) is None
    asyncio.run(node.post(manifest(peers=[GW2.key_text], serial=2)))
    asyncio.run(w.run())
    assert w.admitted(GID)
    ranks = sorted([GW1.public_key, GW2.public_key], key=crypto.fingerprint)
    assert w.delay(GID) == ranks.index(GW2.public_key) * 300


def test_both_gateways_agree_on_the_order(tmp_path):
    node = Inbox()
    asyncio.run(node.post(manifest(peers=[GW2.key_text])))
    w1, w2 = watcher(tmp_path, node, GW1), watcher(tmp_path, node, GW2)
    asyncio.run(w1.run()), asyncio.run(w2.run())
    assert {w1.rank(GID), w2.rank(GID)} == {0, 1}
    assert w1.gateways(GID) == w2.gateways(GID)


def test_join_request_reaches_the_owning_gateway(tmp_path):
    node = Inbox()
    d2 = Desk(state=State(tmp_path / "2.db"), node=node, gateway=GW2, operator=OP2.key_text,
              contact=personal_inbox_id(OP2.public_key), pow_bits=0)
    assert asyncio.run(d2.request_join(group=GID, to_inbox=personal_inbox_id(GW1.public_key),
                                       to_key=GW1.public_key, network="matrix",
                                       source="#room:x", account="@gw2:y"))
    assert not asyncio.run(d2.request_join(group=GID, to_inbox=personal_inbox_id(GW1.public_key),
                                           to_key=GW1.public_key, network="matrix",
                                           source="#room:x", account="@gw2:y"))
    d1 = Desk(state=State(tmp_path / "1.db"), node=node, gateway=GW1, operator=OP1.key_text,
              contact=personal_inbox_id(OP1.public_key), pow_bits=0)
    asyncio.run(d1.poll())
    [jr] = d1.joins
    assert jr.sender == GW2.key_text and jr.group == GID and jr.account == "@gw2:y"
    assert jr.operator == OP2.key_text
    # the owner's operator is asked, and an approval needs no name
    assert asyncio.run(d1.ask(JOIN, f"{GID}|{jr.sender}", title="t", detail="d",
                              name=jr.account))
    assert d1.get(JOIN, f"{GID}|{jr.sender}").status == "pending"


class FakeAd:
    network, desk = "matrix", object()

    def __init__(self, groups, direction="both"):
        self.section = {"direction": direction}
        self._groups = groups

    def host(self):
        return "@gw:x"

    def carried(self):
        class C:
            def __init__(self, g):
                self.group_id = g
        return [C(g) for g in self._groups]


CFG = {"gateway": {"name": "test gw", "operator": OP1.key_text,
                   "contact": personal_inbox_id(OP1.public_key)}}


def test_announcement_lists_networks_and_groups():
    a = announcement(CFG, [FakeAd([GID, GID])])
    assert a["v"] == 1 and a["name"] == "test gw" and a["groups"] == [GID]
    [n] = a["networks"]
    assert n == {"network": "matrix", "account": "@gw:x", "import": True, "export": True,
                 "portals": False, "requests": True}


def test_announcer_posts_only_when_something_changed(tmp_path):
    node, state = Inbox(), State(tmp_path / "s.db")
    ad = FakeAd([GID])
    an = Announcer(cfg=CFG, ads=[ad], state=state, node=node, gateway=GW1, pow_bits=0)
    asyncio.run(an.run())
    asyncio.run(an.run())
    [raw] = node.groups[an.directory]
    art = Article.from_bytes(raw)
    art.verify_signature()
    assert art.author_key == GW1.public_key and json.loads(art.body)["groups"] == [GID]
    ad._groups.append(make_group_id("matrix.x.other", OWNER.public_key))
    asyncio.run(an.run())
    assert len(node.groups[an.directory]) == 2


def test_a_new_peer_always_raises_the_serial(tmp_path):
    from unseenet_gateway.cli import manifest_serial

    class Ad:
        section = {"manifest_serial": 1}
    st = State(tmp_path / "s.db")
    assert manifest_serial(st, Ad, GID, bump=True) == 2        # serial 1 is out already
    assert manifest_serial(st, Ad, GID) == 2
    assert manifest_serial(st, Ad, GID, bump=True) == 3


def test_announcer_follows_the_directory_to_the_council(tmp_path):
    """UN-RFC 0038 section 6.1: the project's directory says it moved (``moved_to``);
    the gateway announces in the new one. A directory set in the config stays."""
    from types import SimpleNamespace

    from unseenet_gateway.core.directory import DEFAULT_DIRECTORY
    new = "unseenet.gateways@abcdefghijklmnop"
    node, state = Inbox(), State(tmp_path / "s.db")
    manifests = {DEFAULT_DIRECTORY: SimpleNamespace(raw={"moved_to": new})}

    async def manifest(gid):
        return manifests.get(gid)
    node.manifest = manifest
    an = Announcer(cfg=CFG, ads=[FakeAd([GID])], state=state, node=node, gateway=GW1, pow_bits=0)
    asyncio.run(an.run())
    assert an.directory == new and len(node.groups[new]) == 1
    assert DEFAULT_DIRECTORY not in node.groups
    own = {**CFG, "gateway": {**CFG["gateway"], "directory": "my.directory@abcdefghijklmnop"}}
    an2 = Announcer(cfg=own, ads=[FakeAd([GID])], state=state, node=node, gateway=GW1, pow_bits=0)
    asyncio.run(an2.run())
    assert an2.directory == "my.directory@abcdefghijklmnop"
