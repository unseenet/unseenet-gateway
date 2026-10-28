"""Export, the Unseenet side (UN-RFC 0023 section 5), shared by all networks.

Submissions arrive in the gateway's inbox (UN-RFC 0009 section 3), sealed to
the gateway key. For each one the gateway checks it, hands it to the network
adapter after a random delay, and approves the original in Unseenet only
once the other network has accepted it.

The gateway learns nothing about authors beyond their key: submissions come
through Tor and a one-time key, and nothing here records how they arrived.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from dataclasses import dataclass

from unseenet.core import crypto
from unseenet.core.article import Article, ArticleError, validate_structure
from unseenet.core.groups import (Manifest, approve, check_permitted, make_manifest,
                                  open_inbox_article, personal_inbox_id)
from unseenet.nntp import NNTPError, strip_reader_headers

from .adapter import is_text
from .state import State
from .unseenet_side import Node

log = logging.getLogger(__name__)

MANIFEST_TTL = 300                # seconds a group's manifest is trusted between reads
SUBMISSION_TYPE = "message/unseenet"


class Refused(Exception):
    """A submission the gateway will not export; the reason is for the log."""


class NotYet(Exception):
    """A submission that cannot go out yet (its files are still on their way)."""


@dataclass(frozen=True)
class PlainFile:
    """A file a submitted post carries (UN-RFC 0027 section 5.3), decrypted and checked
    against the blob the post names: what the other network gets and the node stores."""
    blob: str
    octets: bytes
    type: str
    name: str = ""
    alt: str = ""
    dim: tuple | None = None


def takes_files(send) -> bool:
    """Whether an adapter's ``send`` can carry files (``send(art, files=...)``)."""
    try:
        return "files" in inspect.signature(send).parameters
    except (TypeError, ValueError):
        return False


@dataclass
class ExportStats:
    exported: int = 0
    refused: int = 0
    failed: int = 0
    waiting: int = 0
    unmatched: int = 0          # on the other network, but the node refused the copy
    retracted: int = 0          # the node refused the copy, so it was taken back there (#18)


def inbox_manifest(gateway: crypto.Identity, pow_bits: int = 18) -> Article:
    """The gateway's personal inbox, where submissions arrive."""
    return make_manifest(gateway, "inbox", "inbox", retention_days=30, pow_bits=pow_bits)


class Exporter:
    """``targets`` maps each Unseenet Group ID with export to (network, send),
    where ``send(art)`` is the adapter's coroutine returning the foreign id."""

    def __init__(self, *, state: State, node: Node, gateway: crypto.Identity,
                 targets: dict, min_pow: int = 20, per_key_per_hour: int = 10,
                 delay_max: float = 600.0, watcher=None):
        self.state, self.node, self.gateway = state, node, gateway
        self.targets = targets
        self.min_pow, self.per_key_per_hour = min_pow, per_key_per_hour
        self.delay_max = delay_max
        self.watcher = watcher          # core/cooperation.py, when other gateways may share
        self.inbox = personal_inbox_id(gateway.public_key)
        self._manifests: dict[str, tuple[Manifest | None, float]] = {}
        self._files: dict[str, tuple[list[dict], dict]] = {}   # Message-ID -> (shown, keys)
        self.state.db.execute("CREATE TABLE IF NOT EXISTS exports ("
                              "author TEXT NOT NULL, at INTEGER NOT NULL)")

    async def _submissions(self) -> list[tuple[int, Article]]:
        c = await self.node._conn()
        try:
            _, low, high = await c.group(self.inbox)
        except NNTPError:
            return []
        after = self.state.get_cursor("unseenet", self.inbox) or (low - 1)
        out = []
        files: dict[str, tuple[list[dict], dict]] = {}
        for n in range(max(after + 1, low), high + 1):
            raw = await c.article(str(n))
            if raw is None:
                continue
            try:
                outer = Article.from_bytes(strip_reader_headers(raw))
                inner, _sender = open_inbox_article(outer, self.gateway)
            except (ArticleError, ValueError):
                continue                # not for us, or not readable: nothing to do
            if not inner.get("Content-Type", "").startswith(SUBMISSION_TYPE):
                continue
            body = inner.body.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
            try:
                sub = Article.from_bytes(body)
                validate_structure(sub, raw_size=len(body))
            except ArticleError as exc:
                log.info("submission #%d unusable: %s", n, exc)
                continue
            if sub.get("Blobs") and outer.get("Blobs"):     # files sent with it (0027 5.3)
                from unseenet.core import blobs as bl
                files[sub.message_id] = bl.inner_attachments(outer, inner.headers)
            out.append((n, sub))
        self._files = files
        return out

    async def _plain_files(self, art: Article) -> list[PlainFile]:
        """The files a submitted post names, as sent with the submission: fetched from
        the node, decrypted with their keys and checked to be exactly the blobs the post
        names (as a moderator's client does, UN-RFC 0027 section 5.3). ``Refused`` when
        they are not; ``NotYet`` while the node is still fetching one."""
        from unseenet.core import blobs as bl
        try:
            named = dict(bl.parse_blobs(art))
        except ArticleError as exc:
            raise Refused(f"it names its files wrongly: {exc}") from exc
        if not named:
            return []
        shown, keys = self._files.get(art.message_id, ([], {}))
        if not keys:
            raise Refused("its files did not come with it")
        c = await self.node._conn()
        plain: dict[str, bytes] = {}
        for a in shown:
            try:
                cipher = await c.blob(a["blob"])
            except NNTPError as exc:
                if exc.code == 436:
                    raise NotYet("the node is still fetching a file") from exc
                raise Refused(f"its file could not be had: {exc}") from exc
            if cipher is None:
                raise Refused("its files are not on the node")
            fits = getattr(bl, "fits_padding", None)     # not in every released core
            if bl.blob_id(cipher) != a["blob"] or (fits and not fits(len(cipher), a["size"])):
                raise Refused("the node sent another file than the submission names")
            try:
                octets = bl.decrypt_file(keys[a["blob"]], cipher, a["size"])
            except (KeyError, ValueError) as exc:
                raise Refused("a file does not open with its key") from exc
            plain[bl.blob_id(octets)] = octets
        if set(plain) != set(named) or any(len(plain[b]) != n for b, n in named.items()):
            raise Refused("the files sent with it are not the ones it names")
        out = []
        for a in bl.shown_attachments(art.headers):
            if a["blob"] in plain:
                out.append(PlainFile(a["blob"], plain.pop(a["blob"]), a["type"],
                                     a.get("name") or "", a.get("alt") or "", a.get("dim")))
        if plain:
            raise Refused("its Blobs and Attachment headers do not agree")
        return out

    def _check(self, art: Article) -> str:
        if art.group not in self.targets:
            raise Refused("not a group this gateway exports")
        net = self.targets[art.group][0]
        if self.state.foreign_for(art.message_id):
            raise Refused("already exported")
        if art.control:
            raise Refused("control article")
        if "Encrypted" in art:
            raise Refused("encrypted")
        if not is_text(art):
            raise Refused("not text")
        if art.proven_pow() < self.min_pow:
            raise Refused(f"postage below {self.min_pow} bits")
        hour_ago = int(time.time()) - 3600
        n = self.state.db.execute("SELECT COUNT(*) FROM exports WHERE author=? AND at>?",
                                  (art["Author-Key"], hour_ago)).fetchone()[0]
        if n >= self.per_key_per_hour:
            raise Refused("rate limit for this key")
        return net

    async def _manifest(self, group_id: str) -> Manifest | None:
        """The group's current manifest: the watcher's, or read from the node (cached)."""
        view = getattr(self.watcher, "views", {}).get(group_id) if self.watcher else None
        if view is not None and view.manifest is not None:
            return view.manifest
        hit = self._manifests.get(group_id)
        if hit is not None and time.time() - hit[1] < MANIFEST_TTL:
            return hit[0]
        try:
            m = await self.node.manifest(group_id)
        except (NNTPError, ConnectionError, OSError) as exc:
            log.debug("manifest of %s: %s", group_id, exc)
            m = None
        self._manifests[group_id] = (m, time.time())
        return m

    @staticmethod
    def _permitted(approved: Article, m: Manifest | None) -> None:
        """Would the group take the approved copy? Asked *before* it goes to the other
        network, which cannot be undone: a banned author, a chat line in a group of posts
        or too little postage would otherwise be on the other network and never here.
        The node's own rules (its postage, blocks) can still refuse it after."""
        if m is None:
            return                                       # unknown: the node decides
        if approved.proven_pow() < m.min_pow:
            raise Refused(f"postage below the group's {m.min_pow} bits")
        try:
            check_permitted(approved, m, {})
        except ArticleError as exc:
            raise Refused(f"the group would refuse it: {exc}") from exc

    def _due(self, mid: str, group: str, now: float) -> float:
        """When a submission may go out: the random delay of UN-RFC 0023 5.2, drawn once
        and kept in the state, plus this gateway's turn (UN-RFC 0038 section 5)."""
        key = f"export-due:{mid}"
        due = self.state.get_cursor("unseenet", key)
        if due is None:
            due = int(now + random.uniform(0, self.delay_max))
            self.state.set_cursor("unseenet", key, due)
        turn = self.watcher.delay(group) if self.watcher else 0.0
        return float(due) + (turn or 0.0)

    async def _retract(self, art: Article, foreign_id: str) -> bool:
        """Ask the adapter's sender to take back what it sent, if it can: a sender may
        have ``retract(art, foreign_id) -> bool`` beside ``send`` (core/adapter.py)."""
        send = self.targets[art.group][1]
        retract = getattr(getattr(send, "__self__", None), "retract", None)
        if retract is None:
            return False
        try:
            return bool(await retract(art, foreign_id))
        except Exception as exc:  # noqa: BLE001  (the other network said no)
            log.warning("could not take back %s: %s", foreign_id, exc)
            return False

    def _done(self, n_mid: str) -> None:
        self.state.db.execute("DELETE FROM cursor WHERE network='unseenet' AND source=?",
                              (f"export-due:{n_mid}",))
        self.state.db.commit()

    async def run(self) -> ExportStats:
        st = ExportStats()
        now = time.time()
        first_waiting = None
        last = self.state.get_cursor("unseenet", self.inbox)
        for n, art in await self._submissions():
            last = n
            if self.state.foreign_for(art.message_id):
                continue                                 # sent already (read again while waiting)
            try:
                net = self._check(art)
            except Refused as r:
                st.refused += 1
                log.info("submission %s refused: %s", art.message_id, r)
                self._done(art.message_id)
                continue
            if self.watcher is not None and self.watcher.delay(art.group) is None:
                continue                                 # not (yet) a gateway of the group
            if now < self._due(art.message_id, art.group, now):
                st.waiting += 1
                first_waiting = n if first_waiting is None else min(first_waiting, n)
                continue
            if self.watcher is not None and self.watcher.approved_elsewhere(art.message_id):
                log.info("submission %s already sent by another gateway", art.message_id)
                self._done(art.message_id)
                continue
            approved = approve(Article.from_bytes(art.to_bytes()), self.gateway)
            try:
                self._permitted(approved, await self._manifest(art.group))
            except Refused as r:
                st.refused += 1
                log.info("submission %s refused: %s", art.message_id, r)
                self._done(art.message_id)
                continue
            try:
                files = await self._plain_files(art)
            except NotYet:
                st.waiting += 1
                first_waiting = n if first_waiting is None else min(first_waiting, n)
                continue
            except Refused as r:
                st.refused += 1
                log.info("submission %s refused: %s", art.message_id, r)
                self._done(art.message_id)
                continue
            send = self.targets[art.group][1]
            try:
                foreign_id = await (send(art, files=files) if files and takes_files(send)
                                    else send(art))
            except Exception as exc:  # noqa: BLE001  (refused by the other network)
                st.failed += 1
                log.warning("%s refused %s: %s", net, art.message_id, exc)
                self._done(art.message_id)
                continue
            try:                                         # posted only after the other side took it
                if files:
                    await self.node.post(approved, blobs=[(f.blob, f.octets) for f in files])
                else:
                    await self.node.post(approved)
            except NNTPError as exc:
                # The pre-check cannot see the node's own rules (its postage, blocks):
                # take the message back on the other network rather than leave it
                # there alone (#18).
                if await self._retract(art, foreign_id):
                    st.retracted += 1
                    log.warning("the node refused the approved copy of %s (%s); taken "
                                "back on %s", art.message_id, exc, net)
                    self._done(art.message_id)
                    continue
                st.unmatched += 1
                log.error("%s took %s as %s, but the node refused the approved copy: %s",
                          net, art.message_id, foreign_id, exc)
            self.state.remember(net, foreign_id, art.message_id, art.group, "out")
            self.state.db.execute("INSERT INTO exports VALUES (?,?)",
                                  (art["Author-Key"], int(time.time())))
            self._done(art.message_id)
            st.exported += 1
            log.info("exported %s to %s as %s", art.message_id, net, foreign_id)
        if last is not None:
            # never move past a submission still waiting for its time (UN-RFC 0038 5)
            self.state.set_cursor("unseenet", self.inbox,
                                  last if first_waiting is None else first_waiting - 1)
        return st
