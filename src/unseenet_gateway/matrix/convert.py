"""Matrix event -> Unseenet article (UN-RFC 0037, draft Matrix profile).

Pure functions, no I/O. The feed supplies the lookups (mapped ids, display
names) as callables.

Matrix is chat, so every imported message is a chat line (UN-RFC 0028):
`Message-Kind: chat` and an empty `Subject`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import format_datetime

from unseenet.core.article import MAX_SIZE, Article, new_article
from unseenet.core.markup import MARKDOWN_TYPE

from ..core.adapter import Skip
from ..core.origin import origin_headers
from .html import to_markdown

HTML_FORMAT = "org.matrix.custom.html"

NETWORK = "matrix"
MESSAGE, REDACTION, ENCRYPTED = "m.room.message", "m.room.redaction", "m.room.encrypted"
REACTION = "m.reaction"            # an annotation: a reaction (UN-RFC 0048 section 9)
TEXT_TYPES = ("m.text", "m.notice", "m.emote")
PICTURE = "m.image"                 # a picture: its caption, and the picture (gateway#26)
FROM_UNSEENET_KEY = "org.unseenet.message_id"   # set on events a gateway exported
GATEWAY_NOTICE_KEY = "org.unseenet.gateway"     # on a gateway's own notices (UN-RFC 0037 6)
MAX_FROM = 128

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BAD = re.compile(r"[^a-z0-9+_-]+")


# --- names ----------------------------------------------------------------------

def _component(text: str) -> str:
    return _BAD.sub("-", text.lower()).strip("-")


def unseenet_name(alias: str, prefix: str) -> str | None:
    """`#python:matrix.org` -> `matrix.matrix-org.python`, or None if it cannot map.

    The homeserver comes first, as one component with dots and ports turned
    into `-`, so rooms with the same alias on different servers never meet.
    A room known only by its id has no name of its own; the operator names it.
    """
    if not alias.startswith("#") or ":" not in alias:
        return None
    local, _, server = alias[1:].partition(":")
    parts = [_component(server), _component(local)]
    if not all(p and p[0].isalnum() for p in parts):
        return None
    name = ".".join([prefix, *parts])
    return name if len(name.encode()) <= 128 else None


def clean(value: str, limit: int | None = None) -> str:
    value = _CTRL.sub("", " ".join(str(value).split()))
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def localpart(user_id: str) -> str:
    return user_id[1:].partition(":")[0] if user_id.startswith("@") else user_id


# --- event structure ------------------------------------------------------------

def relation(content: dict) -> dict:
    rel = content.get("m.relates_to")
    return rel if isinstance(rel, dict) else {}


def is_edit(content: dict) -> bool:
    return relation(content).get("rel_type") == "m.replace"


def thread_root(content: dict) -> str | None:
    rel = relation(content)
    return rel.get("event_id") if rel.get("rel_type") == "m.thread" else None


def reply_to(content: dict) -> str | None:
    """The event a message answers. In a thread, the fallback reply is the
    thread's latest message, not a real answer, so it is ignored."""
    rel = relation(content)
    if rel.get("rel_type") == "m.thread" and rel.get("is_falling_back", False):
        return None
    irt = rel.get("m.in_reply_to")
    return irt.get("event_id") if isinstance(irt, dict) else None


def strip_reply_fallback(body: str) -> str:
    """Remove the quoted `> <@user> …` block older clients put before a reply."""
    lines = body.split("\n")
    if not lines or not lines[0].startswith("> <"):
        return body
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    if i < len(lines) and lines[i] == "":
        i += 1
    return "\n".join(lines[i:])


def is_from_unseenet(ev: dict, gateway_user: str | None = None) -> bool:
    content = ev.get("content") or {}
    new = content.get("m.new_content") or {}
    return (FROM_UNSEENET_KEY in content or FROM_UNSEENET_KEY in new
            or GATEWAY_NOTICE_KEY in content          # another gateway's notice
            or (gateway_user is not None and ev.get("sender") == gateway_user))


# --- conversion -----------------------------------------------------------------

def convert_reaction(ev: dict, *, group_id: str, id_map, name_of, gateway_user=None,
                     now: datetime | None = None, takes=None) -> Article:
    """An ``m.reaction`` annotation with one emoji key -> an Unseenet reaction (UN-RFC
    0048 section 9), carrying ``Origin-From`` so each Matrix user counts once. Keys
    that are not one emoji, emoji the group does not take (``takes``), and reactions to
    messages not imported are dropped."""
    from unseenet.core import reactions as rx
    if is_from_unseenet(ev, gateway_user):
        raise Skip("loop", "came from Unseenet")
    rel = (ev.get("content") or {}).get("m.relates_to") or {}
    if rel.get("rel_type") != "m.annotation":
        raise Skip("syntax", "not an annotation")
    key = rel.get("key")
    if not isinstance(key, str) or not rx.is_emoji(key.strip()):
        raise Skip("reaction", "not one emoji")
    emoji = key.strip()
    if takes is not None and not takes(emoji):
        raise Skip("reaction", "not in the group's set")
    target = id_map(rel.get("event_id")) if rel.get("event_id") else None
    if not target:
        raise Skip("reaction", "to a message that was not imported")
    sender = ev.get("sender", "")
    author = name_of(sender)
    ts = ev.get("origin_server_ts")
    when = format_datetime(datetime.fromtimestamp(ts / 1000, timezone.utc)) \
        if isinstance(ts, int) else None
    art = rx.build(group_id, [], target, emoji,
                   from_name=clean(author, MAX_FROM) if author else clean(localpart(sender), MAX_FROM))
    for name, value in origin_headers(NETWORK, ev.get("event_id", ""), when, sender):
        art.headers.append((name, value))
    art.headers.append(("Matrix-Room", ev.get("room_id", "")))
    return art


def convert(ev: dict, *, group_id: str, id_map, name_of, gateway_user: str | None = None,
            now: datetime | None = None, takes=None) -> Article:
    """Build the unsigned Unseenet article for one event from a public room.

    ``id_map(event_id)`` -> Unseenet Message-ID or None; ``name_of(user_id)``
    -> a display name or None.
    """
    etype = ev.get("type")
    if etype == ENCRYPTED:
        raise Skip("encrypted")
    if etype == REACTION:
        return convert_reaction(ev, group_id=group_id, id_map=id_map, name_of=name_of,
                                gateway_user=gateway_user, now=now, takes=takes)
    if etype != MESSAGE:
        raise Skip("type", str(etype))
    if is_from_unseenet(ev, gateway_user):
        raise Skip("loop", "came from Unseenet")
    content = ev.get("content") or {}
    if not content:
        raise Skip("redacted")
    edit = is_edit(content)
    shown = content.get("m.new_content") if edit else content
    if not isinstance(shown, dict):
        raise Skip("syntax", "edit without m.new_content")
    msgtype = shown.get("msgtype")
    picture = msgtype == PICTURE and not edit and isinstance(shown.get("url"), str) \
        and shown["url"].startswith("mxc://")
    if msgtype not in TEXT_TYPES and not picture:
        raise Skip("media", str(msgtype))          # UN-RFC 0023 4.1 item 3
    body = shown.get("body")
    if not isinstance(body, str):
        raise Skip("syntax", "no body")
    if picture:                    # body is the caption when a filename is given (Matrix 1.10)
        name = shown.get("filename")
        body = body if isinstance(name, str) and name and body != name else ""
        if not body.strip():
            body = "(a picture)"
    if not edit and reply_to(content):
        body = strip_reply_fallback(body)
    markdown = None                 # formatted_body as Markdown (gateway#26)
    if shown.get("format") == HTML_FORMAT and isinstance(shown.get("formatted_body"), str):
        markdown = to_markdown(shown["formatted_body"]) or None
    sender = ev.get("sender", "")
    author = name_of(sender)
    from_name = clean(author, MAX_FROM) if author else clean(localpart(sender), MAX_FROM)
    if markdown is not None:
        body = markdown
    if msgtype == "m.emote":
        body = f"* {from_name} {body}" if markdown is None else f"\\* {from_name} {body}"
    if not body.strip():
        raise Skip("empty")

    refs: list[str] = []
    if edit:
        target = relation(content).get("event_id")
        wanted = [target]
    else:
        wanted = [thread_root(content), reply_to(content)]
    for eid in wanted:
        mid = id_map(eid) if eid else None
        if mid and mid not in refs:
            refs.append(mid)
    if edit and not refs:
        raise Skip("edit", "of a message that was not imported")

    ts = ev.get("origin_server_ts")
    when = format_datetime(datetime.fromtimestamp(ts / 1000, timezone.utc)) \
        if isinstance(ts, int) else None

    ctype = MARKDOWN_TYPE if markdown is not None else "text/plain; charset=utf-8"
    extra = [("Content-Type", ctype), ("Message-Kind", "chat")]
    extra += origin_headers(NETWORK, ev.get("event_id", ""), when, sender)
    extra.append(("Matrix-Room", ev.get("room_id", "")))
    if edit:
        extra.append(("Matrix-Edit", relation(content).get("event_id", "")))
    if msgtype != "m.text":
        extra.append(("Matrix-Msgtype", msgtype))
    if picture:
        extra.append(("Gateway-Media", "1"))     # the importer fetches it (core/files.py)

    art = new_article(group_id, "", body, from_name=from_name,
                      references=refs or None, extra=extra, now=now)
    if len(art.to_bytes()) + 400 > MAX_SIZE:
        raise Skip("size")
    return art
