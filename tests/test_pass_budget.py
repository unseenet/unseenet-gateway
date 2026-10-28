"""An import pass leaves the others their turn (#51)."""

import asyncio

from unseenet_gateway.core import importer as imp
from unseenet_gateway.core.adapter import Source
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
