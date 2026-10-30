"""gateway#63: an imported message with a link gets a link card (UN-RFC 0059 section 11),
made over the gateway's Tor without the page's picture, signed with the message; none when
turned off, without Tor, without a link, or when the page gives no title."""
import asyncio
from datetime import datetime, timezone

import pytest

session = pytest.importorskip("unseenet.client.session")
if not hasattr(session, "fetch_card"):
    pytest.skip("the core makes no cards without a Session (unseenet#797)", allow_module_level=True)
from unseenet.client.session import CardDraft
from unseenet.core import cards as cd

from unseenet_gateway.core.importer import Importer
from unseenet_gateway.core.state import State

from test_adapter_core import GW, FakeNode, LoopFeed

URL = "https://www.esa.int/proba-3"


class TorNode(FakeNode):
    def __init__(self, socks_port=9050):
        super().__init__()
        self.socks_port = socks_port


@pytest.fixture
def made(monkeypatch):
    asked = []

    async def fetch_card(url, socks, *, language="en", picture=True):
        asked.append((url, socks, picture))
        title = "" if "notitle" in url else "Kunstig solformørkelse"
        return CardDraft(url=url, title=title, site="ESA",
                         made=datetime(2026, 10, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(session, "fetch_card", fetch_card)
    return asked


def _import(tmp_path, text, node, cards=True):
    state = State(tmp_path / "gw.db")
    imp = Importer(feed=LoopFeed(state, [{"id": "a", "text": text}]), state=state, node=node,
                   gateway=GW, pow_bits=0, link_cards=cards)
    asyncio.run(imp.run())
    (art,) = node.posted
    return art


def test_an_imported_link_gets_a_card(tmp_path, made):
    art = _import(tmp_path, f"Se lige {URL}", TorNode())
    card = cd.check(art[cd.HEADER], art.body, [])
    assert card.url == URL and card.title == "Kunstig solformørkelse" and card.image is None
    assert made == [(URL, 9050, False)]                    # never the page's picture
    art.verify_signature()                                 # the card is signed with it


@pytest.mark.parametrize("text,node,cards", [
    (f"Se {URL}", TorNode(), False),                       # turned off (the default)
    (f"Se {URL}", TorNode(None), True),                    # no Tor
    ("no link here", TorNode(), True),
    ("Se https://example.org/notitle", TorNode(), True),   # the page gives no title
])
def test_no_card(tmp_path, made, text, node, cards):
    art = _import(tmp_path, text, node, cards)
    assert art.get(cd.HEADER) is None
