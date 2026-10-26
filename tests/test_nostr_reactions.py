"""#32: Nostr reactions (kind 7, NIP-25) in through the gateway, UN-RFC 0048 section 9."""
import os

import pytest

from unseenet_gateway.nostr.convert import Skip, convert, npub
from unseenet_gateway.nostr.event import sign_event

ALICE = os.urandom(32)
GROUP = "nostr.example.lab@abcdabcdabcdabcd"


def conv(ev, takes=None):
    return convert(ev, group_id=GROUP, id_map=lambda eid: "<post@x>" if eid == "e1" else None,
                   title_of=lambda eid: None, name_of=lambda pk: "Alice", takes=takes)


@pytest.mark.parametrize("content, emoji", [("❤️", "❤️"), ("+", "👍"), ("", "👍"), ("-", "👎")])
def test_a_reaction_comes_in(content, emoji):
    ev = sign_event(ALICE, 7, content, [["h", "lab"], ["e", "e0"], ["e", "e1"]])
    art = conv(ev)
    assert art["Message-Kind"].strip() == "reaction" and art["Reaction"].strip() == emoji
    assert art["References"].split()[-1] == "<post@x>"                 # the last e tag
    assert art["Origin-Network"] == "nostr" and art["Origin-From"] == npub(ev.pubkey)
    assert art["Origin-Proof"]


@pytest.mark.parametrize("content, tags, why", [
    ("nice", [["h", "lab"], ["e", "e1"]], "not one emoji"),
    ("👍", [["h", "lab"], ["e", "unknown"]], "not imported"),
    ("👍", [["h", "lab"]], "not imported"),
])
def test_what_is_dropped(content, tags, why):
    with pytest.raises(Skip) as exc:
        conv(sign_event(ALICE, 7, content, tags))
    assert why in " ".join(map(str, exc.value.args))


def test_an_emoji_the_group_does_not_take():
    with pytest.raises(Skip):
        conv(sign_event(ALICE, 7, "🦔", [["h", "lab"], ["e", "e1"]]), takes=lambda e: e == "👍")
