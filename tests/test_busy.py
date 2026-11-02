"""While the key ripens, the updater is told not to restart the gateway (unseenet#827)."""

import asyncio

from unseenet_gateway import cli


def test_the_busy_file_is_there_while_ripening_and_gone_after(tmp_path):
    path = tmp_path / "state" / "busy"

    class Node:
        async def ripen(self, key, to):
            await asyncio.sleep(0.05)
            assert path.exists()
    asyncio.run(cli.ripen_quietly(Node(), None, 3, path))
    assert not path.exists()
