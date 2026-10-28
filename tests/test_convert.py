"""Usenet -> Unseenet conversion (UN-RFC 0023, section 4). Synthetic articles only."""

from datetime import datetime, timezone

import pytest

from unseenet.core import crypto
from unseenet.core.article import Article, validate_structure
from unseenet.core.groups import make_group_id

from unseenet_gateway.usenet.convert import Skip, convert, unseenet_name

OWNER = crypto.Identity.generate()
GW = crypto.Identity.generate()
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
CARRIED = {g: make_group_id(unseenet_name(g, "usenet"), OWNER.public_key)
           for g in ("comp.misc", "dk.test")}


def raw(headers: str, body: str = "Hello.\n") -> bytes:
    return (headers.strip("\n").replace("\n", "\r\n") + "\r\n\r\n"
            + body.replace("\n", "\r\n")).encode("utf-8")


BASIC = """
Path: news.example!not-for-mail
From: Ada Lovelace <ada@example.org>
Newsgroups: comp.misc
Subject: Engines
Date: Fri, 16 Oct 2026 07:33:05 +0000
Message-ID: <abc123@example.org>
"""


def conv(r: bytes, id_map=lambda m: None):
    return convert(r, carried=CARRIED, id_map=id_map, now=NOW)


def test_names():
    assert unseenet_name("dk.politik", "usenet") == "usenet.dk.politik"
    assert unseenet_name("Comp.Misc", "usenet") == "usenet.comp.misc"
    assert unseenet_name("comp.lang.c++", "usenet") == "usenet.comp.lang.c++"
    assert unseenet_name("bad name", "usenet") is None
    assert unseenet_name("x." * 70, "usenet") is None


def test_basic_article_is_valid_unseenet():
    c = conv(raw(BASIC))
    art = c.article.finalize(GW, 8)
    validate_structure(Article.from_bytes(art.to_bytes()), raw_size=len(art.to_bytes()), now=NOW)
    assert art.group == CARRIED["comp.misc"]
    assert art["From"] == "Ada Lovelace"
    assert art["Subject"] == "Engines"
    assert art["Origin-ID"] == "<abc123@example.org>"
    assert art["Origin-Network"] == "usenet"
    assert art["Origin-From"] == "Ada Lovelace <ada@example.org>"
    assert art["Origin-Date"] == "Fri, 16 Oct 2026 07:33:05 +0000"
    assert "Path" not in art and "Usenet-Newsgroups" not in art
    assert art["Date"] == "Sat, 17 Oct 2026 12:00:00 +0000"     # time of import


def test_mime_subject_and_charset():
    h = BASIC.replace("Subject: Engines",
                      "Subject: =?UTF-8?Q?R=C3=B8dgr=C3=B8d_med_fl=C3=B8de?=")
    h += "MIME-Version: 1.0\nContent-Type: text/plain; charset=iso-8859-1\n" \
         "Content-Transfer-Encoding: 8bit\n"
    r = (h.strip("\n").replace("\n", "\r\n") + "\r\n\r\n").encode() + "Blåbær\r\n".encode("latin-1")
    art = conv(r).article
    assert art["Subject"] == "Rødgrød med fløde"
    assert art.body == "Blåbær\n"


def test_quoted_printable_body():
    h = BASIC + "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n" \
                "Content-Transfer-Encoding: quoted-printable\n"
    assert conv(raw(h, "Hej =C3=A6=C3=B8=C3=A5\n")).article.body == "Hej æøå\n"


def test_crosspost_imported_once_into_first_carried():
    h = BASIC.replace("Newsgroups: comp.misc", "Newsgroups: alt.other,dk.test,comp.misc")
    c = conv(raw(h))
    assert c.article.group == CARRIED["dk.test"]
    assert c.article["Usenet-Newsgroups"] == "alt.other,dk.test,comp.misc"


def test_references_mapped_and_kept():
    h = BASIC + "References: <root@example.org> <mid@example.org> <x@unseenet>\n"
    mapping = {"<root@example.org>": "<" + "a" * 32 + "@unseenet>"}
    c = conv(raw(h), id_map=mapping.get)
    assert c.article["References"] == "<" + "a" * 32 + "@unseenet> <x@unseenet>"
    assert c.article["Usenet-References"] == "<root@example.org> <mid@example.org> <x@unseenet>"
    assert c.references_unmapped


def test_no_archive_gets_expires():
    c = conv(raw(BASIC + "X-No-Archive: Yes\n"))
    assert c.article["Expires"] == "Sat, 24 Oct 2026 12:00:00 +0000"


@pytest.mark.parametrize("extra,reason", [
    ("Control: cancel <abc@example.org>\n", "control"),
    ("", "loop"),
])
def test_never_imported(extra, reason):
    h = BASIC + extra
    if reason == "loop":
        h = h.replace("<abc123@example.org>", "<" + "b" * 32 + "@unseenet>")
    with pytest.raises(Skip) as e:
        conv(raw(h))
    assert e.value.reason == reason


def test_cmsg_subject_is_control():
    with pytest.raises(Skip) as e:
        conv(raw(BASIC.replace("Subject: Engines", "Subject: cmsg newgroup alt.foo")))
    assert e.value.reason == "control"


def test_group_not_carried():
    with pytest.raises(Skip) as e:
        conv(raw(BASIC.replace("comp.misc", "alt.binaries.foo")))
    assert e.value.reason == "group"


def test_yenc_and_uuencode_are_binary():
    for body in ("=ybegin line=128 size=10 name=x.bin\nxxx\n=yend\n",
                 "see attached\nbegin 644 x.bin\nM9F]O\nend\n"):
        with pytest.raises(Skip) as e:
            conv(raw(BASIC, body))
        assert e.value.reason == "binary"


def test_multipart_keeps_first_text_and_rejects_attachments():
    mp = BASIC + ('MIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="B"\n')
    body = ("--B\nContent-Type: text/plain; charset=utf-8\n\nplain text\n"
            "--B\nContent-Type: text/html\n\n<p>html</p>\n--B--\n")
    assert conv(raw(mp, body)).article.body == "plain text\n"
    att = body.replace("text/html", "image/png")
    with pytest.raises(Skip) as e:
        conv(raw(mp.replace("alternative", "mixed"), att))
    assert e.value.reason == "binary"


def test_too_large():
    with pytest.raises(Skip) as e:
        conv(raw(BASIC, "x" * 70 + "\n" * 1 + ("y" * 79 + "\n") * 820))
    assert e.value.reason == "size"


def test_long_and_odd_headers_are_tamed():
    h = BASIC.replace("Subject: Engines", "Subject: " + "s" * 400) \
             .replace("From: Ada Lovelace <ada@example.org>", "From: ada@example.org")
    art = conv(raw(h)).article
    assert len(art["Subject"]) == 256
    assert art["From"] == "ada"


def _eight_bit(header_from: bytes, body: bytes = b"Hello.\r\n") -> bytes:
    """An article with raw 8-bit octets and no MIME encoding, as some readers send (#55)."""
    return (b"From: " + header_from + b"\r\nNewsgroups: comp.misc\r\nSubject: Eight bit \xe6\xf8\xe5\r\n"
            b"Date: Fri, 16 Oct 2026 07:33:05 +0000\r\nMessage-ID: <eight@example.org>\r\n"
            b"Content-Type: text/plain; charset=us-ascii\r\n\r\n" + body)


def test_raw_eight_bit_headers_become_text_and_the_article_can_be_written():
    c = conv(_eight_bit(b"\x86 Exocet <x@example.org>", b"Bad \x86 body\r\n"))
    art = c.article
    art.finalize(GW, 0)
    data = art.to_bytes()                       # no surrogates left: it can be posted
    validate_structure(Article.from_bytes(data), raw_size=len(data))
    assert art["From"].startswith("† Exocet")          # 0x86 in Windows-1252
    assert art["Subject"] == "Eight bit æøå"  # Latin-1 letters, as sent
    assert "†" in art.get("Origin-From", "")


def test_raw_utf8_headers_stay_utf8():
    c = conv(_eight_bit("Søren <s@example.org>".encode()))
    assert c.article["From"].startswith("Søren")
