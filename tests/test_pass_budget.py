"""An import pass leaves the others their turn (#51)."""

import asyncio

from unseenet_gateway.core import importer as imp
from unseenet_gateway.core.adapter import Incoming, Skip, Source
from unseenet_gateway.core.importer import Importer, RunStats


class Feed:
    network = label = "slow"

    def sources(self):
        return [Source(f"g{i}", f"g{i}@x") for i in range(4)]


def make(monkeypatch, seconds, step):
    clock = [0.0]
    monkeypatch.setattr(imp.time, "monotonic", lambda: clock[0])
    ran = []
    ix = Importer(feed=Feed(), state=None, node=None, gateway=None, pow_bits=0,
                  pass_seconds=seconds)

    async def run_source(source):
        ran.append(source.foreign)
        clock[0] += step
        return RunStats(imported=1)
    ix.run_source = run_source
    return ix, ran


def test_a_pass_stops_starting_groups_after_its_time_and_goes_on_next_pass(monkeypatch):
    ix, ran = make(monkeypatch, 300, 200)
    assert asyncio.run(ix.run()).imported == 2
    assert ran == ["g0", "g1"]
    asyncio.run(ix.run())
    assert ran == ["g0", "g1", "g2", "g3"]
    asyncio.run(ix.run())
    assert ran[-2:] == ["g0", "g1"]           # round again, every group in turn


def test_no_limit_runs_every_group(monkeypatch):
    ix, ran = make(monkeypatch, 0, 1000)
    assert asyncio.run(ix.run()).imported == 4 and ran == ["g0", "g1", "g2", "g3"]


def test_a_quick_pass_runs_every_group_from_the_first(monkeypatch):
    ix, ran = make(monkeypatch, 300, 1)
    asyncio.run(ix.run()), asyncio.run(ix.run())
    assert ran == ["g0", "g1", "g2", "g3"] * 2



class Usenetish:
    """A feed that keeps its cursor per message, as Usenet does; every message is
    skipped, so the importer's real loop runs without a node."""
    network = label = "news"
    resumable = True

    def __init__(self, clock, step):
        self.clock, self.step, self.closed, self.got = clock, step, 0, []

    def sources(self):
        return [Source("a", "a@x"), Source("b", "b@x")]

    async def fetch(self, source):
        try:
            for n in range(10):
                self.clock[0] += self.step
                yield Incoming(f"<{source.foreign}{n}@x>", n, label=str(n))
        finally:
            self.closed += 1

    def convert(self, source, item):
        self.got.append((source.foreign, item.payload))
        raise Skip("test")

    async def retractions(self, source):
        return []


class St:
    def lookup(self, net, fid):
        return None

    def count_skip(self, net, reason):
        pass


def test_a_resumable_feed_stops_inside_a_group_and_goes_on_there(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(imp.time, "monotonic", lambda: clock[0])
    feed = Usenetish(clock, 100)
    ix = Importer(feed=feed, state=St(), node=None, gateway=None, pow_bits=0, pass_seconds=300)
    asyncio.run(ix.run())
    assert feed.got == [("a", 0), ("a", 1)] and feed.closed == 1
    asyncio.run(ix.run())                     # the next pass begins in the same group
    assert feed.got[2] == ("a", 0)


def test_other_feeds_run_a_group_to_its_end(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(imp.time, "monotonic", lambda: clock[0])
    feed = Usenetish(clock, 100)
    feed.resumable = False
    ix = Importer(feed=feed, state=St(), node=None, gateway=None, pow_bits=0, pass_seconds=300)
    asyncio.run(ix.run())
    assert [g for g, _ in feed.got] == ["a"] * 10           # all of a, then time is up


def test_a_message_that_cannot_be_converted_is_skipped_not_the_pass(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(imp.time, "monotonic", lambda: clock[0])
    feed = Usenetish(clock, 0)

    def boom(source, item):
        feed.got.append((source.foreign, item.payload))
        raise UnicodeEncodeError("utf-8", "\udc86", 0, 1, "surrogates not allowed")
    feed.convert = boom
    ix = Importer(feed=feed, state=St(), node=None, gateway=None, pow_bits=0, pass_seconds=0)
    st = asyncio.run(ix.run())
    assert st.skipped == 20 and len(feed.got) == 20          # every message seen, none stopped it
