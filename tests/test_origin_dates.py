"""gateway#41: what every adapter imports keeps the other network's date in Origin-Date,
in a form the core reads (UN-RFC 0049 section 4.2), is posted with the time of posting
(section 3.1), and history brought in late is old on arrival (section 5.2)."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

origins = pytest.importorskip("unseenet.core.origins", reason="the core's 0.6 (UN-RFC 0049)")
from unseenet.core.article import new_article, parse_date

from unseenet_gateway.core.importer import Importer
from unseenet_gateway.core.origin import origin_headers
from unseenet_gateway.core.state import State

from test_adapter_core import GW, FakeNode, LoopFeed

NOW = datetime.now(timezone.utc)


def test_every_adapters_origin_date_is_read_by_the_core():
    usenet = "Mon, 03 Aug 2026 16:20:00 +0200"                     # copied from the article
    matrix = format_datetime(datetime(2026, 8, 3, 14, 20, tzinfo=timezone.utc))   # from a timestamp
    for network, text in (("usenet", usenet), ("matrix", matrix), ("nostr", matrix)):
        h = dict(origin_headers(network, "id-1", text, "someone"))
        o = origins.origin_of(h.get, NOW)
        assert o.date == datetime(2026, 8, 3, 14, 20, tzinfo=timezone.utc), network
        assert origins.old_on_arrival(o.date, NOW)                 # brought in late: not new


def test_a_live_message_is_not_old_on_arrival():
    h = dict(origin_headers("matrix", "id-2", format_datetime(NOW - timedelta(minutes=3))))
    o = origins.origin_of(h.get, NOW)
    assert o.date is not None and not origins.old_on_arrival(o.date, NOW)


class SlowFeed(LoopFeed):
    """Converts with a Date long ago, as if pictures took hours to fetch."""

    def convert(self, source, item):
        art = super().convert(source, item)
        art.set("Date", (NOW - timedelta(hours=5)).strftime("%a, %d %b %Y %H:%M:%S +0000"))
        return art


def test_posted_with_the_time_of_posting(tmp_path):
    state = State(tmp_path / "gw.db")
    node = FakeNode()
    imp = Importer(feed=SlowFeed(state, [{"id": "a", "text": "hello"}]), state=state, node=node,
                   gateway=GW, pow_bits=0)
    asyncio.run(imp.run())
    (art,) = node.posted
    assert datetime.now(timezone.utc) - parse_date(art["Date"]) < timedelta(minutes=11)
    art.verify_signature()                                         # signed after the new Date
