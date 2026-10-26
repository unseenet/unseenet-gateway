"""#32: Matrix reactions (m.reaction annotations) in through the gateway, UN-RFC 0048 section 9."""
import pytest

from unseenet_gateway.core.adapter import Skip
from unseenet_gateway.matrix.convert import REACTION, convert

GROUP = "matrix.example.room@abcdabcdabcdabcd"


def ev(key="👍", target="$m1", sender="@sonia:example.org", **kw):
    e = {"type": REACTION, "event_id": "$r1", "room_id": "!room:example.org", "sender": sender,
         "origin_server_ts": 1_761_480_000_000,
         "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": key}}}
    e.update(kw)
    return e


def conv(e, takes=None):
    return convert(e, group_id=GROUP, id_map=lambda eid: "<post@x>" if eid == "$m1" else None,
                   name_of=lambda uid: "Sonia", gateway_user="@gw:example.org", takes=takes)


def test_a_reaction_comes_in_with_who_gave_it():
    art = conv(ev())
    assert art["Message-Kind"].strip() == "reaction" and art["Reaction"].strip() == "👍"
    assert art["References"].split()[-1] == "<post@x>"
    assert art["Origin-Network"] == "matrix" and art["Origin-From"] == "@sonia:example.org"
    assert art["Origin-ID"] == "$r1" and art["From"].startswith("Sonia")


@pytest.mark.parametrize("e, why", [
    (ev(key="+1"), "not one emoji"),
    (ev(key="👍👍"), "not one emoji"),
    (ev(target="$unknown"), "not imported"),
    (ev(sender="@gw:example.org", content={"m.relates_to": {"rel_type": "m.annotation", "event_id": "$m1",
                                                             "key": "👍"}, "org.unseenet.message_id": "<x@y>"}),
     "Unseenet"),
])
def test_what_is_dropped(e, why):
    with pytest.raises(Skip) as exc:
        conv(e)
    assert why in str(exc.value) or why in repr(exc.value.args)


def test_an_emoji_the_group_does_not_take_is_dropped_not_changed():
    with pytest.raises(Skip):
        conv(ev(key="🦔"), takes=lambda e: e in ("👍", "❤️"))
    assert conv(ev(key="❤️"), takes=lambda e: e in ("👍", "❤️"))["Reaction"].strip() == "❤️"
