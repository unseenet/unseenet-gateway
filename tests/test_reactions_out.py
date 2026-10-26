"""#32: reactions going out (UN-RFC 0048 section 9) - that Unseenet readers reacted, not
how many: one reaction per emoji while any reader gives it, taken back with the last."""
import asyncio

from unseenet.core import crypto
from unseenet.core import reactions as rx
from unseenet.core.article import new_article
from unseenet_gateway.core.reactions_out import ReactionMirror
from unseenet_gateway.core.state import State

GW, ALICE, BOB = (crypto.Identity.generate() for _ in range(3))
GID = "matrix.lab.room@abcdabcdabcdabcd"
POST, OWN = "<from-matrix@x>", "<from-unseenet@x>"


class Client:
    def __init__(self, arts):
        self.arts = arts

    async def group(self, gid):
        return len(self.arts), 1, len(self.arts)

    async def article(self, n):
        return self.arts[int(n) - 1].to_bytes()


class Node:
    def __init__(self):
        self.arts = []

    async def _conn(self):
        return Client(self.arts)


class Sender:
    def __init__(self):
        self.out, self.n = {}, 0

    async def react(self, group, event_id, emoji):
        self.n += 1
        self.out[f"$r{self.n}"] = (event_id, emoji)
        return f"$r{self.n}"

    async def unreact(self, group, reaction_id):
        del self.out[reaction_id]

    async def send(self, art):
        return "$x"


def react(who, target, emoji, supersedes=None):
    return rx.build(GID, [], target, emoji, supersedes=supersedes).finalize(who, 0)


def cancel(who, mid):
    return new_article(GID, None, "", extra=[("Control", f"cancel {mid}")]).finalize(who, 0)


def setup(tmp_path):
    state = State(tmp_path / "gw.db")
    state.remember("matrix", "$m1", POST, GID, "in")          # came in from Matrix
    state.remember("matrix", "$m2", OWN, GID, "out")          # went out from Unseenet
    node, sender = Node(), Sender()
    mirror = ReactionMirror(state=state, node=node, gateway=GW, targets={GID: ("matrix", sender.send)})
    return mirror, node, sender


def test_one_reaction_per_emoji_while_anyone_gives_it(tmp_path):
    mirror, node, sender = setup(tmp_path)
    a = react(ALICE, POST, "👍")
    node.arts += [a, react(BOB, POST, "👍"),
                  react(GW, POST, "❤️"),                 # the gateway's own (an import): not counted
                  react(ALICE, OWN, "😂")]               # not from Matrix: nothing to show there
    st = asyncio.run(mirror.run())
    assert st["sent"] == 1 and list(sender.out.values()) == [("$m1", "👍")]   # one, not two
    node.arts.append(cancel(ALICE, a.message_id))           # Alice takes hers back: Bob's stays
    assert asyncio.run(mirror.run())["taken_back"] == 0 and len(sender.out) == 1
    b2 = react(BOB, POST, "🎉", supersedes=node.arts[1].message_id)   # Bob changes his
    node.arts.append(b2)
    st = asyncio.run(mirror.run())
    assert st == {"read": 1, "sent": 1, "taken_back": 1}
    assert list(sender.out.values()) == [("$m1", "🎉")]
    node.arts.append(cancel(BOB, b2.message_id))            # the last one: taken back
    asyncio.run(mirror.run())
    assert sender.out == {}


def test_a_cancel_from_someone_else_does_not_count(tmp_path):
    mirror, node, sender = setup(tmp_path)
    a = react(ALICE, POST, "👍")
    node.arts += [a, cancel(BOB, a.message_id)]
    asyncio.run(mirror.run())
    assert list(sender.out.values()) == [("$m1", "👍")]


def test_a_sender_without_react_is_left_alone(tmp_path):
    state = State(tmp_path / "gw.db")
    node = Node()
    node.arts.append(react(ALICE, POST, "👍"))

    async def send(art):
        return "$x"
    mirror = ReactionMirror(state=state, node=node, gateway=GW, targets={GID: ("usenet", send)})
    assert asyncio.run(mirror.run()) == {"read": 0, "sent": 0, "taken_back": 0}
