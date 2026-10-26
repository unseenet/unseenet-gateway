"""Matrix adapter: names and conversion. Synthetic events only."""

from datetime import datetime, timezone

import pytest

from unseenet.core import crypto
from unseenet.core.article import Article, validate_structure
from unseenet.core.groups import make_group_id

from unseenet_gateway.core.adapter import Skip
from unseenet_gateway.matrix.adapter import MatrixAdapter
from unseenet_gateway.matrix.convert import (FROM_UNSEENET_KEY, convert, strip_reply_fallback,
                                             unseenet_name)
from unseenet_gateway.matrix.exporter import txn_id

OWNER, GW = crypto.Identity.generate(), crypto.Identity.generate()
GROUP_ID = make_group_id("test.matrix.lab", OWNER.public_key)
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp() * 1000) - 60_000
ROOM = "!lab:example.org"
MID = "<" + "a" * 32 + "@unseenet>"
MID2 = "<" + "b" * 32 + "@unseenet>"


def ev(body="Hello there", *, eid="$e1", sender="@alice:example.org", msgtype="m.text",
       rel=None, etype="m.room.message", **extra):
    content = {"msgtype": msgtype, "body": body, **extra}
    if rel:
        content["m.relates_to"] = rel
    return {"type": etype, "event_id": eid, "sender": sender, "room_id": ROOM,
            "origin_server_ts": TS, "content": content}


def conv(e, ids=None, names=None, gateway_user=None):
    return convert(e, group_id=GROUP_ID, id_map=(ids or {}).get, name_of=(names or {}).get,
                   gateway_user=gateway_user, now=NOW)


def test_names():
    assert unseenet_name("#python:matrix.org", "matrix") == "matrix.matrix-org.python"
    assert unseenet_name("#Dk.Snak:example.org:8448", "test.matrix") == \
        "test.matrix.example-org-8448.dk-snak"
    assert unseenet_name("!abcdef:matrix.org", "matrix") is None     # ids need a chosen name
    assert unseenet_name("#___:matrix.org", "matrix") is None


def test_text_is_a_valid_chat_line():
    art = conv(ev(), names={"@alice:example.org": "Alice"}).finalize(GW, 8)
    raw = art.to_bytes()
    validate_structure(Article.from_bytes(raw), raw_size=len(raw), now=NOW)
    assert art["Message-Kind"] == "chat" and art["Subject"] == ""
    assert art["From"] == "Alice" and art.body.rstrip("\n") == "Hello there"
    assert art["Origin-Network"] == "matrix" and art["Origin-ID"] == "$e1"
    assert art["Origin-From"] == "@alice:example.org" and art["Matrix-Room"] == ROOM
    assert "Origin-Proof" not in art


def test_from_falls_back_to_localpart_and_emote():
    art = conv(ev("waves", msgtype="m.emote"))
    assert art["From"] == "alice" and art.body.startswith("* alice waves")
    assert art["Matrix-Msgtype"] == "m.emote"


@pytest.mark.parametrize("e,reason", [
    (ev(msgtype="m.video", url="mxc://x/y"), "media"),
    (ev(msgtype="m.image", url="https://elsewhere/y.png"), "media"),   # only mxc://
    (ev(msgtype="m.file"), "media"),
    (ev(etype="m.room.encrypted"), "encrypted"),
    (ev(etype="m.sticker"), "type"),
    (ev("   "), "empty"),
    ({"type": "m.room.message", "event_id": "$r", "sender": "@a:x", "content": {}}, "redacted"),
    (ev(**{FROM_UNSEENET_KEY: MID}), "loop"),
])
def test_skips(e, reason):
    with pytest.raises(Skip) as s:
        conv(e)
    assert s.value.reason == reason


def test_own_account_is_a_loop():
    with pytest.raises(Skip):
        conv(ev(sender="@gw:example.org"), gateway_user="@gw:example.org")


def test_reply_strips_fallback_and_references_parent():
    body = "> <@bob:example.org> original\n> second line\n\nMy answer"
    art = conv(ev(body, eid="$e2", rel={"m.in_reply_to": {"event_id": "$e1"}}),
               ids={"$e1": MID})
    assert art.body.rstrip("\n") == "My answer" and art["References"] == MID


def test_fallback_left_alone_without_reply():
    assert strip_reply_fallback("plain\ntext") == "plain\ntext"


def test_thread_references_root_then_parent():
    rel = {"rel_type": "m.thread", "event_id": "$root", "is_falling_back": False,
           "m.in_reply_to": {"event_id": "$p"}}
    art = conv(ev(rel=rel), ids={"$root": MID, "$p": MID2})
    assert art["References"] == f"{MID} {MID2}"


def test_thread_fallback_reply_is_ignored():
    rel = {"rel_type": "m.thread", "event_id": "$root", "is_falling_back": True,
           "m.in_reply_to": {"event_id": "$latest"}}
    art = conv(ev(rel=rel), ids={"$root": MID, "$latest": MID2})
    assert art["References"] == MID


def test_edit_becomes_new_article_referring_to_the_old():
    e = ev("* fixed", eid="$e3", rel={"rel_type": "m.replace", "event_id": "$e1"},
           **{"m.new_content": {"msgtype": "m.text", "body": "fixed"}})
    art = conv(e, ids={"$e1": MID})
    assert art.body.rstrip("\n") == "fixed" and art["References"] == MID
    assert art["Matrix-Edit"] == "$e1"
    with pytest.raises(Skip) as s:
        conv(e)
    assert s.value.reason == "edit"


def test_txn_id_is_stable():
    assert txn_id(MID) == txn_id(MID) != txn_id(MID2)


def test_adapter_rooms_and_manifest_fields(tmp_path):
    cfg = {"gateway": {"group_owner": OWNER.key_text, "gateway_key": str(tmp_path / "gw.key")},
           "matrix": {"homeserver": "https://example.org", "user": "@gw:example.org",
                      "prefix": "test.matrix", "direction": "both",
                      "rooms": ["#lab:example.org", {"room": ROOM, "name": "lab2"}]}}
    ad = MatrixAdapter(cfg)
    carried = ad.carried()
    assert [c.name for c in carried] == ["test.matrix.example-org.lab", "test.matrix.lab2"]
    assert all(c.view == "chat" and c.kinds == ("chat",) for c in carried)
    assert ad.host() == "@gw:example.org"
    assert ad.token_path() == tmp_path / "matrix.token"
    assert "kick this account" in ad.announcement("#lab:example.org", carried[0].name)


# --- consent and openness -------------------------------------------------------

from unseenet_gateway.matrix.client import Matrix
from unseenet_gateway.matrix.feed import openness


def st(join_rule=None, encrypted=False):
    out = []
    if join_rule:
        out.append({"type": "m.room.join_rules", "content": {"join_rule": join_rule}})
    if encrypted:
        out.append({"type": "m.room.encryption", "content": {"algorithm": "m.megolm.v1.aes-sha2"}})
    return out


def test_openness():
    assert openness(st("public")) is None
    assert openness(st("public", encrypted=True)) == "encrypted"
    assert "not public" in openness(st("invite"))
    assert "not public" in openness(st("knock"))
    assert openness(st()) == "of unknown join rule"
    space = st("public") + [{"type": "m.room.create", "content": {"type": "m.space"}}]
    assert openness(space) == "a space, not a room"


def test_invites_are_tracked_across_incremental_syncs():
    import asyncio
    replies = [
        {"next_batch": "s1", "rooms": {"invite": {"!a:x": {"invite_state": {"events": st("public")}}}}},
        {"next_batch": "s2", "rooms": {"invite": {"!b:x": {"invite_state": {"events": []}}}}},
        {"next_batch": "s3", "rooms": {"join": {"!a:x": {}}}},
    ]
    seen = []

    class Fake(Matrix):
        def _call(self, method, path, params=None, body=None):
            seen.append((params or {}).get("since"))
            assert (params or {}).get("set_presence") == "offline"    # never shows as online
            return replies.pop(0)

    m = Fake("https://x")
    assert set(asyncio.run(m.invites())) == {"!a:x"}
    assert set(asyncio.run(m.invites())) == {"!a:x", "!b:x"}     # earlier invite kept
    assert set(asyncio.run(m.invites())) == {"!b:x"}             # joined elsewhere: gone
    assert seen == [None, "s1", "s2"]


def test_alias_is_pinned_to_its_first_room(tmp_path):
    import asyncio
    from unseenet_gateway.core.state import State
    from unseenet_gateway.matrix.feed import MatrixFeed, RoomMoved

    class FakeMatrix:
        base, user_id, target = "https://x", "@gw:x", "!one:x"
        async def resolve(self, room):
            return self.target

    st, mx = State(tmp_path / "s.db"), FakeMatrix()
    assert asyncio.run(MatrixFeed(state=st, matrix=mx, carried={}).room_id("#lab:x")) == "!one:x"
    mx.target = "!two:x"
    with pytest.raises(RoomMoved):
        asyncio.run(MatrixFeed(state=st, matrix=mx, carried={}).room_id("#lab:x"))


def test_portal_counterpart_needs_an_alias(tmp_path):
    from unseenet_gateway.core.state import State
    from unseenet_gateway.matrix.portal import MatrixCounterpart, portal_topic
    with pytest.raises(ValueError):
        MatrixCounterpart(state=State(tmp_path / "s.db"), matrix=None, alias="!id:x",
                          group_id=GROUP_ID)
    cp = MatrixCounterpart(state=State(tmp_path / "s.db"), matrix=None,
                           alias="#unseenet:unseenet.org", group_id=GROUP_ID,
                           admins=["@a:example.org"])
    assert cp.source == "#unseenet:unseenet.org" and cp.network == "matrix"
    t = portal_topic("unseenet", GROUP_ID)
    assert GROUP_ID in t and "late" in t and "unseenet.org" in t


def test_new_thread_carries_its_subject():
    import asyncio
    from unseenet_gateway.matrix.exporter import MatrixSender
    from unseenet.core.article import new_article

    sent = {}

    class FakeMatrix:
        async def membership(self, rid):
            return "join"

        async def messages(self, rid, **kw):
            return {"chunk": []}

        async def send(self, rid, content, txn):
            sent[txn] = content["body"]
            return "$x"

    class FakeState:
        def foreign_for(self, mid):
            return None

    async def rid(_):
        return "!r:x"

    s = MatrixSender(matrix=FakeMatrix(), state=FakeState(), rooms={GROUP_ID: "#r:x"},
                     room_id_of=rid)
    me = crypto.Identity.generate()
    post = new_article(GROUP_ID, "Topic", "Body", from_name="Dora").finalize(me, 0)
    chat = new_article(GROUP_ID, "", "Line", from_name="Dora",
                       extra=[("Message-Kind", "chat")]).finalize(me, 0)
    asyncio.run(s.send(post)), asyncio.run(s.send(chat))
    bodies = list(sent.values())
    assert bodies[0].split("\n")[0].endswith("on Unseenet: Topic")
    assert bodies[1].split("\n")[0].endswith("on Unseenet:")


def test_join_survives_a_room_where_the_gateway_may_not_write(tmp_path):
    import asyncio
    from unseenet_gateway.core.state import State
    from unseenet_gateway.matrix.client import MatrixError
    from unseenet_gateway.matrix.feed import MatrixFeed

    class FakeMatrix:
        base, user_id = "https://x", "@gw:x"
        joined = False
        async def resolve(self, room): return "!r:x"
        async def membership(self, rid): return "join" if self.joined else None
        async def invites(self): return {"!r:x": st("public")}
        async def whoami(self): return self.user_id
        async def join(self, rid): self.joined = True
        async def send(self, *a, **k): raise MatrixError(403, "M_FORBIDDEN", "send_level 100")

    feed = MatrixFeed(state=State(tmp_path / "s.db"), matrix=FakeMatrix(), carried={},
                      announce={"#r:x": "hello"})
    assert asyncio.run(feed.ensure_joined("#r:x")) == "!r:x"


def test_markdown_goes_to_matrix_formatted_and_safe():
    import asyncio
    from unseenet.core.article import new_article
    from unseenet.core.markup import MARKDOWN_TYPE
    from unseenet_gateway.core.adapter import is_text
    from unseenet_gateway.matrix.exporter import MatrixSender, markdown_html

    html = markdown_html("**bold** [ok](https://unseenet.org) [bad](javascript:alert(1)) "
                         "<script>x</script> ![img](https://x/y.png)")
    assert "<strong>bold</strong>" in html and 'href="https://unseenet.org"' in html
    assert "javascript:" not in html.split("href=")[-1][:20] and "<script>" not in html
    assert "<img" not in html

    sent = {}

    class FakeMatrix:
        async def membership(self, rid):
            return "join"

        async def messages(self, rid, **kw):
            return {"chunk": []}

        async def send(self, rid, content, txn):
            sent.update(content)
            return "$x"

    class FakeState:
        def foreign_for(self, mid):
            return None

    async def rid(_):
        return "!r:x"

    art = new_article(GROUP_ID, "", "Hej **verden**", from_name="Andreas",
                      extra=[("Content-Type", MARKDOWN_TYPE), ("Message-Kind", "chat")]
                      ).finalize(crypto.Identity.generate(), 0)
    assert is_text(art)
    asyncio.run(MatrixSender(matrix=FakeMatrix(), state=FakeState(), rooms={GROUP_ID: "#r:x"},
                             room_id_of=rid).send(art))
    assert sent["body"].endswith("Hej **verden**")
    assert sent["format"] == "org.matrix.custom.html"
    assert "<strong>verden</strong>" in sent["formatted_body"] and "Andreas" in sent["formatted_body"]


def test_another_gateways_notice_is_never_imported():
    import pytest
    from unseenet_gateway.core.adapter import Skip
    from unseenet_gateway.matrix.convert import GATEWAY_NOTICE_KEY, convert
    ev = {"type": "m.room.message", "event_id": "$n", "sender": "@gw2:x", "origin_server_ts": 1,
          "content": {"msgtype": "m.notice", "body": "This room is now gatewayed",
                      GATEWAY_NOTICE_KEY: True}}
    with pytest.raises(Skip):
        convert(ev, group_id="matrix.x.r@aaaaaaaaaaaaaaaa", id_map=lambda e: None,
                name_of=lambda u: None, gateway_user="@gw1:x")


# --- formatted_body as Markdown (gateway#26) ---------------------------------------

def test_formatted_body_comes_in_as_markdown():
    e = ev("Hello *world* (plain)", format="org.matrix.custom.html",
           formatted_body='<b>Hello</b> <a href="https://example.org">world</a> 2*3')
    art = conv(e).finalize(GW, 8)
    raw = art.to_bytes()
    validate_structure(Article.from_bytes(raw), raw_size=len(raw), now=NOW)
    assert art["Content-Type"].startswith("text/markdown")
    assert art.body.strip() == r"**Hello** [world](https://example.org) 2\*3"


def test_no_html_is_passed_on_and_bad_links_are_dropped():
    from unseenet_gateway.matrix.html import to_markdown
    md = to_markdown('<script>x()</script><span onclick="y()">hi</span> '
                     '<a href="javascript:alert(1)">click</a> <img src="mxc://a/b" alt="cat">')
    assert "<" not in md and "javascript" not in md and md.strip() == "hi click cat"


def test_markdown_reply_leaves_the_quoted_fallback_out():
    e = ev("> <@bob:example.org> old\n\nnew", format="org.matrix.custom.html",
           formatted_body="<mx-reply><blockquote>old</blockquote></mx-reply><i>new</i>",
           rel={"m.in_reply_to": {"event_id": "$old"}})
    art = conv(e, ids={"$old": MID})
    assert art.body.strip() == "*new*" and art["References"] == MID


def test_lists_quotes_and_code():
    from unseenet_gateway.matrix.html import to_markdown
    assert to_markdown("<ul><li>a</li><li>b</li></ul>") == "- a\n- b\n"
    assert to_markdown('<ol start="3"><li>x</li></ol>') == "3. x\n"
    assert to_markdown("<blockquote>q</blockquote>after") == "> q\n\nafter\n"
    assert to_markdown("<pre><code>a = 1\n</code></pre>") == "```\na = 1\n```\n"
    assert to_markdown("1. no list") == "1\\. no list\n"


def test_plain_text_stays_plain():
    art = conv(ev("Just *text*"))
    assert art["Content-Type"].startswith("text/plain") and art.body.strip() == "Just *text*"


# --- pictures in (gateway#26) ------------------------------------------------------

def test_a_picture_is_a_chat_line_with_its_caption_and_a_mark():
    art = conv(ev("A cat", msgtype="m.image", url="mxc://example.org/abc", filename="cat.png"))
    assert art.body.strip() == "A cat" and art["Gateway-Media"] == "1"
    bare = conv(ev("IMG_1.jpg", msgtype="m.image", url="mxc://example.org/abc"))
    assert bare.body.strip() == "(a picture)"           # a filename is not a caption


class _Feed:
    network = "matrix"

    def __init__(self, files):
        self.files = files

    async def media(self, source, item):
        return self.files


class _Node:
    def __init__(self):
        self.posted = []

    async def post(self, art, *, blobs=(), extra_bits=0):
        self.posted.append((art, list(blobs), extra_bits))
        return "ok"


def _png(size=8):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_the_importer_puts_the_picture_on_the_article():
    import asyncio
    import pytest
    from unseenet_gateway.core import files as fl
    from unseenet_gateway.core.importer import Importer
    if not fl.core_posts_files():
        pytest.skip("this core cannot post files (unseenet 0.5)")
    pytest.importorskip("PIL")
    imp = Importer(feed=_Feed([fl.Media("cat.png", _png(), "A red square")]), state=None,
                   node=_Node(), gateway=GW, pow_bits=8, pictures_max=1 << 20)
    art = conv(ev("A cat", msgtype="m.image", url="mxc://example.org/abc", filename="cat.png"))
    blobs, bits = asyncio.run(imp._pictures(None, type("I", (), {"label": "#1"})(), art))
    assert len(blobs) == 1 and art.get("Blobs", "").startswith(blobs[0][0])
    assert "Gateway-Media" not in art and art["Attachment-1"].startswith(blobs[0][0])
    assert bits >= 0 and art.body.strip() == "A cat"


def test_without_pictures_a_line_says_one_was_left_out():
    import asyncio
    from unseenet_gateway.core import files as fl
    from unseenet_gateway.core.importer import Importer
    imp = Importer(feed=_Feed([fl.Media("cat.png", b"x")]), state=None, node=_Node(),
                   gateway=GW, pow_bits=8, pictures_max=0)
    art = conv(ev("A cat", msgtype="m.image", url="mxc://example.org/abc", filename="cat.png"))
    blobs, _ = asyncio.run(imp._pictures(None, type("I", (), {"label": "#1"})(), art))
    assert blobs == [] and "Gateway-Media" not in art
    assert art.body.strip().endswith("(a picture was left out - this gateway carries no pictures)")


def test_a_gatewayed_group_says_it_takes_pictures():
    from unseenet_gateway.core import files as fl
    from unseenet_gateway.core.unseenet_side import gateway_manifest
    att = fl.attachments_field(1 << 20)
    art = gateway_manifest(OWNER, GW, "test.matrix.lab", network="matrix", source="#lab:x",
                           direction="import", host="@gw:x", operator="o", contact="c",
                           attachments=att, pow_bits=8)
    if fl.core_posts_files():
        assert '"attachments"' in art.body and '"image/*"' in art.body
    assert fl.attachments_field(0) is None
