"""unseenet-gateway: command line.

  unseenet-gateway -c gateway.toml keys          create the group key and the gateway key
  unseenet-gateway -c gateway.toml create-groups publish manifests (needs the group key)
  unseenet-gateway -c gateway.toml import-once   one pass: import, then export submissions
  unseenet-gateway -c gateway.toml run           a pass every `interval` seconds
  unseenet-gateway -c gateway.toml stats         what has been imported and skipped

Networks are configured as sections ([usenet], [nostr], [matrix], …); -n limits
a command to one. Network adapters may add commands of their own (nostr-key:
create the gateway's Nostr key, for export; matrix-login: save the token of
the gateway's Matrix account).

Passphrases come from UNSEENET_GATEWAY_PASSPHRASE (gateway key) and
UNSEENET_GATEWAY_GROUP_PASSPHRASE (group key), or are asked for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tomllib
from pathlib import Path

from unseenet.client.runtime import SERVICE_RIPEN_TO
from unseenet.core import crypto
from unseenet.core.groups import personal_inbox_id

from . import __version__
from .core import keys
from .core.adapter import Adapter
from .core.cooperation import Watcher
from .core.desk import JOIN, Desk
from .core.directory import DEFAULT_DIRECTORY, Announcer
from .core.importer import PASS_SECONDS, Importer
from .core.portal import Portal
from .core.registry import adapters
from .core.state import State
from .core.submissions import Exporter, inbox_manifest
from .core.files import attachments_field
from .core.unseenet_side import Node, gateway_manifest

log = logging.getLogger("unseenet_gateway")
ONLY: str | None = None          # -n
GATEWAY_PASS = ("UNSEENET_GATEWAY_PASSPHRASE", "Passphrase for the GATEWAY key: ")
GROUP_PASS = ("UNSEENET_GATEWAY_GROUP_PASSPHRASE", "Passphrase for the GROUP key: ")


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    base = Path(path).resolve().parent
    gw = cfg["gateway"]
    for k in ("state", "gateway_key", "group_key"):
        if k in gw:
            gw[k] = str((base / gw[k]).resolve())
    return cfg


def configured(cfg: dict, desk: Desk | None = None) -> list[Adapter]:
    """An adapter for each configured network ([usenet], [nostr], …), honouring -n."""
    out = []
    for name, cls in adapters().items():
        if name not in cfg or ONLY not in (None, name):
            continue
        try:
            ad = cls(cfg, desk)
        except TypeError:                 # an adapter from before the desk: cls(cfg)
            ad = cls(cfg)
            ad.desk = desk
        out.append(ad)
    return out


def make_desk(cfg: dict, state: State, node: Node, gateway) -> Desk | None:
    """The operator's desk (core/desk.py), when gateway.operator is a key and
    gateway.contact its inbox; `requests = false` turns it off."""
    gw = cfg["gateway"]
    if not gw.get("requests", True):
        return None
    if not str(gw.get("operator", "")).startswith("ed25519:") \
            or not str(gw.get("contact", "")).startswith("inbox@"):
        return None
    return Desk(state=state, node=node, gateway=gateway, operator=gw["operator"],
                contact=gw["contact"], pow_bits=int(gw.get("pow_bits", 18)))


def manifest_serial(state: State, ad: Adapter, group_id: str, bump: bool = False) -> int:
    """The serial for a group's next manifest: the configured one, or one more than the
    last this gateway published when it changes the manifest itself (a new peer)."""
    key = f"serial:{group_id}"
    base = int(ad.section.get("manifest_serial", 1))
    last = state.get_cursor("unseenet", key) or 0
    serial = max(base, last) + 1 if bump else max(base, last)   # bump: beyond what was out
    state.set_cursor("unseenet", key, serial)
    return serial


async def publish_manifest(cfg: dict, ad: Adapter, c, owner, gateway, node: Node,
                           state: State | None = None, bump: bool = False) -> str:
    """Publish one gatewayed group's manifest (and the submissions inbox if it exports)."""
    gw = cfg["gateway"]
    inbox = personal_inbox_id(gateway.public_key)
    peers = [k for k, _ in ad.desk.peers(c.group_id)] if ad.desk is not None else []
    serial = (manifest_serial(state, ad, c.group_id, bump) if state is not None
              else int(ad.section.get("manifest_serial", 1)))
    if c.direction == "both":
        await node.post(inbox_manifest(gateway))
    art = gateway_manifest(
        owner, gateway, c.name, network=ad.network, source=c.source,
        direction=c.direction, host=ad.host(),
        submissions=inbox if c.direction == "both" else None,
        operator=gw["operator"], contact=gw["contact"], abuse=gw.get("abuse", ""),
        policy=gw.get("policy", ""), title=c.title, description=c.description,
        view=c.view, kinds=c.kinds,
        # clients pay what the manifest says; for export that is the gateway's own
        # minimum (0023 5.2), so say it here or every submission would be refused
        min_pow=int(gw.get("export_min_pow", 20)) if c.direction == "both" else 0,
        peers=peers, requests=inbox,
        attachments=attachments_field(int(gw.get("pictures_max_size", 0))),
        serial=serial,
        retention_days=int(ad.section.get("retention_days", 90)),
        pow_bits=int(gw.get("pow_bits", 18)))
    return await node.post(art)


def cmd_keys(cfg: dict) -> None:
    gw = cfg["gateway"]
    g = keys.create(Path(gw["group_key"]), keys.passphrase(
        "UNSEENET_GATEWAY_GROUP_PASSPHRASE", "New passphrase for the GROUP key: "), "group")
    k = keys.create(Path(gw["gateway_key"]), keys.passphrase(
        "UNSEENET_GATEWAY_PASSPHRASE", "New passphrase for the GATEWAY key: "), "gateway")
    print(f"group key   {g.key_text}  ({gw['group_key']})")
    print(f"gateway key {k.key_text}  ({gw['gateway_key']})")
    print("\nPut the group key's public key in the config as gateway.group_owner,")
    print("and move the group key file off this host once the groups exist.")


def key_state(key_file: str) -> Path:
    """Where a key's public ripening chain is kept (UN-RFC 0025): beside its file."""
    return Path(f"{key_file}.state.json")


def make_node(cfg: dict, *, gateway, owner=None) -> Node:
    """The node, posting through the core's Session for our keys (gateway#19)."""
    gw = cfg["gateway"]
    node = Node(cfg["unseenet"]["node"], cfg["unseenet"].get("socks_port"))
    node.use_key(gateway, key_state(gw["gateway_key"]))
    if owner is not None:
        node.use_key(owner, key_state(gw["group_key"]))
    return node


async def ripen_quietly(node: Node, key, to: int) -> None:
    try:
        await node.ripen(key, to)
    except Exception as exc:  # noqa: BLE001 - posting ripens to level 0 by itself anyway
        log.warning("ripening stopped: %s", exc)


async def cmd_ripen(cfg: dict, to: int, group_key: bool) -> None:
    """Ripen the gateway key (and with --group-key the group key) to a level and
    publish its chain (UN-RFC 0025): once at setup, 7-11 minutes to level 0."""
    from unseenet.core import ripening as rp
    if not rp.available():
        sys.exit("key ripening needs chiavdf (pip install chiavdf)")
    gw = cfg["gateway"]
    gateway = keys.load(Path(gw["gateway_key"]), keys.passphrase(*GATEWAY_PASS))
    todo = [("gateway key", gateway)]
    owner = None
    if group_key:
        owner = keys.load(Path(gw["group_key"]), keys.passphrase(*GROUP_PASS))
        todo.append(("group key", owner))
    node = make_node(cfg, gateway=gateway, owner=owner)
    try:
        for label, key in todo:
            s = await node.session(key)
            await s.adopt_published_ripening()          # ripened elsewhere already?
            print(f"{label} {key.key_text}: level {s.ripening().level}", flush=True)
            while s.ripening().level is None or s.ripening().level < to:
                if not await s.ripen_segment():
                    break
                lv = s.ripening().level
                print(f"  t = {s.ripening().t:,}, level {lv}", flush=True)
                if lv is not None and lv != s.published_level():
                    await s.publish_ripening()
            if s.ripening().level != s.published_level():
                await s.publish_ripening()
            print(f"{label}: level {s.ripening().level}, published", flush=True)
    finally:
        await node.shutdown()


async def cmd_create_groups(cfg: dict) -> None:
    gw = cfg["gateway"]
    owner = keys.load(Path(gw["group_key"]), keys.passphrase(*GROUP_PASS))
    gateway = keys.load(Path(gw["gateway_key"]), keys.passphrase(*GATEWAY_PASS))
    if owner.key_text != gw["group_owner"]:
        sys.exit("group_owner in the config does not match the group key")
    node = make_node(cfg, gateway=gateway, owner=owner)
    state = State(Path(gw["state"]))
    ads = configured(cfg, make_desk(cfg, state, node, gateway))   # approved sources too
    if int(gw.get("pow_bits", 18)) < int(gw.get("export_min_pow", 20)) and any(
            c.direction == "both" for ad in ads for c in ad.carried()):
        sys.exit("pow_bits is below export_min_pow: the manifest would ask more postage "
                 "than the gateway pays for its own imports, and nodes would refuse them")
    for ad in ads:
        for c in ad.carried():
            if c.joined:
                print(f"{'joined':9} {c.group_id} (owned by another gateway)")
                continue
            print(f"{await publish_manifest(cfg, ad, c, owner, gateway, node, state):9} "
                  f"{c.group_id}")
    await node.shutdown()


def make_portals(cfg: dict, gateway, state: State, node: Node, feeds: list) -> list[Portal]:
    """Portals ([[portals]]): Unseenet groups with a counterpart on another network.

    [[portals]]
    group = "test.portal@<fingerprint>"   # the Unseenet group ID
    network = "nostr"
    relay = "wss://nostr.unseenet.org"    # as in the manifest's portal source
    connect = "ws://10.0.0.5:2929"        # optional: how the gateway reaches it
    id = "test-portal"                    # the NIP-29 group id
    """
    gw, out, known = cfg["gateway"], [], adapters()
    pow_bits = int(gw.get("pow_bits", 18))
    by_net: dict[str, Adapter] = {}
    for p in cfg.get("portals", []):
        net = p.get("network", "nostr")
        if ONLY not in (None, net):
            continue
        cls = known.get(net)
        if cls is None or not cls.capabilities.portals:
            log.warning("portal %s: no adapter for %s with portals", p.get("group"), net)
            continue
        ad = by_net.setdefault(net, cls(cfg))
        out.append(Portal(
            state=state, node=node, gateway=gateway,
            counterpart=ad.counterpart(p, state=state, feeds=feeds),
            group_id=p["group"], pow_bits=pow_bits,
            delay_max=float(p.get("delay_max", gw.get("export_delay_max", 600))),
            pictures_max=int(gw.get("pictures_max_size", 0))))
    return out


def make_exporter(cfg: dict, ads: list[Adapter], gateway, state: State, node: Node,
                  feeds: list):
    """An Exporter for every gatewayed group with direction "both", or None."""
    gw, targets = cfg["gateway"], {}
    for ad in ads:
        if ad.capabilities.export:
            for gid, send in ad.export_targets(state=state, feeds=feeds).items():
                targets[gid] = (ad.network, send)
    if not targets:
        return None
    from .core.reactions_out import ReactionMirror
    exporter = Exporter(state=state, node=node, gateway=gateway, targets=targets,
                        min_pow=int(gw.get("export_min_pow", 20)),
                        per_key_per_hour=int(gw.get("export_per_hour", 10)),
                        delay_max=float(gw.get("export_delay_max", 600)))
    # reactions going out: that Unseenet readers reacted, not how many (UN-RFC 0048 9)
    exporter.reactions = ReactionMirror(state=state, node=node, gateway=gateway, targets=targets)
    return exporter


async def _close(task) -> None:
    if isinstance(task, Watcher):
        return
    try:
        await (task.feed.close() if isinstance(task, Importer) else task.close())
    except Exception:  # noqa: BLE001
        pass


async def keep_inbox(state: State, node: Node, gateway, pow_bits: int,
                     every: int = 7 * 86400) -> None:
    """The gateway's personal inbox must exist: submissions (UN-RFC 0009 section 3), the
    operator's answers and join requests (UN-RFC 0038 3.2) arrive there. A gateway that
    joins another's group never publishes a group manifest, so it is kept here."""
    import time
    key = "inbox-manifest-at"
    if time.time() - (state.get_cursor("unseenet", key) or 0) < every:
        return
    await node.post(inbox_manifest(gateway, pow_bits))
    state.set_cursor("unseenet", key, int(time.time()))
    log.info("published the gateway's inbox manifest")


async def cmd_import(cfg: dict, loop: bool) -> None:
    gw = cfg["gateway"]
    gateway = keys.load(Path(gw["gateway_key"]), keys.passphrase(*GATEWAY_PASS))
    node = make_node(cfg, gateway=gateway)
    state = State(Path(gw["state"]))
    pow_bits = int(gw.get("pow_bits", 18))
    desk = make_desk(cfg, state, node, gateway)
    owner = None
    if desk is not None and gw.get("auto_create"):
        # approved sources get their group at once: the group key must be on this host
        owner = keys.load(Path(gw["group_key"]), keys.passphrase(*GROUP_PASS))
        if owner.key_text != gw["group_owner"]:
            sys.exit("group_owner in the config does not match the group key")
        node.use_key(owner, key_state(gw["group_key"]))
    if loop:                           # on to level 3 (no surcharge), low priority (UN-RFC 0025)
        ripening = asyncio.create_task(ripen_quietly(node, gateway, SERVICE_RIPEN_TO))

    seen: dict = {}                    # the Watcher's views, kept across rebuilds

    def build():
        ads = configured(cfg, desk)
        feeds = [f for ad in ads
                 for f in ad.feeds(state=state, node=node, gateway=gateway, pow_bits=pow_bits)]
        # UN-RFC 0038: read the groups served, so several gateways can share them
        watcher = Watcher(state=state, node=node, gateway=gateway,
                          groups=[c.group_id for ad in ads for c in ad.carried()],
                          joined=[c.group_id for ad in ads for c in ad.carried() if c.joined])
        watcher.views.update({g: v for g, v in seen.items() if g in watcher.views})
        seen.update(watcher.views)
        tasks: list = [watcher]
        pictures = int(gw.get("pictures_max_size", 0))     # gateway#26
        tasks += [Importer(feed=f, state=state, node=node, gateway=gateway,
                           pow_bits=pow_bits, delay_for=watcher.delay, pictures_max=pictures,
                           pass_seconds=float(gw.get("pass_seconds", PASS_SECONDS)))
                  for f in feeds]
        tasks += make_portals(cfg, gateway, state, node, feeds)
        if gw.get("directory", DEFAULT_DIRECTORY):       # "" turns announcing off
            tasks.append(Announcer(cfg=cfg, ads=ads, state=state, node=node, gateway=gateway,
                                   pow_bits=pow_bits))
        exporter = make_exporter(cfg, ads, gateway, state, node, feeds)
        if exporter is not None:
            exporter.watcher = watcher
        return ads, feeds, tasks, exporter

    ads, feeds, tasks, exporter = build()
    while True:
        if desk is not None or exporter is not None:
            try:
                await keep_inbox(state, node, gateway, pow_bits)
            except Exception as exc:  # noqa: BLE001
                log.warning("inbox manifest not published: %s", exc)
        for task in tasks:
            try:
                st = await task.run()
                log.info("%s pass done: %s", task.label, st)
            except Exception as exc:  # noqa: BLE001  (one network failing must not stop the others)
                log.warning("%s pass failed: %s", task.label, exc)
                await _close(task)
                await node.close()
        if exporter is not None:
            try:
                log.info("export pass done: %s", await exporter.run())
                if getattr(exporter, "reactions", None) is not None:
                    log.info("reactions out: %s", await exporter.reactions.run())
            except Exception as exc:  # noqa: BLE001
                log.warning("export pass failed: %s", exc)
                await node.close()
        if desk is not None:
            try:
                if await desk_pass(cfg, desk, ads, feeds, owner, gateway, node, state,
                                   tasks[0]):
                    for task in tasks:
                        await _close(task)
                    ads, feeds, tasks, exporter = build()     # carry what was approved
            except Exception as exc:  # noqa: BLE001
                log.warning("request pass failed: %s", exc)
                await node.close()
        if not loop:
            break
        await asyncio.sleep(int(gw.get("interval", 60)))
    for task in tasks:
        await _close(task)
    await node.shutdown()


async def desk_pass(cfg: dict, desk: Desk, ads: list[Adapter], feeds: list, owner, gateway,
                    node: Node, state: State, watcher: Watcher) -> bool:
    """New requests to the operator, their answers, and gateways joining one another's
    groups (UN-RFC 0038 3.2). True if what is carried changed, so the CLI rebuilds."""
    for ad in ads:
        await ad.handle_requests(desk, feeds)
    await ask_to_join(desk, ads, watcher)
    rebuild = False
    decided = await desk.poll()
    for jr in desk.joins:                         # another gateway asks to serve ours too
        await consider_join(cfg, desk, ads, feeds, jr)
    for req in decided:
        if req.status != "approved":
            continue                       # the adapter declines on the other side next pass
        if req.network == JOIN:
            rebuild |= await admit_peer(cfg, desk, ads, owner, gateway, node, state, req)
            continue
        ad = next((a for a in ads if a.network == req.network), None)
        c = next((c for c in ad.carried() if c.source == req.source), None) if ad else None
        if c is None:
            continue
        if c.joined:
            rebuild = True                         # serve another gateway's group: ask to join
            continue
        if owner is None:
            log.warning("%s %s approved: run create-groups to publish %s (or set "
                        "gateway.auto_create)", req.network, req.source, c.name)
            continue
        log.info("approved %s %s: %s %s", req.network, req.source, c.name,
                 await publish_manifest(cfg, ad, c, owner, gateway, node, state))
        rebuild = True
    return rebuild


async def ask_to_join(desk: Desk, ads: list[Adapter], watcher: Watcher) -> None:
    """For groups this gateway helps serve but is not yet a gateway of: ask their owner."""
    for ad in ads:
        for c in ad.carried():
            view = watcher.views.get(c.group_id)
            if not c.joined or view is None or view.manifest is None \
                    or watcher.admitted(c.group_id):
                continue
            gw_obj = view.manifest.raw.get("gateway") or {}
            to = gw_obj.get("requests") or view.manifest.raw.get("submissions")
            key = next((k for k in view.gateways if to and to.endswith("@" + crypto.fingerprint(k))),
                       None)
            if not to or key is None:
                log.warning("%s: its manifest says nowhere to ask to join", c.group_id)
                continue
            await desk.request_join(group=c.group_id, to_inbox=to, to_key=key,
                                    network=ad.network, source=c.source, account=ad.host())


async def consider_join(cfg: dict, desk: Desk, ads: list[Adapter], feeds: list, jr) -> None:
    """A join request for a group this gateway publishes: ask the operator (once)."""
    owner_fp = cfg["gateway"].get("group_owner", "")
    ad = next((a for a in ads if a.network == jr.network), None)
    c = next((c for c in ad.carried() if c.group_id == jr.group), None) if ad else None
    if c is None or c.joined or not owner_fp.startswith("ed25519:") \
            or jr.group.rpartition("@")[2] != crypto.fingerprint(crypto.key_from_text(owner_fp)):
        log.info("join request for %s from %s: not a group this gateway publishes",
                 jr.group, jr.sender[:20])
        return
    member = await ad.is_member(feeds, c.source, jr.account)
    seen = {None: "cannot be checked", True: "yes", False: "NO - it has not been invited"}[member]
    detail = (f"Another gateway asks to help serve {jr.group}.\n"
              f"Gateway key: {jr.sender}\nIts {jr.network} account: {jr.account}\n"
              f"In {c.source}: {seen}\n"
              + (f"{ad.consent_note}\n" if member is None and ad.consent_note else "")
              + f"Its operator: {jr.operator}\nContact: {jr.contact}\n\n"
              "If approved, it is added to the group's gateways in a new manifest; the "
              "gateways then take turns (UN-RFC 0038).")
    await desk.ask(JOIN, f"{jr.group}|{jr.sender}", title=f"{jr.group} (another gateway)",
                   detail=detail, name=jr.account)


async def admit_peer(cfg: dict, desk: Desk, ads: list[Adapter], owner, gateway, node: Node,
                     state: State, req) -> bool:
    group, _, key = req.source.partition("|")
    if owner is None:
        log.warning("gateway %s approved for %s: needs gateway.auto_create to publish the "
                    "new manifest here", key[:20], group)
        return False
    for ad in ads:
        c = next((c for c in ad.carried() if c.group_id == group and not c.joined), None)
        if c is None:
            continue
        desk.add_peer(group, key, req.name or "")
        log.info("admitted %s to %s: %s", key[:20], group,
                 await publish_manifest(cfg, ad, c, owner, gateway, node, state, bump=True))
        return True
    return False


def main() -> None:
    known = adapters()
    extra = {name: cls for cls in known.values() for name in cls.commands}
    p = argparse.ArgumentParser(prog="unseenet-gateway", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="gateway.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-n", "--network", choices=list(known),
                   help="only this network (create-groups, import-once, run)")
    p.add_argument("--version", action="version", version=f"unseenet-gateway {__version__}")
    p.add_argument("command", choices=["keys", "ripen", "create-groups", "import-once", "run",
                                       "stats", *extra])
    p.add_argument("--to", type=int, default=0, choices=range(5),
                   help="ripen: the level to reach (0: enough to post; 3: no surcharge)")
    p.add_argument("--group-key", action="store_true",
                   help="ripen: the group key too (when it is on this host)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose or a.command == "run" else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    global ONLY
    ONLY = a.network
    if a.command == "keys":
        cmd_keys(cfg)
    elif a.command in extra:
        extra[a.command](cfg).run_command(a.command)
    elif a.command == "ripen":
        asyncio.run(cmd_ripen(cfg, a.to, a.group_key))
    elif a.command == "create-groups":
        asyncio.run(cmd_create_groups(cfg))
    elif a.command in ("import-once", "run"):
        asyncio.run(cmd_import(cfg, a.command == "run"))
    elif a.command == "stats":
        print(json.dumps(State(Path(cfg["gateway"]["state"])).stats(), indent=2))


if __name__ == "__main__":
    main()
