"""Shares through the gateway (UN-RFC 0060 section 9, #66): reposts and quotes in from
Nostr, reposts, quotes and plain text out, and share notices never carried."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from unseenet.core import crypto, shares as sh
from unseenet.core.article import Article, new_article, validate_structure
from unseenet.core.groups import make_group_id

from unseenet_gateway.core import sharing
from unseenet_gateway.core.portal import exportable
from unseenet_gateway.core.sharing import Takes
from unseenet_gateway.core.state import State
from unseenet_gateway.nostr.convert import Skip, convert, note_id
from unseenet_gateway.nostr.event import sign_event
from unseenet_gateway.nostr.exporter import NostrSender

from test_nostr import ALICE, BOB, GW, OWNER, _Relay

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
FEED = make_group_id("nostr.feed", OWNER.public_key)
LAB = make_group_id("nostr.lab", OWNER.public_key)
TARGET = "<" + "t" * 32 + "@unseenet>"
SHARER = crypto.Identity.generate()


def conv(ev, *, carried=None, shares=Takes(True, True)):
    carried = carried or {}
    return convert(ev, group_id=FEED, id_map=lambda e: carried.get(e, (None,))[0],
                   title_of={}.get, name_of={ev.pubkey: "Bob"}.get, now=NOW,
                   group_of=lambda mid: next((g for m, g in carried.values() if m == mid), None),
                   shares=shares)


def test_takes_from_the_manifest():
    assert Takes.of(None) == Takes(False, False)
    assert Takes.of({"view": "threads"}) == Takes(True, False)      # needs a comment
    assert Takes.of({"view": "chat"}) == Takes(False, False)        # chat: no shares
    assert Takes.of({"view": "feed"}) == Takes(True, True)
    assert Takes.of({"view": "feed", "share_into": False}) == Takes(False, False)


def test_a_repost_of_what_the_gateway_carries_is_a_plain_share():
    orig = sign_event(ALICE, 11, "Original", [["h", "lab"], ["title", "T"]])
    rp = sign_event(BOB, 16, orig.to_json(), [["h", "feed"], ["e", orig.id], ["k", "11"]])
    art = conv(rp, carried={orig.id: (TARGET, LAB)}).finalize(GW, 8)
    raw = art.to_bytes()
    validate_structure(Article.from_bytes(raw), raw_size=len(raw))
    sh.check_form(art)
    assert sh.is_plain(art) and sh.target(art) == (TARGET, LAB)
    assert art["From"] == "Bob" and art["Origin-ID"] == rp.id and art["Nostr-Kind"] == "16"
    assert "Original" not in art.body                       # never the original's text
    # a group that takes no plain shares: not imported
    with pytest.raises(Skip) as e:
        conv(rp, carried={orig.id: (TARGET, LAB)}, shares=Takes(True, False))
    assert e.value.reason == "repost"


def test_a_repost_of_something_else_is_a_post_with_a_link():
    other = "e" * 64
    rp = sign_event(BOB, 6, "", [["h", "feed"], ["e", other]])
    art = conv(rp)
    assert not sh.is_share(art) and f"nostr:{note_id(other)}" in art.body
    assert note_id(other).startswith("note1")
    with pytest.raises(Skip):
        conv(sign_event(BOB, 6, "", [["h", "feed"]]))          # reposts nothing


def test_a_quote_in_a_thread_is_a_share_with_a_comment():
    orig = sign_event(ALICE, 11, "Original", [["h", "lab"], ["title", "T"]])
    q = sign_event(BOB, 11, "Look at this", [["h", "feed"], ["title", "Look"],
                                             ["q", orig.id, "wss://relay.example", orig.pubkey]])
    art = conv(q, carried={orig.id: (TARGET, LAB)})
    assert sh.target(art) == (TARGET, LAB) and not sh.is_plain(art)
    sh.check_form(art)
    assert art.body.startswith("Look at this")
    # not carried, or a group without shares: an ordinary post
    assert not sh.is_share(conv(q))
    assert not sh.is_share(conv(q, carried={orig.id: (TARGET, LAB)}, shares=Takes()))
    # a chat line's q is a reply, never a share
    chat = sign_event(BOB, 9, "re", [["h", "feed"], ["q", orig.id]])
    assert not sh.is_share(conv(chat, carried={orig.id: (TARGET, LAB)}))


def _share(plain: bool) -> Article:
    if plain:
        art = sh.build_plain(FEED, TARGET, LAB)
    else:
        art = new_article(FEED, "", "Se her\n", extra=[("Message-Kind", "chat"),
                                                       *sh.headers(TARGET, LAB)])
    return art.finalize(SHARER, 8)


def test_out_text_never_carries_the_original():
    plain, said = _share(True), _share(False)
    where = sh.address(LAB, TARGET)
    assert sharing.out_text(plain) == f"Shared: {where}"
    assert sharing.out_text(said) == f"Se her\n\nShared: {where}"
    assert sharing.out_text(new_article(FEED, "x", "plain\n")) == "plain"


def test_share_notices_are_never_exported():
    notice = new_article(FEED, "", "x\n", extra=[("Content-Type", sh.NOTICE_TYPE)])
    assert exportable(notice.finalize(SHARER, 8), GW.public_key) == "share notice"


def _sender(tmp_path, events):
    relay = _Relay(events)
    state = State(Path(tmp_path) / "s.db")
    state.set_cursor("nostr", "joined:wss://relay.example'feed", 1)
    return NostrSender(relay=relay, secret=ALICE, state=state, groups={FEED: "feed"}), relay, state


def test_out_a_plain_share_of_a_nostr_event_is_a_repost(tmp_path):
    orig = sign_event(BOB, 11, "Original", [["h", "lab"], ["title", "T"]])
    sender, relay, state = _sender(tmp_path, [orig])
    state.remember("nostr", orig.id, TARGET, LAB)
    assert state.group_for(TARGET) == LAB
    asyncio.run(sender.send(_share(True)))
    ev = relay.published[-1]
    assert ev.kind == 16 and ev.content == ""
    assert ["e", orig.id, "wss://relay.example"] in ev.tags and ["k", "11"] in ev.tags
    assert ["h", "feed"] in ev.tags and ev.tag("unseenet")


def test_out_a_share_with_a_comment_is_a_quote(tmp_path):
    orig = sign_event(BOB, 11, "Original", [["h", "lab"], ["title", "T"]])
    sender, relay, state = _sender(tmp_path, [orig])
    state.remember("nostr", orig.id, TARGET, LAB)
    asyncio.run(sender.send(_share(False)))
    ev = relay.published[-1]
    assert ev.kind == 11 and ev.tag("q") == orig.id
    assert ev.content.startswith("Se her") and f"nostr:{note_id(orig.id)}" in ev.content
    assert "Original" not in ev.content and "Shared: unseenet:" not in ev.content


def test_out_a_share_of_something_not_on_nostr_is_its_text_and_address(tmp_path):
    sender, relay, _ = _sender(tmp_path, [])
    asyncio.run(sender.send(_share(True)))
    ev = relay.published[-1]
    assert ev.kind == 11 and ev.content.startswith(f"Shared: {sh.address(LAB, TARGET)}")
    assert not ev.tag("q")
