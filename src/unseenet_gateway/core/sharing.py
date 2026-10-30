"""Shares through a gateway (UN-RFC 0060 section 9), the same for every network.

- **Out.** A plain share goes out as the other network's repost when the target is
  there too, a share with a comment as a quote; otherwise as a message with the share's
  text and the target's address (``out_text``). The target's own text is never copied
  out on the sharer's behalf.
- **In.** A repost or a quote becomes a share when the gateway carries the target,
  otherwise an ordinary message with a link (the network's adapter).
- Share notices are never carried, either way (``is_notice``).

Whether a group takes shares is its manifest's (``share_into`` and its view, UN-RFC 0060
section 7.2); a group whose manifest the gateway could not read takes none.
"""

from __future__ import annotations

from dataclasses import dataclass

from unseenet.core import shares as sh
from unseenet.core.article import Article


@dataclass(frozen=True)
class Takes:
    """What a gatewayed group takes of shares coming in."""
    shares: bool = False       # a share with a comment
    plain: bool = False        # a plain share

    @classmethod
    def of(cls, manifest_raw: dict | None) -> "Takes":
        if not manifest_raw:
            return cls()
        return cls(sh.share_into(manifest_raw), sh.takes_plain(manifest_raw))


NONE = Takes()


def target(art: Article) -> tuple[str, str] | None:
    """``(Message-ID, Group ID)`` an article shares, or None."""
    return sh.target(art) if sh.is_share(art) else None


def is_plain(art: Article) -> bool:
    return sh.is_plain(art)


def is_notice(art: Article) -> bool:
    return sh.is_notice(art)


def out_text(art: Article) -> str:
    """The text a network without reposts gets: a plain share says what it shares, a
    share with a comment has the target's address under the comment; anything else is
    its body. Never the target's own text."""
    body = art.body.rstrip("\n")
    t = target(art)
    if t is None:
        return body
    where = sh.address(t[1], t[0])
    if is_plain(art):
        return f"Shared: {where}"
    return f"{body}\n\nShared: {where}"


def plain(group: str, target_id: str, target_group: str, from_name: str | None = None,
          extra: list[tuple[str, str]] = ()) -> Article:
    """An unsigned plain share for ``group``, with the other network's ``extra``."""
    art = sh.build_plain(group, target_id, target_group, from_name=from_name)
    for name, value in extra:
        art.headers.append((name, value))
    return art


def with_comment(art: Article, target_id: str, target_group: str) -> Article:
    """Make a converted thread root a share with a comment of ``target_id``."""
    for name, value in sh.headers(target_id, target_group):
        art.set(name, value)
    return art
