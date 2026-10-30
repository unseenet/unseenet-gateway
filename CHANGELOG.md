# Changelog

All notable changes to unseenet-gateway are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/) on the same principles as
[unseenet](https://github.com/unseenet/unseenet/blob/main/docs/development.md#versioning).
The rules gateways follow are in the [UN-RFC series](https://github.com/unseenet/unseenet/tree/main/rfc):
UN-RFC 0023 for all gateways, 0024 for Usenet, 0026 for Nostr, 0038 for
several gateways per group.

## [Unreleased]

Needs unseenet 0.6.6 (`unseenet>=0.6.6,<0.7`), which shares.

### Added

- **Shares** (#66, UN-RFC 0060 section 9): out, a plain share of a post that is on Nostr
  too is a NIP-18 repost (kind 16 for group events, 6 for a kind 1 note, empty content)
  and a share with a comment a quote (`q` tag and a `nostr:note1…` link); otherwise, and
  to Matrix, the share's words with the shared post's `unseenet:` address - never the
  original's text. In, a Nostr repost of something the gateway carries is a plain share
  where the group takes them, a new thread quoting it a share with a comment, anything
  else an ordinary post with a `nostr:` link. Share notices are never carried. What a
  group takes is read from its manifest with its emoji.

## [0.4.0] - 2026-10-30

Needs unseenet 0.6.5 (`unseenet>=0.6.5,<0.7`), which makes the link cards; nodes need no update for it.

### Added

- **Link cards on imported messages** (#63, UN-RFC 0059 section 11; needs unseenet
  0.6.5): with `gateway.link_cards = true`, a message from Usenet,
  Nostr or Matrix with a link gets a card for its first link - the site's icon, domain,
  title and description, made over the gateway's Tor and signed with the message, so
  readers fetch nothing. Never the page's picture, whose licence the gateway cannot know.
  Off by default: every card is a fetch over Tor. Outgoing messages keep the plain link.

## [0.3.0] - 2026-10-29

Needs unseenet 0.6 (`unseenet>=0.6,<0.7`): update the nodes first.

- **Ripening to the level you choose** (#58, unseenet#740): the gateway key ripens in the
  background to `ripen_to` under `[gateway]`, else `UNSEENET_RIPEN_TO`, else level 3.
  Level 4 takes two to three weeks of one core, then every imported article costs about a
  quarter of the postage. The project's own gateway runs at 4.
- **A container image** (#39, unseenet/unseenet#571): `ghcr.io/unseenet/gateway` -
  the gateway, its adapters and Tor on the node image, config, keys and state in
  `/data`, passphrases as Docker secrets - and a `compose.yaml`. Built and tested on
  pull requests that change it, published for amd64 and arm64 only for release tags,
  through the project's shared workflow. See *In Docker* in the docs.

- **Raw 8-bit Usenet headers no longer stop the import** (#55): an article with an
  unencoded 8-bit `From:` could not be written (`surrogates not allowed`), and the error
  failed the whole pass, so the cursor never moved past it. Raw 8-bit text is read as
  UTF-8 when it is, else Windows-1252, and a message that cannot be converted is skipped
  (counted as `error`), never the pass.
- **A long import leaves export and requests their turn** (#51): the passes run one
  after another, and the lab's Usenet import (24 groups) took hours per pass - no
  submission went out and no join request reached the operator meanwhile. An import
  pass now starts no new group after `gateway.pass_seconds` (default 300; 0: no limit),
  and the next pass begins with the group after the last one done. Usenet, whose
  cursor moves per article, may also stop inside a group and go on there next pass.
- **Join requests and questions to the operator pay what the inbox asks** (#16): an inbox
  article was built with the desk's fixed postage, and a node that wanted more (an
  inbox's own minimum, a young key's) refused it every pass - `441 postage: need 24 bits`
  in the lab, so a second gateway could never ask to serve a Usenet group. It is now
  built again with the bits the node names, once.
- **Usenet with several gateways** (#16, UN-RFC 0038, UN-RFC 0024 6.1): tested - joining
  another gateway's group in `[usenet] groups`, and the same Usenet article imported once.
- **Pictures go out too** (#26, UN-RFC 0027 section 5.3): a submitted post with pictures
  is exported with them. The gateway fetches the encrypted files sent with the
  submission, decrypts them and checks that they are exactly the blobs the post names
  (refused otherwise, as a moderator's client does). On Matrix each picture is uploaded
  and sent as an `m.image` after the text, in the same thread, and taken back with it.
  On Nostr, which carries no files, the note says how many pictures are on Unseenet.
  The approved copy is posted with its plain files.

- **Imported reactions take the group's set** (#45): the importer reads each
  gatewayed group's reaction set from its manifest (kept an hour), and a Matrix or
  Nostr reaction with an emoji the group does not take is dropped, not changed
  (UN-RFC 0048 section 9).

- **Reactions go out** (#32, UN-RFC 0048 section 9): in groups that export, the
  gateway shows *that* Unseenet readers reacted, never how many - its own
  `m.reaction` / kind 7 with an emoji on a message from Matrix or Nostr while at
  least one reader gives it, taken back (redaction / deletion) with the last.
  Nothing for messages that did not come from that network; its own reactions
  never come back in.

- **Reactions come in** (#32, UN-RFC 0048 section 9): Matrix `m.reaction`
  annotations and Nostr kind 7 (`+`/empty as 👍, `-` as 👎) with one emoji, to
  imported messages, become reactions carrying `Origin-From`, so each outside
  person counts once; their redaction or deletion is a cancel. Other keys,
  emoji the group does not take and reactions to messages not imported are
  dropped.

- **Imports are posted with the time of posting** (#41, UN-RFC 0049): `Date` is
  set again just before an import is signed - fetching its pictures may take a
  while - so nodes that refuse a `POST` more than an hour old take it; the other
  network's date stays in `Origin-Date`, which the core now reads, shows and
  orders by, and history brought in late (Matrix backfill, a Usenet article
  fetched days later) is *old on arrival*: it notifies nobody.

## [0.2.0] - 2026-10-25

For unseenet 0.5 (`unseenet>=0.5,<0.6`); update the nodes first. Markdown and pictures come in from Matrix and NIP-29 groups, each network gets its default view, and several gateways can serve one Nostr or Usenet group.

### Added
- **Several gateways for Nostr and Usenet groups too** (#16, UN-RFC 0038), as
  for Matrix: `groups = [{id = ..., group = "...@<fingerprint>"}]` (Nostr) or
  `[{name = ..., group = ...}]` (Usenet) helps serve a group another gateway
  publishes. For Nostr, the other side's consent is the gateway's key on the
  NIP-29 group's member or admin list, shown to the owner's operator in the
  join request; Usenet has none, and the request says that admission rests on
  their approval alone. Before exporting to Nostr, a gateway looks whether
  another one sent the article already, and takes back only events it sent.
- **Pictures in from Matrix** (#26): an `m.image` message comes in as a chat
  line with its caption and the picture, downloaded through Matrix's
  authenticated media API, cleaned of metadata by the core as every client
  does, and posted as a file (UN-RFC 0027). Turned on with
  `gateway.pictures_max_size` (octets, e.g. `1048576`): the groups' manifests
  then allow pictures up to that size (`create-groups` publishes them), and
  nodes must carry files. Without it, with a core before 0.5, or for a picture
  that is too big, the message comes in with a line saying a picture was left
  out.
- **Pictures in from NIP-29 groups** (#26), with the same setting: the pictures a
  note links to - its `imeta` tags (NIP-92) or `https` links to picture files,
  at most four - are fetched **only over the gateway's Tor**, never from its own
  address, only from `https`, only pictures, at most 8 MiB, and posted as files
  as from Matrix. NIP-29 groups are moderated by their relay's admins, as
  Matrix rooms are; a note deleted there is cancelled here, and its pictures
  with it. Without Tor, nothing is fetched and a line says so. Pictures out to
  Matrix are still to come.
- **Markdown in** (#26): a Matrix message's `formatted_body` comes in as
  Unseenet Markdown (UN-RFC 0031) - emphasis, code, links, quotes, lists,
  headings - with every HTML tag turned into Markdown or dropped, links only
  to `http(s)`, `unseenet:` and `mailto:`, and the quoted reply (`<mx-reply>`)
  left out. A Nostr note written in Markdown (links, emphasis, code, lists)
  comes in as Markdown; other notes stay plain text, so a lone `*` is never
  emphasis. Usenet stays plain text.
- **Each network's default view** (#26): NIP-29 groups are shown as chat, as
  in Nostr clients; Usenet groups as threads; Matrix rooms as chat, as before.
  Readers can always switch. Run `unseenet-gateway create-groups` to give
  groups that exist already their new manifest.

## [0.1.0] - 2026-10-22

The first release. Works with unseenet 0.4 (`unseenet>=0.4.1,<0.5`); nodes
from 0.4.0 need the gateway's keys ripened once (`unseenet-gateway ripen`).

### Added

- A generic core with one adapter per network, found through the
  `unseenet_gateway.adapters` entry point, so other packages can add their
  own; groups from another network appear under its own hierarchy
  (`usenet.*`, `nostr.*`, `matrix.*`) with `Origin-*` headers
  (UN-RFC 0023).
- **Usenet:** import from an INN server (UN-RFC 0024).
- **Nostr:** import from NIP-29 groups on one or more relays, author names
  from profile relays, NIP-40 expiration, cancels for messages a relay no
  longer serves, reconnecting after a dropped connection (UN-RFC 0026).
- **Matrix:** import from rooms, Unseenet Markdown exported as formatted
  Matrix messages; spaces are never gatewayed, and a room where the gateway
  may not write does not stop the import.
- **Export:** posts from Unseenet go to the other network through a
  submission queue, after asking the group (UN-RFC 0023 section 5).
  When the node still refuses the approved copy (its own postage, a block
  in the hierarchy), the message is taken back on the other network: a
  redaction on Matrix, a NIP-09 deletion on Nostr, and only what this
  gateway sent (#18).
- **Portals:** an Unseenet group mirrored to Nostr or Matrix with the owner's
  consent, its counterpart named "<group> (Unseenet)".
- **Requests:** rooms that invite the gateway become requests the operator
  decides from their Unseenet inbox.
- **Several gateways per group:** turns, admission and a directory of
  gateways (UN-RFC 0038).
- The gateway's inbox manifest is published at start and weekly.
- `unseenet-gateway ripen [--to N] [--group-key]`: ripen the gateway key, and
  the group key when it is on this host (UN-RFC 0025).

### Changed

- Everything the gateway posts goes through the core's `Session`, so key
  ripening, the `Key-Ripening` header and postage are the same code as in
  every client (gateway#19).
- The repository is now
  [unseenet/unseenet-gateway](https://github.com/unseenet/unseenet-gateway)
  (the old address redirects).

[Unreleased]: https://github.com/unseenet/unseenet-gateway/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/unseenet/unseenet-gateway/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/unseenet/unseenet-gateway/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/unseenet/unseenet-gateway/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/unseenet/unseenet-gateway/releases/tag/v0.1.0
