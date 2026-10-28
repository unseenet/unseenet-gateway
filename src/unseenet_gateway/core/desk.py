"""The operator's desk: requests to gateway a source, decided from the operator's inbox.

A source on another network can ask to be gatewayed (on Matrix: by inviting the
gateway's account to a room it does not carry yet). The gateway then

1. sends the operator a private post (UN-RFC 0009) in their own inbox, signed by
   the gateway key, saying what the source is and what the group would be called;
2. reads the gateway's own inbox for answers. An answer counts only if its sender
   is verified as the configured operator key and it refers to the request;
3. records the decision: `approve` (optionally with a different name) or
   `decline <reason>`. Each source is asked about once.

With `auto_create`, an approved group's manifest is published by the gateway
itself, which needs the group key on the gateway host; without it, approval
only marks the source, and the operator runs `create-groups` as before.

Network-neutral: an adapter decides what counts as a request and how to answer
it on its own network; this module only talks to Unseenet and the operator.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from unseenet.core import crypto
from unseenet.core.article import Article, ArticleError
from unseenet.core.groups import inbox_article, open_inbox_article, personal_inbox_id
from unseenet.nntp import NNTPError, strip_reader_headers

from .state import State
from .unseenet_side import Node

log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    group_id TEXT NOT NULL,
    key      TEXT NOT NULL,          -- another gateway admitted to the group (UN-RFC 0038 3.2)
    account  TEXT,
    PRIMARY KEY (group_id, key)
);
CREATE TABLE IF NOT EXISTS desk (
    network  TEXT NOT NULL,
    source   TEXT NOT NULL,
    status   TEXT NOT NULL,          -- 'pending', 'approved', 'declined'
    name     TEXT,                   -- the Unseenet group name (proposed, then decided)
    request  TEXT,                   -- Message-ID of the request in the operator's inbox
    reason   TEXT,
    detail   TEXT,                   -- what the request said, for the log and the answer
    at       INTEGER NOT NULL,
    PRIMARY KEY (network, source)
);
"""
_NAME = re.compile(r"^[a-z0-9][a-z0-9+_-]*(\.[a-z0-9][a-z0-9+_-]*)*$")
_GID = re.compile(r"^[a-z0-9][a-z0-9+_-]*(\.[a-z0-9][a-z0-9+_-]*)*@[a-z2-7]{16}$")   # join one


JOIN = "join"                      # the desk's "network" for join requests from gateways


@dataclass
class JoinRequest:
    """A signed request from another gateway to serve one of our groups too."""
    sender: str                      # its gateway key
    group: str
    network: str
    source: str
    account: str
    operator: str
    contact: str


@dataclass
class Request:
    network: str
    source: str
    status: str
    name: str | None
    request: str | None
    reason: str | None


def parse_join(body: str, sender: str) -> JoinRequest | None:
    import json
    try:
        d = json.loads(body)
        req = JoinRequest(sender=sender, group=str(d["group"]), network=str(d["network"]),
                          source=str(d["source"]), account=str(d.get("account", "")),
                          operator=str(d.get("operator", "")), contact=str(d.get("contact", "")))
    except (ValueError, KeyError, TypeError):
        return None
    return req if "@" in req.group else None


def parse_answer(body: str) -> tuple[str, str] | None:
    """('approve', name or '') or ('decline', reason) from the first line that is not a
    quote; None if the answer says neither."""
    for line in body.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith(">"):
            continue
        word, _, rest = line.partition(" ")
        word = word.lower().rstrip(":.,!")
        if word in ("approve", "approved", "yes", "ja"):
            return "approve", rest.strip()
        if word in ("decline", "declined", "no", "nej"):
            return "decline", rest.strip()
        return None
    return None


class Desk:
    def __init__(self, *, state: State, node: Node, gateway: crypto.Identity, operator: str,
                 contact: str, pow_bits: int = 18):
        self.state, self.node, self.gateway = state, node, gateway
        self.operator_text = operator
        self.operator = crypto.key_from_text(operator) if operator.startswith("ed25519:") else None
        self.contact, self.pow_bits = contact, pow_bits
        self.inbox = personal_inbox_id(gateway.public_key)
        self.cursor_key = f"desk:{self.inbox}"
        self.joins: list[JoinRequest] = []
        self.state.db.executescript(SCHEMA)

    # --- what is known ---
    def get(self, network: str, source: str) -> Request | None:
        row = self.state.db.execute(
            "SELECT network, source, status, name, request, reason FROM desk "
            "WHERE network=? AND source=?", (network, source)).fetchone()
        return Request(*row) if row else None

    def approved(self, network: str) -> list[Request]:
        rows = self.state.db.execute(
            "SELECT network, source, status, name, request, reason FROM desk "
            "WHERE network=? AND status='approved'", (network,)).fetchall()
        return [Request(*r) for r in rows]

    def peers(self, group_id: str) -> list[tuple[str, str]]:
        return [tuple(r) for r in self.state.db.execute(
            "SELECT key, account FROM peers WHERE group_id=? ORDER BY key", (group_id,))]

    def add_peer(self, group_id: str, key: str, account: str) -> None:
        self.state.db.execute("INSERT OR REPLACE INTO peers VALUES (?,?,?)",
                              (group_id, key, account))
        self.state.db.commit()

    def _set(self, network: str, source: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.state.db.execute(f"UPDATE desk SET {cols} WHERE network=? AND source=?",
                              (*fields.values(), network, source))
        self.state.db.commit()

    # --- asking ---
    async def ask(self, network: str, source: str, *, title: str, detail: str,
                  name: str | None) -> bool:
        """Send the operator one request for ``source``; False if asked before."""
        if self.get(network, source) is not None:
            return False
        if self.operator is None or not self.contact.startswith("inbox@"):
            log.warning("request for %s %s not sent: gateway.operator and gateway.contact "
                        "must be a key and its personal inbox", network, source)
            return False
        if network == JOIN:
            body = (f"{detail}\n\n"
                    "Answer this post with one line:\n"
                    "  approve              - let it serve the group too\n"
                    "  decline <reason>     - do not\n\n"
                    "Only answers signed by the operator key in gateway.toml count.")
        else:
            proposed = name or "(none: give one, e.g. 'approve matrix.example.room')"
            body = (f"{detail}\n\n"
                    f"Proposed Unseenet group: {proposed}\n"
                    "(or a Group ID, name@fingerprint, to join a group another gateway runs)\n\n"
                    "Answer this post with one line:\n"
                    "  approve              - gateway it under the proposed name\n"
                    "  approve <name>       - gateway it under another name, or join <name@fp>\n"
                    "  decline <reason>     - do not gateway it; the reason is shown to them\n\n"
                    "Only answers signed by the operator key in gateway.toml count.")
        art = await self._post_inbox(lambda bits: inbox_article(
            self.contact, self.operator, from_name="Unseenet gateway",
            subject=f"Gateway request: {title}", body=body, sender=self.gateway,
            reply_inbox=self.inbox, pow_bits=bits))
        self.state.db.execute("INSERT INTO desk VALUES (?,?,?,?,?,?,?,?)",
                              (network, source, "pending", name, art.message_id, None, detail,
                               int(time.time())))
        self.state.db.commit()
        log.info("asked the operator about %s %s (%s)", network, source, art.message_id)
        return True

    async def _post_inbox(self, make) -> Article:
        """Post an inbox article built by ``make(bits)``, with this desk's postage - and,
        if the node asks for more ("441 postage: need N bits": an inbox's own minimum, or
        a young key's), once more with that. Its outer signature is a one-time key's, so
        it is built anew rather than signed again (gateway#16)."""
        bits = self.pow_bits
        for attempt in (1, 2):
            art = make(bits)
            try:
                await self.node.post(art)
                return art
            except NNTPError as exc:
                need = re.search(r"need (\d+) bits", str(exc))
                if attempt == 2 or not need or int(need.group(1)) <= bits:
                    raise
                bits = int(need.group(1))
        raise AssertionError("unreachable")

    async def request_join(self, *, group: str, to_inbox: str, to_key: bytes, network: str,
                           source: str, account: str) -> bool:
        """Ask the gateway that owns ``group`` to admit this one (UN-RFC 0038 3.2); once."""
        flag = f"join-sent:{group}"
        if self.state.get_cursor("unseenet", flag) is not None:
            return False
        import json
        body = json.dumps({"group": group, "network": network, "source": source,
                           "account": account, "operator": self.operator_text,
                           "contact": self.contact}, sort_keys=True)
        await self._post_inbox(lambda bits: inbox_article(
            to_inbox, to_key, from_name="Unseenet gateway", subject=f"Join request: {group}",
            body=body + "\n", sender=self.gateway, reply_inbox=self.inbox,
            content_type="application/json", pow_bits=bits, extra=[("Gateway-Request", JOIN)]))
        self.state.set_cursor("unseenet", flag, int(time.time()))
        log.info("asked %s to let this gateway serve %s too", to_inbox, group)
        return True

    # --- answers ---
    async def poll(self) -> list[Request]:
        """Read the gateway's inbox for the operator's answers; return the decided requests."""
        c = await self.node._conn()
        try:
            _, low, high = await c.group(self.inbox)
        except NNTPError:
            return []
        after = self.state.get_cursor("unseenet", self.cursor_key) or (low - 1)
        pending = {r[0]: (r[1], r[2]) for r in self.state.db.execute(
            "SELECT request, network, source FROM desk WHERE status='pending'")}
        decided = []
        self.joins: list[JoinRequest] = []
        for n in range(max(after + 1, low), high + 1):
            raw = await c.article(str(n))
            self.state.set_cursor("unseenet", self.cursor_key, n)
            if raw is None:
                continue
            try:
                outer = Article.from_bytes(strip_reader_headers(raw))
                inner, sender = open_inbox_article(outer, self.gateway)
            except (ArticleError, ValueError):
                continue
            if sender is not None and inner.get("Gateway-Request", "").strip() == JOIN:
                req = parse_join(inner.body, crypto.key_to_text(sender))
                if req is not None:
                    self.joins.append(req)
                continue
            if sender is None or sender != self.operator or not pending:
                continue                           # only the operator decides
            refs = outer.get("References", "").split() + inner.get("References", "").split()
            hit = next((pending[m] for m in refs if m in pending), None)
            if hit is None:
                continue
            answer = parse_answer(inner.body)
            if answer is None:
                log.info("operator answered %s %s but said neither approve nor decline", *hit)
                continue
            network, source = hit
            verdict, arg = answer
            req = self.get(network, source)
            if verdict == "approve" and network == JOIN:
                self._set(network, source, status="approved", reason=None)
            elif verdict == "approve":
                name = arg.lower() if arg else req.name
                if not name or not (_NAME.match(name) or _GID.match(name)) or len(name.encode()) > 150:
                    log.warning("approval of %s %s has no valid group name (%r)", network,
                                source, arg)
                    continue
                self._set(network, source, status="approved", name=name, reason=None)
            else:
                self._set(network, source, status="declined",
                          reason=arg or "Declined by the gateway's operator.")
            pending = {k: v for k, v in pending.items() if v != hit}
            decided.append(self.get(network, source))
            log.info("operator %sd %s %s", verdict, network, source)
        return decided
