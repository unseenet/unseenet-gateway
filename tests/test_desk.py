"""The operator's desk: requests to the operator's inbox, answers from it."""

import asyncio

from unseenet.core import crypto
from unseenet.core.article import Article
from unseenet.core.groups import inbox_article, open_inbox_article, personal_inbox_id

from unseenet_gateway.core.desk import Desk, parse_answer
from unseenet_gateway.core.state import State

GW, OP, OTHER = (crypto.Identity.generate() for _ in range(3))


class Inbox:
    """A node with inboxes: post stores by group, group/article read them back."""

    def __init__(self):
        self.groups: dict[str, list[bytes]] = {}
        self._group = None

    async def post(self, art):
        self.groups.setdefault(art.group, []).append(art.to_bytes())
        return "ok"

    async def _conn(self):
        return self

    async def group(self, gid):
        self._group = gid
        n = len(self.groups.get(gid, []))
        return n, 1, n

    async def article(self, n):
        return self.groups[self._group][int(n) - 1]


def desk(tmp_path, node):
    return Desk(state=State(tmp_path / "s.db"), node=node, gateway=GW, operator=OP.key_text,
                contact=personal_inbox_id(OP.public_key), pow_bits=0)


def answer(node, request_mid, text, who=OP):
    art = inbox_article(personal_inbox_id(GW.public_key), GW.public_key, from_name="x",
                        subject="Re: Gateway request", body=text, sender=who,
                        references=[request_mid], pow_bits=0)
    asyncio.run(node.post(art))


def test_parse_answer():
    assert parse_answer("> quoted\n\napprove\n") == ("approve", "")
    assert parse_answer("Approve matrix.x.y") == ("approve", "matrix.x.y")
    assert parse_answer("decline Not on topic for us") == ("decline", "Not on topic for us")
    assert parse_answer("ja") == ("approve", "")
    assert parse_answer("maybe later") is None


def test_request_reaches_the_operator_once(tmp_path):
    node = Inbox()
    d = desk(tmp_path, node)
    assert asyncio.run(d.ask("matrix", "#r:x", title="#r:x", detail="details",
                             name="matrix.x.r"))
    assert not asyncio.run(d.ask("matrix", "#r:x", title="#r:x", detail="details", name=None))
    raw = node.groups[personal_inbox_id(OP.public_key)]
    assert len(raw) == 1
    inner, sender = open_inbox_article(Article.from_bytes(raw[0]), OP)
    assert sender == GW.public_key and "matrix.x.r" in inner.body
    assert inner["Reply-Inbox"] == personal_inbox_id(GW.public_key)


def test_only_the_operator_decides(tmp_path):
    node = Inbox()
    d = desk(tmp_path, node)
    asyncio.run(d.ask("matrix", "#r:x", title="t", detail="d", name="matrix.x.r"))
    mid = d.get("matrix", "#r:x").request
    answer(node, mid, "approve", who=OTHER)                  # someone else: ignored
    answer(node, "<nope@unseenet>", "approve")               # not about the request: ignored
    assert asyncio.run(d.poll()) == [] and d.get("matrix", "#r:x").status == "pending"
    answer(node, mid, "> the request\n\napprove matrix.x.better")
    [req] = asyncio.run(d.poll())
    assert (req.status, req.name) == ("approved", "matrix.x.better")
    assert [r.source for r in d.approved("matrix")] == ["#r:x"]


def test_decline_keeps_the_reason(tmp_path):
    node = Inbox()
    d = desk(tmp_path, node)
    asyncio.run(d.ask("matrix", "#r:x", title="t", detail="d", name=None))
    answer(node, d.get("matrix", "#r:x").request, "decline Not for this network")
    [req] = asyncio.run(d.poll())
    assert (req.status, req.reason) == ("declined", "Not for this network")


def test_approval_without_a_usable_name_waits(tmp_path):
    node = Inbox()
    d = desk(tmp_path, node)
    asyncio.run(d.ask("matrix", "!id:x", title="t", detail="d", name=None))
    answer(node, d.get("matrix", "!id:x").request, "approve")
    assert asyncio.run(d.poll()) == [] and d.get("matrix", "!id:x").status == "pending"


# --- Matrix: which invitations become requests ---------------------------------

from unseenet_gateway.matrix.adapter import MatrixAdapter  # noqa: E402
from unseenet_gateway.matrix.feed import MatrixFeed  # noqa: E402


def room_state(alias=None, join="public", space=False, encrypted=False):
    st = [{"type": "m.room.join_rules", "content": {"join_rule": join}},
          {"type": "m.room.create", "content": {"type": "m.space"} if space else {}},
          {"type": "m.room.name", "content": {"name": "A room"}},
          {"type": "m.room.member", "state_key": "@gw:x", "sender": "@mod:x",
           "content": {"membership": "invite"}}]
    if alias:
        st.append({"type": "m.room.canonical_alias", "content": {"alias": alias}})
    if encrypted:
        st.append({"type": "m.room.encryption", "content": {}})
    return st


class InviteMatrix:
    base, user_id = "https://x", "@gw:x"

    def __init__(self, invites):
        self._invites, self.left = invites, {}

    async def invites(self):
        return self._invites

    async def whoami(self):
        return self.user_id

    async def resolve(self, room):
        return "!carried:x"

    async def leave(self, rid, reason=""):
        self.left[rid] = reason


def test_matrix_invitations_become_requests(tmp_path):
    node = Inbox()
    d = desk(tmp_path, node)
    cfg = {"gateway": {"group_owner": GW.key_text, "gateway_key": str(tmp_path / "k")},
           "matrix": {"homeserver": "https://x", "user": "@gw:x", "rooms": ["#carried:x"]}}
    ad = MatrixAdapter(cfg, d)
    mx = InviteMatrix({"!carried:x": room_state("#carried:x"),
                       "!new:x": room_state("#new:x"),
                       "!space:x": room_state("#space:x", space=True),
                       "!secret:x": room_state("#secret:x", encrypted=True)})
    feed = MatrixFeed(state=d.state, matrix=mx, carried={"#carried:x": "g@x"})
    asyncio.run(ad.handle_requests(d, [feed]))
    assert d.get("matrix", "#new:x").name == "matrix.x.new"
    assert d.get("matrix", "#carried:x") is None              # the feed joins it itself
    assert set(mx.left) == {"!space:x", "!secret:x"}
    answer(node, d.get("matrix", "#new:x").request, "decline Off topic")
    asyncio.run(d.poll())
    mx.left.clear()
    asyncio.run(ad.handle_requests(d, [feed]))
    assert mx.left == {"!new:x": "Off topic"} or mx.left.get("!new:x") == "Off topic"


def test_approved_rooms_are_carried(tmp_path):
    node = Inbox()
    d = desk(tmp_path, node)
    asyncio.run(d.ask("matrix", "#new:x", title="t", detail="d", name="matrix.x.new"))
    answer(node, d.get("matrix", "#new:x").request, "approve")
    asyncio.run(d.poll())
    cfg = {"gateway": {"group_owner": GW.key_text, "gateway_key": str(tmp_path / "k")},
           "matrix": {"homeserver": "https://x", "user": "@gw:x", "rooms": []}}
    names = [c.name for c in MatrixAdapter(cfg, d).carried()]
    assert names == ["matrix.x.new"]


class Dear(Inbox):
    """An inbox that wants more postage than the desk pays at first (gateway#16)."""

    def __init__(self, need):
        super().__init__()
        self.need, self.tries = need, []

    async def post(self, art):
        from unseenet.nntp import NNTPError
        self.tries.append(art.proven_pow())
        if art.proven_pow() < self.need:
            raise NNTPError(441, f"postage: need {self.need} bits")
        return await super().post(art)


def test_a_join_request_pays_what_the_inbox_asks(tmp_path):
    import asyncio
    node = Dear(10)
    d = desk(tmp_path, node)
    to = crypto.Identity.generate()
    assert asyncio.run(d.request_join(group="test.usenet.alt.test@abcdefghijklmnop",
                                      to_inbox=personal_inbox_id(to.public_key),
                                      to_key=to.public_key, network="usenet",
                                      source="alt.test", account=""))
    assert len(node.tries) == 2 and node.tries[1] >= 10        # built anew with 10 bits
    assert sum(len(v) for v in node.groups.values()) == 1


def test_it_gives_up_when_asked_for_no_more(tmp_path):
    import asyncio
    import pytest
    from unseenet.nntp import NNTPError
    node = Dear(10)
    d = desk(tmp_path, node)
    d.pow_bits = 12                                             # already more: not "postage"
    async def refuse(art):
        raise NNTPError(441, "permission: no")
    node.post = refuse
    to = crypto.Identity.generate()
    with pytest.raises(NNTPError):
        asyncio.run(d.request_join(group="g@abcdefghijklmnop", to_inbox=personal_inbox_id(to.public_key),
                                   to_key=to.public_key, network="usenet", source="x", account=""))
