"""The contract between the gateway core and one network.

The core does what UN-RFC 0023 asks of every gateway, whatever the network:
the connection to Unseenet, manifests, signing and postage, duplicates,
cancels, the export queue and portals. An adapter does only what its own
network needs, as its profile (UN-RFC 0023 section 9) specifies: reach the
network, read and send messages, and convert them.

This is an API of this implementation, not part of the UN-RFCs. A gateway
in another language is just as correct if it behaves as the RFCs say.

Writing an adapter for a new network:

1. Subclass :class:`Adapter`, set ``network`` (its hierarchy, reserved in
   UN-RFC 0023 section 9) and ``capabilities``.
2. Implement :meth:`Adapter.carried` (which foreign groups become which
   Unseenet groups) and :meth:`Adapter.feeds` (one :class:`Feed` per
   connection to the network).
3. A :class:`Feed` yields :class:`Incoming` messages from :meth:`Feed.fetch`
   and converts them with :meth:`Feed.convert`; everything else is done by
   the core (``core/importer.py``).
4. Register it as an entry point in the ``unseenet_gateway.adapters`` group::

       [project.entry-points."unseenet_gateway.adapters"]
       matrix = "unseenet_gateway_matrix:MatrixAdapter"

   The adapter then reads its own section (``[matrix]``) of ``gateway.toml``,
   and ``-n matrix`` works without any change to this package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from unseenet.core import crypto
from unseenet.core.article import Article
from unseenet.core.groups import make_group_id


def is_text(art: Article) -> bool:
    """Plain text, or Unseenet's Markdown (UN-RFC 0031): what a gateway may send out.
    Markdown goes out as its source text, which reads well as plain text; an adapter
    whose network shows formatting may render it as well."""
    from unseenet.core.markup import is_markdown
    ct = art.get("Content-Type", "text/plain")
    return ct.startswith("text/plain") or is_markdown(ct)


class Skip(Exception):
    """A foreign message is not imported. ``reason`` is a short, loggable word."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class ExportError(Exception):
    """The other network refused a message the gateway tried to send."""


@dataclass(frozen=True)
class Capabilities:
    export: bool = False         # can send Unseenet articles to the network (0023 section 5)
    retractions: bool = False    # the network deletes messages; imports are cancelled
    portals: bool = False        # Unseenet groups can get a counterpart there (0023 3.5)


@dataclass(frozen=True)
class Carried:
    """One foreign group this gateway carries into Unseenet (0023 sections 3.1, 3.2)."""
    source: str          # the manifest's `gateway.source`
    name: str            # the Unseenet group name, under the network's hierarchy
    group_id: str
    direction: str       # "import" or "both"
    title: str
    description: str
    view: str = ""               # UN-RFC 0008: "chat" for chat networks; "" = default
    kinds: tuple[str, ...] = ()  # UN-RFC 0028 4.1: e.g. ("chat",); () = both
    joined: bool = False         # a group owned by another gateway that this one helps serve


@dataclass(frozen=True)
class Source:
    """One foreign group a feed reads in a pass."""
    foreign: str         # the group's name or id in the other network
    group_id: str        # the Unseenet group it is imported into
    # the emoji the group takes, for imported reactions (UN-RFC 0048 section 9, #45):
    # set by the importer from the group's manifest; None takes any single emoji
    takes: Callable | None = field(default=None, compare=False, hash=False)


@dataclass
class Incoming:
    """One message read from the other network, not yet converted.

    ``foreign_id`` is what the gateway remembers it by (for duplicates and
    threading); None when it could not be read, which ``convert`` then skips.
    """
    foreign_id: str | None
    payload: Any
    label: str = ""          # for the log, e.g. an article number
    when: float | None = None   # the other network's time of the message (epoch seconds)


@dataclass(frozen=True)
class Retraction:
    """An imported message the other network no longer has: cancel it in Unseenet."""
    foreign_id: str
    unseenet_id: str
    reason: str


class Feed:
    """One connection to the other network that imports a set of groups.

    ``fetch`` is an async generator: code after a ``yield`` runs once the core
    has finished with that message, so a feed can move its cursor there.
    """

    network: str = ""
    label: str = ""

    def sources(self) -> list[Source]:
        raise NotImplementedError

    def fetch(self, source: Source) -> AsyncIterator[Incoming]:
        raise NotImplementedError

    def convert(self, source: Source, item: Incoming) -> Article:
        """The unsigned Unseenet article for ``item``; raise :class:`Skip` to leave it out."""
        raise NotImplementedError

    async def media(self, source: Source, item: Incoming) -> list:
        """The files (``core.files.Media``) the message carries, downloaded; [] by
        default. Called only when the gateway carries pictures (gateway#26)."""
        return []

    def imported(self, item: Incoming, art: Article) -> None:
        """Called after ``item`` was posted to Unseenet as ``art``."""

    def hold(self, source: Source, when: float) -> None:
        """Read again from ``when`` (epoch seconds) on the next pass: messages from then
        on were left for later (UN-RFC 0038 section 4). A feed that cannot does nothing,
        and then its messages are imported without waiting their turn."""

    async def retractions(self, source: Source) -> list[Retraction]:
        """Imported messages to cancel after this pass over ``source``."""
        return []

    async def close(self) -> None:
        pass


class Counterpart:
    """The other network's side of a portal (0023 section 3.5)."""

    network: str = ""
    source: str = ""             # as in the manifest's portal entry
    feed: Feed                   # imports what is written in the counterpart

    async def ensure(self, manifest) -> None:
        """Create the counterpart if needed and bring its metadata up to date."""
        raise NotImplementedError

    async def send(self, art: Article) -> str:
        """Send ``art``; return its id in the other network, or raise ExportError."""
        raise NotImplementedError


Send = Callable[[Article], Awaitable[str]]


class Adapter:
    """One network. Reads its own section of the configuration."""

    network: ClassVar[str] = ""
    capabilities: ClassVar[Capabilities] = Capabilities()
    # extra CLI commands: name -> one line of help; run by run_command(name)
    commands: ClassVar[dict[str, str]] = {}

    def __init__(self, cfg: dict, desk=None):
        self.cfg = cfg
        self.gw = cfg["gateway"]
        self.section: dict = cfg.get(self.network, {})
        self.desk = desk          # core/desk.py: requests decided by the operator, if any

    # --- naming ---
    @property
    def prefix(self) -> str:
        """The hierarchy (`test.<network>` on test nodes, so groups expire quickly)."""
        return self.section.get("prefix", self.network)

    def group_id(self, name: str) -> str:
        """Group ID of a gatewayed group: owned by the configured group key."""
        return make_group_id(name, crypto.key_from_text(self.gw["group_owner"]))

    def host(self) -> str:
        """The manifest's `gateway.host` for this network's groups."""
        return self.section.get("host", self.gw.get("host", ""))

    # --- what the network implements ---
    def carried(self) -> list[Carried]:
        raise NotImplementedError

    def feeds(self, *, state, node, gateway: crypto.Identity, pow_bits: int) -> list[Feed]:
        raise NotImplementedError

    def export_targets(self, *, state, feeds: list[Feed]) -> dict[str, Send]:
        """Unseenet Group ID -> send, for groups with direction "both".

        ``send`` is best a bound method of a sender object. If that object also has
        ``async retract(art, foreign_id) -> bool``, the exporter calls it when the node
        refuses the approved copy after the other network took the message, so it does
        not stay there alone (#18); it should take back only what this gateway sent."""
        return {}

    def counterpart(self, entry: dict, *, state, feeds: list[Feed]) -> Counterpart:
        """The counterpart for one [[portals]] entry of this network."""
        raise NotImplementedError

    consent_note = ""          # said in a join request when is_member cannot tell

    async def is_member(self, feeds: list[Feed], source: str, account: str) -> bool | None:
        """Whether another gateway's ``account`` takes part in ``source`` (its consent
        there, UN-RFC 0038 3.2); None if this network cannot tell."""
        return None

    async def handle_requests(self, desk, feeds: list[Feed]) -> None:
        """Turn requests from the other network into questions to the operator, and
        answer the other side once the operator has decided (core/desk.py). A network
        without a way to ask does nothing here."""

    def run_command(self, name: str) -> None:
        raise NotImplementedError(name)
