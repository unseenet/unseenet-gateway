# Unseenet gateway

Gateways between [Unseenet](https://unseenet.org) and other networks, as
specified in [UN-RFC 0023](https://github.com/unseenet/unseenet/blob/main/rfc/un-rfc-0023.md)
(what every gateway shares) and one profile per network.

The gateway is a generic core with one **adapter** per network. The core does
everything Unseenet's rules ask of every gateway; an adapter only reaches its
network and converts messages. Three adapters come with it, and they are also
the reference for writing new ones:

| Adapter  | Network               | Hierarchy  | Profile     | Status in the lab               |
|----------|-----------------------|------------|-------------|---------------------------------|
| `usenet` | Usenet, through INN   | `usenet.*` | UN-RFC 0024 | import                          |
| `nostr`  | Nostr groups (NIP-29) | `nostr.*`  | UN-RFC 0026 | import, export, portals         |
| `matrix` | Matrix rooms          | `matrix.*` | UN-RFC 0037 (draft) | import, export, portals |

Adapters for other networks – FidoNet, IRC, XMPP, the fediverse and
others whose names UN-RFC 0023 section 9 reserves – are meant to be easy to
add, also by others and in their own repositories, without any change to
this one. See [Writing an adapter](#writing-an-adapter).

> **Pre-alpha.** Everything above runs against a lab setup only. Export to
> Usenet is not implemented yet. Do not point this at a real Usenet peer
> without an agreement with its administrators.

## How it works

Groups from another network live in Unseenet under a top-level hierarchy named
after that network: the Usenet group `comp.misc` becomes `usenet.comp.misc`.
Each is an ordinary **moderated** group whose moderator is the gateway, so
nodes and clients carry it without knowing anything about gateways.

```
 Usenet peers ──NNTP──▶ INN (on the gateway host) ──▶ unseenet-gateway ──Tor──▶ Unseenet node
```

The gateway does not talk to Usenet itself. A local news server (INN) does
the peering, history, `Path` and spam filtering, and the gateway reads new
articles from it like a newsreader. Each article is converted (UN-RFC 0024), signed with the gateway key, given its postage and posted to an
Unseenet node over Tor.

```
src/unseenet_gateway/
  core/      shared by all networks: the adapter contract, the import loop,
             export queue, portals, keys, state, Origin-* headers, the Unseenet side
  usenet/    Usenet adapter: conversion, reading from INN
  nostr/     Nostr adapter: NIP-29 groups, BIP-340 signatures, export, portals
  matrix/    Matrix adapter: public rooms through a bot account, export
```

The core does what UN-RFC 0023 asks of every gateway; an adapter does only
what its network needs, as its profile specifies (UN-RFC 0023 section 9).
The core knows no network by name: adapters are found through the
`unseenet_gateway.adapters` entry point group, and each reads its own section
of `gateway.toml`. See [Writing an adapter](#writing-an-adapter).

**Nostr.** The adapter reads public NIP-29 groups from a relay. It checks that
the group's metadata is signed by the relay and that the group is not private,
drops events whose id or signature do not verify, and imports threads (kind
11), comments (kind 1111) and chat (kind 9) into `nostr.<group-id>`. The
original signed event goes into `Origin-Proof`, so a reader can check that the
gateway did not change it. Messages the group deletes are cancelled in
Unseenet. Signatures are pure Python; `pip install unseenet-gateway[fast]`
adds coincurve for speed.

Every imported article carries the shared `Origin-Network`, `Origin-ID`,
`Origin-Date` and `Origin-From` headers (UN-RFC 0023 section 4.3), so a
client can show gatewayed articles from any network the same way. Headers that
only make sense for one network keep its prefix (`Usenet-Newsgroups`, …).

**Matrix.** The adapter reads public, unencrypted Matrix rooms through one
ordinary bot account, over the Client-Server API (no SDK needed). It joins a
room only when the room's moderators invite it, and declines the invitation
with a reason if the room is encrypted or not public; it stops reading a
room as soon as it is kicked. Messages become chat lines in
`matrix.<homeserver>.<alias>` (`#python:matrix.org` →
`matrix.matrix-org.python`), with replies and threads as `References`, edits
as new articles referring to the old one, and redactions as cancels. Images
and files are left out. With `direction = "both"` the bot also writes
submissions from Unseenet into the room, as replies where they answer
something, naming the Unseenet author and key. The gateway never sends
typing notifications, read receipts or presence. Portals work too: the bot
creates a public room on its own homeserver for an Unseenet group whose owner
opened one, with optional human admins. Virtual users per Unseenet key (an
Application Service) are a later step.

Planned networks: FidoNet (`fido.*`).

What a gateway never does: learn or record anything about who wrote an
Unseenet post or where it came from. It can stop a key; it cannot identify a
person (UN-RFC 0023 section 6.2).

## Setup

Requires Python 3.11+, Tor, and INN carrying the groups you want.

```
python3 -m venv .venv
.venv/bin/pip install unseenet-gateway
cp gateway.example.toml gateway.toml          # and edit it

unseenet-gateway -c gateway.toml keys            # creates the group key and the gateway key
# put the printed group key in gateway.group_owner
unseenet-gateway -c gateway.toml ripen --group-key   # once: 7-11 minutes per key, see below
unseenet-gateway -c gateway.toml create-groups   # publishes one manifest per group
unseenet-gateway -c gateway.toml import-once -v
unseenet-gateway -c gateway.toml run             # every `interval` seconds
unseenet-gateway -c gateway.toml stats
unseenet-gateway -c gateway.toml -n nostr import-once -v   # one network only
unseenet-gateway -c gateway.toml -n matrix matrix-login    # Matrix: save the bot's token
```

Passphrases are read from `UNSEENET_GATEWAY_PASSPHRASE` (gateway key) and
`UNSEENET_GATEWAY_GROUP_PASSPHRASE` (group key), or asked for. After
`create-groups`, move the group key file off the gateway host: it is only
needed again to change a group's manifest.

**Key ripening** (UN-RFC 0025). Nodes take articles only from keys that have
done a few minutes of work, which keeps throwaway keys from spamming. The
gateway posts through the unseenet core, so ripening, the `Key-Ripening`
header and postage are the same as in every client. `ripen` does the work
for the gateway key (and with `--group-key` the group key) once at setup and
publishes it: about 7-11 minutes each, one core. The chains are kept beside
the key files as `<key file>.state.json` (public, nothing secret). While
`run` runs, the gateway key ripens on to level 3, where it pays no extra
postage: a few days on one core at low priority. The group key stays at
level 0; it only signs manifests now and then. If a key was ripened and
published elsewhere, the gateway takes its chain over instead of starting again.

## Requests from other networks

A source can ask to be gatewayed. On Matrix, a room asks by inviting the
gateway's account; a room that is not in the configuration then becomes a
**request in the operator's own Unseenet inbox** (`gateway.contact`, the personal
inbox of `gateway.operator`): what the room is, who invited, and the group name
it would get. The operator answers the post from any Unseenet client:

```
approve                 gateway it under the proposed name
approve matrix.x.name   ... under another name
decline <reason>        the room's invitation is declined with this reason
```

Only answers signed by the operator key count. With `auto_create = true` the
gateway publishes the approved group itself (the group key must then be on the
gateway host) and joins the room within a pass; otherwise approval only marks
it, and the operator runs `create-groups`. Spaces, private and encrypted rooms
are declined without asking. Each room is asked about once.

## Several gateways for one group

A gatewayed group can have several gateways (UN-RFC 0038), so it does not
depend on one host. They share the work by turns: each takes a rank from its
key, the first imports and exports at once, the next fills in what is still
missing after `stagger` seconds (300 unless the manifest says otherwise), and
so on. Nothing is sent twice: a gateway skips messages another has imported
and submissions another has exported. If a gateway stops, the others go on.

People decide who takes part. To help serve a group another gateway runs:

1. The room invites this gateway's account too.
2. The operator approves the request with the group's full Group ID instead of
   a name (`approve matrix.matrix-org.room@<fingerprint>`), or configures the
   room with `rooms = [{room = "#room:server", group = "<Group ID>"}]`.
3. This gateway sends a join request to the owning gateway, whose operator gets
   it in their inbox, with whether the new account is in the room.
   `approve` there publishes a new manifest with both gateways
   (the owner needs `auto_create`).

Until the manifest lists it, the joining gateway writes nothing in the group.
Every gateway announces itself in the directory `unseenet.gateways` (name,
operator, accounts to invite, groups served); a group stays active while one of
its gateways announces it.

## Writing an adapter

Usenet, Nostr and Matrix are reference adapters, not the limit: an adapter for
another network can be written by anyone and live in its own package;
nothing here has to change. Usenet shows a network reached through a local
server and read by article number; Nostr shows one with several relays,
signed messages, deletions, export and portals; Matrix one reached through
an HTTP API with an account, where the other side's consent is an
invitation. The contract is in `src/unseenet_gateway/core/adapter.py`, and the
smallest complete example is the loopback adapter in
`tests/test_adapter_core.py`. In short:

- a subclass of `Adapter` with `network` (its hierarchy, reserved in
  UN-RFC 0023 section 9) and `capabilities` (export, retractions, portals);
- `carried()`: which foreign groups become which Unseenet groups, for the
  manifests;
- `feeds()`: one `Feed` per connection. A feed yields `Incoming` messages
  from `fetch()` and converts each with `convert()` (or raises `Skip`). The
  core skips duplicates, signs, pays postage, posts, threads and cancels;
- optionally `export_targets()` (a `send` per group) and `counterpart()`
  for portals;
- an entry point:

  ```toml
  [project.entry-points."unseenet_gateway.adapters"]
  matrix = "unseenet_gateway_matrix:MatrixAdapter"
  ```

The Python API belongs to this implementation, not to the UN-RFCs: they
specify what a gateway does, so a gateway written in another language is
just as correct. A new network also needs a profile UN-RFC and its hierarchy
reserved in the root list.

This is all the documentation for adapter authors so far: the contract in
`core/adapter.py`, this section and the two reference adapters. The API may
still change before Unseenet 1.0, and fuller documentation will follow once
it has settled.

## Development

```
python3 -m venv .venv
.venv/bin/pip install -e ../unseenet pytest   # a checkout of unseenet/unseenet
.venv/bin/pip install --no-deps -e .
.venv/bin/pytest
```

The tests use synthetic articles only.

## Versions and releases

Versions follow [semantic versioning](https://semver.org/) on the same
principles as [unseenet](https://github.com/unseenet/unseenet/blob/main/docs/development.md#versioning):
tags `vX.Y.Z` on `main`, the changes in [CHANGELOG.md](CHANGELOG.md), and
releases on [PyPI](https://pypi.org/p/unseenet-gateway). Each release names the
minor version of unseenet it works with (`unseenet>=0.5,<0.6`); a new minor
version of unseenet means a new release of the gateway.

To release: move the entries under `## [Unreleased]` in `CHANGELOG.md` to a
new `## [X.Y.Z] - YYYY-MM-DD` section, set `__version__` in
`src/unseenet_gateway/__init__.py`, commit, then
`git tag -a vX.Y.Z -m "unseenet-gateway X.Y.Z" && git push origin vX.Y.Z`.
`.github/workflows/release.yml` checks the tag, version and changelog, runs
the tests, publishes to PyPI (trusted publishing) and makes the GitHub release.

## Part of Unseenet

Unseenet is made in several repositories, each released on its own:

| Repository | What it is |
|---|---|
| [unseenet](https://github.com/unseenet/unseenet) | The protocol (UN-RFCs), the core library, the node and the command-line client |
| [unseenet-tui](https://github.com/unseenet/unseenet-tui) | The terminal client (moving here from unseenet, unseenet#548) |
| [unseenet-gui](https://github.com/unseenet/unseenet-gui) | The graphical client - in a window, booted, or in a browser |
| [unseenet-portable](https://github.com/unseenet/unseenet-portable) | Builds the portable clients, Terminal and Graphical, with Tor inside (unseenet#549) |
| [unseenet-bootable](https://github.com/unseenet/unseenet-bootable) | Builds the bootable images, Terminal and Graphical, and later a node image (unseenet#550) |
| [unseenet-docs](https://github.com/unseenet/unseenet-docs) | The site [unseenet.org](https://unseenet.org), gathering the docs of every repository (unseenet#547) |
| [unseenet-entrance](https://github.com/unseenet/unseenet-entrance) | Web entrances: the graphical client in an ordinary browser |
| [unseenet-gateway](https://github.com/unseenet/unseenet-gateway) | Gateways to other networks: Usenet, Nostr, Matrix |
| [unseenet-bots](https://github.com/unseenet/unseenet-bots) | Bots that keep channels filled with current content |
| [unseenet-helpbot](https://github.com/unseenet/unseenet-helpbot) | The bot that answers questions in `unseenet.help` |
| [unseenet-stats](https://github.com/unseenet/unseenet-stats) | Statistics for the network |
| [unseenet-mcp](https://github.com/unseenet/unseenet-mcp) | An MCP server, so a language model can read and post as you (planning) |
| [unseenet-demo](https://github.com/unseenet/unseenet-demo) | Demo people, feeds and sites, made with the command-line client |

## License

Apache-2.0, like the rest of Unseenet.
