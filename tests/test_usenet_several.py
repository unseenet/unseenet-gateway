"""gateway#16: a Usenet group served by several gateways (UN-RFC 0038, UN-RFC 0024 6.1) -
joining another gateway's group in the configuration, and the same Usenet article
imported once, whichever gateway reads it first."""

import asyncio

from unseenet.core import crypto
from unseenet.core.article import new_article
from unseenet.core.groups import make_group_id, personal_inbox_id

from unseenet_gateway.core.cooperation import Watcher
from unseenet_gateway.core.state import State
from unseenet_gateway.core.unseenet_side import gateway_manifest
from unseenet_gateway.usenet.adapter import UsenetAdapter

from test_desk import Inbox

OWNER, GW1, GW2, OP = (crypto.Identity.generate() for _ in range(4))
NAME = "test.usenet.alt.test"
GID = make_group_id(NAME, OWNER.public_key)


def test_joining_another_gateways_group_in_the_configuration():
    ad = UsenetAdapter({"gateway": {"group_owner": GW2.key_text, "host": "gw2.lab.invalid"},
                        "usenet": {"prefix": "test.usenet", "direction": "import",
                                   "groups": ["comp.misc", {"name": "alt.test", "group": GID}]}})
    own, joined = ad.carried()
    assert not own.joined and own.name == "test.usenet.comp.misc"
    assert joined.joined and joined.group_id == GID and joined.source == "alt.test"
    assert joined.name == NAME and joined.view == "threads"
    assert "approval alone" in UsenetAdapter.consent_note       # no consent of its own


def test_an_article_another_gateway_imported_is_not_imported_again(tmp_path):
    """Both gateways read the same Usenet article; the one that sees the other's import
    in the group first remembers its origin pair and skips it (UN-RFC 0038 section 4)."""
    node = Inbox()
    m = gateway_manifest(OWNER, GW1, NAME, network="usenet", source="alt.test",
                         direction="import", host="gw1.lab.invalid", operator=OP.key_text,
                         contact=personal_inbox_id(OP.public_key), peers=[GW2.key_text],
                         requests=personal_inbox_id(GW1.public_key), pow_bits=0)
    asyncio.run(node.post(m))
    art = new_article(GID, "Test", "Hello from Usenet\n", from_name="Someone",
                      extra=[("Origin-Network", "usenet"), ("Origin-ID", "<abc@example.com>")])
    asyncio.run(node.post(art.finalize(GW1, 0)))
    state = State(tmp_path / "gw2.db")
    w = Watcher(state=state, node=node, gateway=GW2, groups=[GID])
    assert state.lookup("usenet", "<abc@example.com>") is None
    assert "1 articles" in asyncio.run(w.run())
    assert state.lookup("usenet", "<abc@example.com>") == art.message_id   # GW2 skips it now
    assert w.admitted(GID)
