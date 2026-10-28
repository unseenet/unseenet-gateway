"""The gateway posts through the core's Session (gateway#19): ripening, Key-Ripening and
postage as everywhere else (UN-RFC 0025)."""
import asyncio

import pytest

from unseenet.core import crypto
from unseenet.core import ripening as rp
from unseenet.core.article import new_article
from unseenet.core.groups import make_group_id, make_manifest
from unseenet.core.store import Store
from unseenet.node.server import Node as UnseenetNode, NodeConfig

from unseenet_gateway.core.unseenet_side import Node

pytest.importorskip("chiavdf")
pytestmark = pytest.mark.usefixtures("ripening_rules")


def test_imports_ripen_carry_the_header_and_pay_the_level(tmp_path):
    asyncio.run(_imports(tmp_path))


async def _imports(tmp_path):
    owner, gateway = crypto.Identity.generate(), crypto.Identity.generate()
    store = UnseenetNode(Store(), NodeConfig(min_pow=8, forward_delay=(0, 0)))
    server = await store.serve("127.0.0.1", 0)
    addr = f"127.0.0.1:{server.sockets[0].getsockname()[1]}"
    node = Node(addr, None)
    node.use_key(gateway, tmp_path / "gateway.key.state.json")
    node.use_key(owner, tmp_path / "group.key.state.json")

    # the group key's manifest: ripened first, like any article of ours
    manifest = make_manifest(owner, "alt.gw.test", "moderated", moderators=[gateway.key_text],
                             pow_bits=12)
    assert await node.post(manifest) == "ok"
    gid = make_group_id("alt.gw.test", owner.public_key)

    art = new_article(gid, "Imported", "From the other network\n", from_name="someone",
                      extra=[("Content-Type", "text/plain; charset=utf-8")])
    art.finalize(gateway, 8)                       # what the importer does; too little now
    assert await node.post(art) == "ok"
    raw = store.store.get(art.message_id).decode()
    assert "Key-Ripening: vdf-cg1; t=" in raw
    assert store.store.manifest(make_group_id(rp.GROUP_NAME, gateway.public_key)) is not None
    assert await node.post(art) == "duplicate"
    assert (tmp_path / "gateway.key.state.json").exists()
    await node.shutdown()
    server.close()


def test_an_import_that_waited_over_an_hour_is_posted_with_a_new_date(tmp_path):
    """UN-RFC 0049 3.2 and 3.4 (#38): a node refuses a Date more than an hour old with
    ``441 date``; the gateway's session gives it the time of posting, signs it again and
    posts once more - the article is changed in place, so the importer remembers the
    Message-ID that was posted."""
    asyncio.run(_late(tmp_path))


async def _late(tmp_path):
    from datetime import datetime, timedelta, timezone

    from unseenet.core.article import format_date, parse_date
    owner, gateway = crypto.Identity.generate(), crypto.Identity.generate()
    store = UnseenetNode(Store(), NodeConfig(min_pow=8, forward_delay=(0, 0)))
    server = await store.serve("127.0.0.1", 0)
    addr = f"127.0.0.1:{server.sockets[0].getsockname()[1]}"
    node = Node(addr, None)
    node.use_key(gateway, tmp_path / "gateway.key.state.json")
    node.use_key(owner, tmp_path / "group.key.state.json")
    manifest = make_manifest(owner, "alt.gw.late", "moderated", moderators=[gateway.key_text],
                             pow_bits=12)
    assert await node.post(manifest) == "ok"
    gid = make_group_id("alt.gw.late", owner.public_key)

    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    art = new_article(gid, "Imported", "Fetched long ago\n", from_name="someone",
                      extra=[("Content-Type", "text/plain; charset=utf-8")])
    art.set("Date", format_date(two_hours_ago))
    art.finalize(gateway, 8)
    first_id = art.message_id
    assert await node.post(art) == "ok"
    assert art.message_id != first_id                      # signed again: a new Message-ID
    assert store.store.get(first_id) is None
    posted = store.store.get(art.message_id)
    assert posted is not None
    age = datetime.now(timezone.utc) - parse_date(art["Date"])
    assert age < timedelta(minutes=5)
    await node.shutdown()
    server.close()
