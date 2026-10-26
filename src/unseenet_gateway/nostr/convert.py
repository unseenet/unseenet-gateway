"""Nostr event -> Unseenet article (draft Nostr profile, UN-RFC 0023 section 9).

Pure functions, no I/O. The importer supplies the lookups (mapped ids, root
titles, author names) as callables.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import format_datetime

from unseenet.core.markup import MARKDOWN_TYPE
from unseenet.core.article import MAX_SIZE, Article, new_article

from ..core.adapter import Skip  # noqa: F401  (shared by all networks)
from ..core.importer import cancel_article  # noqa: F401
from ..core.origin import origin_headers
from .event import Event

NETWORK = "nostr"
THREAD, CHAT, COMMENT, DELETION, REACTION = 11, 9, 1111, 5, 7
IMPORTED_KINDS = (THREAD, CHAT, COMMENT, REACTION)   # reactions: UN-RFC 0048 section 9
FROM_UNSEENET_TAG = "unseenet"          # set on events a gateway exported
MAX_SUBJECT = 256
MAX_FROM = 128

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"




# --- names and keys ---------------------------------------------------------

def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    return chk


def npub(pubkey_hex: str) -> str:
    """NIP-19 bech32 encoding of a public key."""
    data, acc, bits = [], 0, 0
    for byte in bytes.fromhex(pubkey_hex):
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    hrp = "npub"
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(exp + data + [0] * 6) ^ 1
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[d] for d in data + checksum)


def npub_hex(value: str) -> str | None:
    """The hex public key of an ``npub1...`` (NIP-19), or of 64 hex digits; None if
    it is neither or its checksum is wrong."""
    value = value.strip().lower()
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    if not value.startswith("npub1"):
        return None
    try:
        data = [_BECH32.index(c) for c in value[5:]]
    except ValueError:
        return None
    hrp = "npub"
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    if len(data) < 7 or _polymod(exp + data) != 1:
        return None
    acc, bits, out = 0, 0, bytearray()
    for v in data[:-6]:
        acc = (acc << 5) | v
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return out.hex() if len(out) == 32 else None


def short_npub(pubkey_hex: str) -> str:
    n = npub(pubkey_hex)
    return n[:12] + "…" + n[-4:]


def unseenet_name(group_id: str, prefix: str) -> str | None:
    """A NIP-29 group id -> `nostr.<id>`, or None if it cannot be mapped."""
    part = re.sub(r"[^a-z0-9+_-]", "-", group_id.lower()).strip("-")
    if not part or not part[0].isalnum():
        return None
    name = f"{prefix}.{part}"
    return name if len(name.encode()) <= 128 else None


def clean(value: str, limit: int | None = None) -> str:
    value = _CTRL.sub("", " ".join(str(value).split()))
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def first_line(text: str, limit: int) -> str:
    for line in text.splitlines():
        if line.strip():
            return clean(line, limit)
    return ""


# --- conversion ---------------------------------------------------------------

def is_from_unseenet(ev: Event) -> bool:
    return any(t and t[0] == FROM_UNSEENET_TAG for t in ev.tags)


def root_and_parent(ev: Event) -> tuple[str | None, str | None]:
    """Event ids of the thread root and the direct parent of a message."""
    if ev.kind == COMMENT:
        return ev.tag("E"), ev.tag("e")
    if ev.kind == CHAT:
        parent = ev.tag("e") or ev.tag("q")
        return None, parent
    return None, None


# Markdown that Nostr clients write and show: links, emphasis, code, quotes, lists
_MARKDOWN = re.compile(r"\[[^\]\n]+\]\((?:https?|unseenet|nostr):[^)\s]+\)"   # [text](url)
                       r"|\*\*[^*\n]+\*\*|__[^_\n]+__"                       # strong
                       r"|(?m:^```)|`[^`\n]+`"                                 # code
                       r"|(?m:^>\s)|(?m:^[-*]\s+\S)|(?m:^\d+\.\s+\S)"         # quote, lists
                       r"|(?m:^#{1,6}\s+\S)")                                 # heading


def looks_like_markdown(text: str) -> bool:
    """Whether a Nostr note is written in Markdown (gateway#26): then it goes in as
    Markdown, else as plain text, so a lone * or _ is never taken for emphasis."""
    return bool(_MARKDOWN.search(text))


def pictures_of(ev: Event) -> list[tuple[str, str]]:
    """(url, alt) of the pictures a note carries: its ``imeta`` tags (NIP-92) and
    https links to picture files in its text - at most four, no duplicates."""
    from ..core.files import PICTURE_URL
    out: dict[str, str] = {}
    for t in ev.tags:
        if t and t[0] == "imeta":
            fields = dict(x.split(" ", 1) for x in t[1:] if " " in x)
            url, mime = fields.get("url", ""), fields.get("m", "")
            if url.startswith("https://") and (mime.startswith("image/") or PICTURE_URL.fullmatch(url)):
                out.setdefault(url, fields.get("alt", "")[:512])
    for m in PICTURE_URL.finditer(ev.content):
        out.setdefault(m.group(0), "")
    return list(out.items())[:4]


def convert_reaction(ev: Event, *, group_id: str, id_map, name_of, now=None, takes=None) -> Article:
    """A kind 7 reaction (NIP-25) -> an Unseenet reaction (UN-RFC 0048 section 9): one
    emoji, or ``+``/empty as 👍 and ``-`` as 👎; to the event its last ``e`` tag names,
    if that was imported; with ``Origin-From`` (the npub) so each person counts once.
    An emoji the group does not take (``takes``) is dropped, not changed."""
    from unseenet.core import reactions as rx
    content = ev.content.strip()
    emoji = {"": "👍", "+": "👍", "-": "👎"}.get(content, content)
    if not rx.is_emoji(emoji):
        raise Skip("reaction", "not one emoji")
    if takes is not None and not takes(emoji):
        raise Skip("reaction", "not in the group's set")
    targets = [t[1] for t in ev.tags if len(t) > 1 and t[0] == "e"]
    target = id_map(targets[-1]) if targets else None
    if not target:
        raise Skip("reaction", "to a message that was not imported")
    author = name_of(ev.pubkey)
    when = format_datetime(datetime.fromtimestamp(ev.created_at, timezone.utc))
    art = rx.build(group_id, [], target, emoji,
                   from_name=clean(author, MAX_FROM) if author else short_npub(ev.pubkey))
    for name, value in origin_headers(NETWORK, ev.id, when, npub(ev.pubkey)):
        art.headers.append((name, value))
    art.headers.append(("Origin-Proof", ev.to_json()))
    art.headers.append(("Nostr-Kind", str(REACTION)))
    return art


def convert(ev: Event, *, group_id: str, id_map, title_of, name_of,
            now: datetime | None = None, takes=None) -> Article:
    """Build the unsigned Unseenet article for one verified group event.

    ``id_map(event_id)`` -> Unseenet Message-ID or None; ``title_of(event_id)``
    -> the subject of an earlier message or None; ``name_of(pubkey)`` -> a
    display name or None.
    """
    if ev.kind not in IMPORTED_KINDS:
        raise Skip("kind", str(ev.kind))
    if is_from_unseenet(ev):
        raise Skip("loop", "came from Unseenet")
    if ev.kind == REACTION:
        return convert_reaction(ev, group_id=group_id, id_map=id_map, name_of=name_of,
                                now=now, takes=takes)
    if not ev.content.strip():
        raise Skip("empty")

    root, parent = root_and_parent(ev)
    if ev.kind == THREAD:
        subject = clean(ev.tag("title") or first_line(ev.content, 80) or "(no title)", MAX_SUBJECT)
    elif ev.kind == COMMENT:
        rt = title_of(root) if root else None
        subject = clean("Re: " + (rt or "(thread)"), MAX_SUBJECT)
    else:
        subject = ""                # a chat line: never a made-up subject (UN-RFC 0028 3.3)

    refs = []
    for eid in (root, parent):
        mid = id_map(eid) if eid else None
        if mid and mid not in refs:
            refs.append(mid)

    author = name_of(ev.pubkey)
    from_name = clean(author, MAX_FROM) if author else short_npub(ev.pubkey)
    when = format_datetime(datetime.fromtimestamp(ev.created_at, timezone.utc))

    ctype = MARKDOWN_TYPE if looks_like_markdown(ev.content) else "text/plain; charset=utf-8"
    extra = [("Content-Type", ctype),
             ("Message-Kind", "chat" if ev.kind == CHAT else "post")]
    extra += origin_headers(NETWORK, ev.id, when, npub(ev.pubkey))
    extra.append(("Origin-Proof", ev.to_json()))
    extra.append(("Nostr-Kind", str(ev.kind)))
    if pictures_of(ev):
        extra.append(("Gateway-Media", "1"))     # the importer fetches them (core/files.py)
    exp = ev.tag("expiration")              # NIP-40: a request not to keep it (0026 4.5)
    if exp and exp.isdigit():
        base = now or datetime.now(timezone.utc)
        until = min(int(exp), int(base.timestamp()) + 7 * 86400)
        extra.append(("Expires", format_datetime(datetime.fromtimestamp(until, timezone.utc))))

    art = new_article(group_id, subject, ev.content, from_name=from_name,
                      references=refs or None, extra=extra, now=now)
    if len(art.to_bytes()) + 400 > MAX_SIZE:
        raise Skip("size")
    return art
