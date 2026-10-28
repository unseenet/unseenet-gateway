"""Usenet article -> Unseenet article (UN-RFC 0024, section 4).

Pure functions, no I/O, so they can be tested against real articles.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses

from unseenet.core.article import MAX_SIZE, Article, new_article

from ..core.adapter import Skip  # noqa: F401  (shared by all networks)
from ..core.origin import origin_headers

NETWORK = "usenet"
MAX_FROM = 128
MAX_SUBJECT = 256
NO_ARCHIVE_DAYS = 7

_YENC = re.compile(r"^=ybegin\s", re.M)
_UUENCODE = re.compile(r"^begin [0-7]{3} \S", re.M)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MSGID = re.compile(r"<[^<>\s]+@[^<>\s]+>")




@dataclass
class Converted:
    article: Article              # unsigned; the caller finalizes it
    usenet_id: str
    references_unmapped: bool
    other_groups: list[str] = field(default_factory=list)


def parse(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


def unescape(value: str) -> str:
    """Raw 8-bit octets the email parser kept as surrogates (a header sent without MIME
    encoding, a body with the wrong charset) as text: UTF-8 if they are, else
    Windows-1252, as Usenet readers do. A surrogate cannot be written as UTF-8 at all,
    so an article with one could not be posted (#55)."""
    if not any("\udc80" <= ch <= "\udcff" for ch in value):
        return value
    octets = value.encode("utf-8", "surrogateescape")
    try:
        return octets.decode("utf-8")
    except UnicodeDecodeError:
        return octets.decode("cp1252", "replace")


def clean(value: str, limit: int | None = None) -> str:
    """Unfold, drop control characters, trim to ``limit`` characters."""
    value = " ".join(unescape(str(value)).split())
    value = _CTRL.sub("", value)
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def header_text(msg: EmailMessage, name: str) -> str | None:
    """A header as text: MIME-decoded as usual, but sent as raw 8-bit octets it is
    read as ``unescape`` reads them, not as the parser's U+FFFD (#55)."""
    for k, v in msg.raw_items():
        if k.lower() == name.lower() and any("\udc80" <= ch <= "\udcff" for ch in v):
            return unescape(v)
    value = msg.get(name)
    return None if value is None else str(value)


def raw_header(msg: EmailMessage, name: str) -> str | None:
    """Header value as sent, unfolded, without MIME decoding."""
    for k, v in msg.raw_items():
        if k.lower() == name.lower():
            return clean(v)
    return None


def display_from(msg: EmailMessage) -> str:
    value = header_text(msg, "From")
    if value is None:
        raise Skip("syntax", "no From")
    pairs = getaddresses([str(value)])
    name, addr = pairs[0] if pairs else ("", "")
    if not name:
        name = addr.partition("@")[0] or "unknown"
    return clean(name, MAX_FROM)


def is_control(msg: EmailMessage) -> bool:
    if msg.get("Control"):
        return True
    return str(msg.get("Subject", "")).startswith("cmsg ")


def text_body(msg: EmailMessage) -> str:
    """The first text/plain part as text, or Skip (section 4.1 item 3)."""
    if msg.is_multipart():
        part = None
        for p in msg.walk():
            ctype = p.get_content_type()
            if p.is_multipart():
                continue
            if ctype == "text/plain" and part is None:
                part = p
            elif ctype in ("text/plain", "text/html", "application/pgp-signature",
                           "application/pkcs7-signature"):
                continue
            else:
                raise Skip("binary", ctype)
        if part is None:
            raise Skip("binary", "no text/plain part")
    else:
        part = msg
        if part.get_content_type() != "text/plain":
            raise Skip("binary", part.get_content_type())
    try:
        text = part.get_content()
    except (LookupError, UnicodeError, ValueError) as exc:
        raise Skip("charset", str(exc)) from exc
    if not isinstance(text, str):
        raise Skip("binary", "not text")
    text = unescape(text)
    if _YENC.search(text) or _UUENCODE.search(text):
        raise Skip("binary", "encoded file in body")
    return text


def unseenet_name(usenet_group: str, prefix: str) -> str | None:
    """`dk.politik` -> `usenet.dk.politik` (section 3.1), or None if impossible."""
    name = f"{prefix}.{usenet_group.lower()}"
    if len(name.encode()) > 128:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9+_-]*(\.[a-z0-9][a-z0-9+_-]*)*", name):
        return None
    return name


def convert(raw: bytes, *, carried: dict[str, str], id_map,
            now: datetime | None = None) -> Converted:
    """Build the Unseenet article for one Usenet article.

    ``carried`` maps Usenet group names this gateway gateways to their Unseenet
    Group IDs. ``id_map(usenet_message_id) -> unseenet_message_id | None``.
    """
    now = now or datetime.now(timezone.utc)
    msg = parse(raw)
    usenet_id = clean(msg.get("Message-ID", ""))
    if not _MSGID.fullmatch(usenet_id):
        raise Skip("syntax", "no Message-ID")
    if usenet_id.endswith("@unseenet>"):
        raise Skip("loop", "came from Unseenet")            # 4.1 item 2
    if is_control(msg):
        raise Skip("control")                               # 4.1 item 1

    groups = [g.strip() for g in str(msg.get("Newsgroups", "")).split(",") if g.strip()]
    target = next((g for g in groups if g in carried), None)
    if target is None:
        raise Skip("group", ",".join(groups))
    group_id = carried[target]                             # 4.4: first carried group

    subject = clean(header_text(msg, "Subject") or "(no subject)", MAX_SUBJECT)
    from_name = display_from(msg)
    body = text_body(msg)

    # 4.3 references
    refs_raw = _MSGID.findall(str(msg.get("References", "")))
    mapped, unmapped = [], False
    for ref in refs_raw:
        if ref.endswith("@unseenet>"):
            mapped.append(ref)
            continue
        hit = id_map(ref)
        if hit:
            mapped.append(hit)
        else:
            unmapped = True

    extra: list[tuple[str, str]] = [("Content-Type", "text/plain; charset=utf-8")]
    extra += origin_headers(NETWORK, usenet_id, raw_header(msg, "Date"),
                            raw_header(msg, "From"))
    if len(groups) > 1:
        extra.append(("Usenet-Newsgroups", ",".join(groups)))
    if unmapped and refs_raw:
        extra.append(("Usenet-References", " ".join(refs_raw)))
    fup = raw_header(msg, "Followup-To")
    if fup:
        extra.append(("Usenet-Followup-To", clean(fup, 998)))

    # 4.5 no-archive
    xna = str(msg.get("X-No-Archive", "")).strip().lower()
    arch = str(msg.get("Archive", "")).strip().lower()
    if xna == "yes" or arch.startswith("no"):
        expires = (now + timedelta(days=NO_ARCHIVE_DAYS)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        extra.append(("Expires", expires))

    art = new_article(group_id, subject, body, from_name=from_name,
                      references=mapped or None, extra=extra, now=now)
    # Room for Author-Key, Signature, Proof-Of-Work and Message-ID (~300 octets)
    if len(art.to_bytes()) + 400 > MAX_SIZE:
        raise Skip("size")
    return Converted(art, usenet_id, unmapped, [g for g in groups if g != target])
