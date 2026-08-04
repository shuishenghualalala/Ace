"""Secure XML parsing helpers for Office document processing.

Provides lxml and defusedxml.ElementTree parsers configured to resist
common XML attacks (XXE, DTD-based denial of service, and entity expansion).
"""

import io
import re
from pathlib import Path

import lxml.etree
from defusedxml import ElementTree as DefusedET


_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)


def _raise_if_malicious_dtd(data: bytes) -> None:
    """Reject Office Open XML parts that contain DTD/ENTITY declarations.

    OOXML documents are not allowed to contain DTDs. Any occurrence of
    <!DOCTYPE or <!ENTITY (outside of a comment) indicates a malformed or
    potentially malicious file.
    """
    # Ignore XML comments so that innocuous comment text does not trigger a
    # false positive.
    lowered = _COMMENT_RE.sub(b"", data).lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise lxml.etree.XMLSyntaxError(
            "DTD/ENTITY declarations are not allowed in Office Open XML files",
            0,
            1,
            1,
        )


def secure_lxml_parser() -> lxml.etree.XMLParser:
    """Return an lxml parser suitable for untrusted Office document XML.

    External entities, DTD loading, and network access are disabled.
    """
    return lxml.etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
    )


def xsd_parser() -> lxml.etree.XMLParser:
    """Return an lxml parser for trusted local XSD schema files.

    Network access is left enabled so that standard schema imports can be
    resolved, but entity resolution and DTD loading are still disabled.
    """
    return lxml.etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        recover=False,
    )


def raise_if_malicious_dtd(path: str | Path) -> None:
    """Scan an XML file for DTD/ENTITY declarations and raise if found."""
    with open(path, "rb") as f:
        _raise_if_malicious_dtd(f.read())


def lxml_parse(path: str | Path) -> lxml.etree._ElementTree:
    """Parse an XML file with the secure (untrusted-data) lxml parser."""
    with open(path, "rb") as f:
        data = f.read()
        _raise_if_malicious_dtd(data)
        f.seek(0)
        return lxml.etree.parse(f, parser=secure_lxml_parser())


def lxml_fromstring(text: str | bytes) -> lxml.etree._Element:
    """Parse XML from a string/bytes with the secure lxml parser."""
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    _raise_if_malicious_dtd(data)
    return lxml.etree.fromstring(text, parser=secure_lxml_parser())
