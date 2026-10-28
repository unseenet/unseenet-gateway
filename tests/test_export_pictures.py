"""Pictures out of Unseenet (gateway#26): a submission's files go with it.

A post with pictures arrives as a submission whose encrypted files travel with the
inbox article (UN-RFC 0027 section 5.3). The exporter decrypts them, checks they are
the blobs the post names, gives them to the other network and posts the approved
copy with the plain blobs."""

import asyncio

from unseenet.core import blobs as bl
from unseenet.core import crypto
from unseenet.core.article import new_article
from unseenet.core.groups import Manifest, inbox_article, make_manifest
from unseenet_gateway.core.state import State
from unseenet_gateway.core.submissions import SUBMISSION_TYPE, Exporter, PlainFile
from unseenet_gateway.matrix.exporter import MatrixSender
from unseenet_gateway.nostr.exporter import pictures_line

OWNER, GW, ALICE = (crypto.Identity.generate() for _ in range(3))
PNG = b"\x89PNG\r\n\x1a\n" + b"picture" * 20
FILES = [("cat.png", "image/png", PNG, "a cat", (2, 2))]


class Conn:
    def __init__(self, inbox, raws, blobs):
        self.inbox, self.raws, self.blobs = inbox, raws, blobs

    async def group(self, gid):
        assert gid == self.inbox
        return len(self.raws), 1, len(self.raws)

    async def article(self, n):
        return self.raws[int(n) - 1]

    async def blob(self, bid):
        return self.blobs.get(bid)


class Node:
    def __init__(self, manifest):
        self.m, self.posted, self.conn = manifest, [], None

    async def manifest(self, gid):
        return self.m

    async def _conn(self):
        return self.conn

    async def post(self, art, blobs=()):
        self.posted.append((art, list(blobs)))
        return "ok"


def setup(tmp_path, send):
    art = make_manifest(OWNER, "matrix.lab.room", "moderated", moderators=[GW.key_text],
                        pow_bits=0, extra={"attachments": {"max_size": 65536,
                                                           "types": ["image/*"]}})
    m = Manifest.from_article(art)
    node = Node(m)
    ex = Exporter(state=State(tmp_path / "gw.db"), node=node, gateway=GW,
                  targets={m.group_id: ("matrix", send)}, min_pow=0, delay_max=0)
    return ex, node, m.group_id


def submit(ex, gid, files=FILES, sent_files=None, *, swap=False):
    """The inbox article a client sends: the post names the plain blobs, the inbox
    article carries them encrypted, their keys inside (as Session._submit does)."""
    post = new_article(gid, "Look", "a picture", from_name="Alice",
                       extra=bl.attachment_headers(files)).finalize(ALICE, 0)
    outer, inner, cipher = bl.encrypted_attachment_headers(sent_files or files)
    if swap:                                  # the node hands out another blob
        cipher = [(bid, cipher[0][1][:-1] + bytes([cipher[0][1][-1] ^ 1])) for bid, _ in cipher]
    raw = post.to_bytes().decode()
    sealed = inbox_article(ex.inbox, GW.public_key, from_name="", subject="Submission",
                           body=raw, content_type=SUBMISSION_TYPE, pow_bits=0,
                           extra=inner, outer_extra=outer)
    ex.node.conn = Conn(ex.inbox, [sealed.to_bytes()], dict(cipher))
    return post


def test_pictures_go_to_the_other_network_and_into_unseenet(tmp_path):
    got = []

    async def send(art, files=()):
        got.append(list(files))
        return "$event"

    ex, node, gid = setup(tmp_path, send)
    post = submit(ex, gid)
    st = asyncio.run(ex.run())
    assert st.exported == 1
    f = got[0][0]
    assert f.octets == PNG and f.type == "image/png" and f.name == "cat.png" and f.alt == "a cat"
    approved, blobs = node.posted[0]
    assert approved.message_id == post.message_id and approved["Approved-By"] == GW.key_text
    assert blobs == [(bl.blob_id(PNG), PNG)]


def test_a_sender_without_files_still_gets_the_text(tmp_path):
    got = []

    async def send(art):
        got.append(art.message_id)
        return "note1"

    ex, node, gid = setup(tmp_path, send)
    submit(ex, gid)
    assert asyncio.run(ex.run()).exported == 1 and len(got) == 1
    assert node.posted[0][1] == [(bl.blob_id(PNG), PNG)]


def test_files_other_than_the_post_names_are_refused(tmp_path):
    async def send(art, files=()):
        raise AssertionError("must not be sent")

    ex, node, gid = setup(tmp_path, send)
    other = [("cat.png", "image/png", PNG + b"!", "a cat", (2, 2))]
    submit(ex, gid, sent_files=other)
    st = asyncio.run(ex.run())
    assert st.refused == 1 and node.posted == []


def test_a_changed_blob_is_refused(tmp_path):
    async def send(art, files=()):
        raise AssertionError("must not be sent")

    ex, node, gid = setup(tmp_path, send)
    submit(ex, gid, swap=True)
    st = asyncio.run(ex.run())
    assert st.refused == 1 and node.posted == []


def test_a_missing_blob_is_refused(tmp_path):
    async def send(art, files=()):
        raise AssertionError("must not be sent")

    ex, node, gid = setup(tmp_path, send)
    submit(ex, gid)
    ex.node.conn.blobs.clear()
    assert asyncio.run(ex.run()).refused == 1


# --- Matrix: the pictures follow the text ------------------------------------------

class FakeMatrix:
    def __init__(self):
        self.events, self.uploads, self.redacted = [], [], []

    async def membership(self, rid):
        return "join"

    async def messages(self, rid, **kw):
        return {"chunk": []}

    async def upload(self, data, content_type, filename=""):
        self.uploads.append((data, content_type, filename))
        return f"mxc://x/{len(self.uploads)}"

    async def send(self, rid, content, txn, event_type="m.room.message"):
        self.events.append((txn, content))
        return f"$e{len(self.events)}"

    async def redact(self, rid, eid, txn, reason=""):
        self.redacted.append(eid)


class FakeState:
    def foreign_for(self, mid):
        return None


def sender(mx, gid):
    async def rid(_):
        return "!r:x"
    return MatrixSender(matrix=mx, state=FakeState(), rooms={gid: "#r:x"}, room_id_of=rid)


def test_matrix_sends_each_picture_as_an_image_after_the_text():
    gid = make_manifest(OWNER, "matrix.lab.room", "moderated", pow_bits=0)
    gid = Manifest.from_article(gid).group_id
    mx = FakeMatrix()
    post = new_article(gid, "Look", "a picture", from_name="Alice").finalize(ALICE, 0)
    f = PlainFile(bl.blob_id(PNG), PNG, "image/png", "cat.png", "a cat", (2, 2))
    eid = asyncio.run(sender(mx, gid).send(post, [f]))
    assert eid == "$e1"
    assert mx.uploads == [(PNG, "image/png", "cat.png")]
    text, pic = mx.events[0][1], mx.events[1][1]
    assert text["msgtype"] == "m.text"
    assert pic["msgtype"] == "m.image" and pic["url"] == "mxc://x/1" and pic["body"] == "a cat"
    assert pic["info"] == {"mimetype": "image/png", "size": len(PNG), "w": 2, "h": 2}
    assert pic["m.relates_to"] == {"m.in_reply_to": {"event_id": "$e1"}}
    assert pic["org.unseenet.message_id"] != post.message_id      # not taken for the text
    assert mx.events[0][0] != mx.events[1][0]                       # its own transaction


def test_taking_back_a_post_takes_its_pictures_back_too():
    gid = Manifest.from_article(make_manifest(OWNER, "m.r", "moderated", pow_bits=0)).group_id
    mx = FakeMatrix()
    s = sender(mx, gid)
    post = new_article(gid, "Look", "x", from_name="Alice").finalize(ALICE, 0)
    f = PlainFile(bl.blob_id(PNG), PNG, "image/png")
    eid = asyncio.run(s.send(post, [f, f]))
    assert asyncio.run(s.retract(post, eid))
    assert mx.redacted == ["$e1", "$e2", "$e3"]


def test_a_picture_matrix_will_not_take_does_not_stop_the_post():
    from unseenet_gateway.matrix.client import MatrixError
    gid = Manifest.from_article(make_manifest(OWNER, "m.r", "moderated", pow_bits=0)).group_id
    mx = FakeMatrix()

    async def no(*a, **kw):
        raise MatrixError(413, "M_TOO_LARGE", "too big")
    mx.upload = no
    post = new_article(gid, "Look", "x", from_name="Alice").finalize(ALICE, 0)
    assert asyncio.run(sender(mx, gid).send(post, [PlainFile("b", PNG, "image/png")])) == "$e1"
    assert len(mx.events) == 1


def test_nostr_says_there_are_pictures_on_unseenet():
    gid = Manifest.from_article(make_manifest(OWNER, "m.r", "moderated", pow_bits=0)).group_id
    one = new_article(gid, "s", "b", extra=bl.attachment_headers(FILES)).finalize(ALICE, 0)
    two = new_article(gid, "s", "b", extra=bl.attachment_headers(FILES + [
        ("d.png", "image/png", PNG + b"2")])).finalize(ALICE, 0)
    none = new_article(gid, "s", "b").finalize(ALICE, 0)
    assert "[1 picture on Unseenet]" in pictures_line(one)
    assert "[2 pictures on Unseenet]" in pictures_line(two)
    assert pictures_line(none) == ""
