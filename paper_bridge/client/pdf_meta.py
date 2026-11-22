"""Pure-stdlib PDF DOI and basic metadata extractor (XMP XML, /Info dict, and FlateDecode streams)."""

from __future__ import annotations

import html
import re
import zlib
from dataclasses import dataclass, field

from ..common.identifiers import normalize_doi
from ..common.models import Author

DOI_BYTES_RE = re.compile(rb"10\.\d{4,9}/[A-Za-z0-9._;()/:+-]+")
TJ_KERN_RE = re.compile(rb"\)\s*-?\d+(?:\.\d+)?\s*\(")
STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


@dataclass
class ExtractedPdfMetadata:
    doi: str | None = None
    title: str = ""
    creators: list[Author] = field(default_factory=list)


def _clean_doi_candidate(raw: str) -> str | None:
    cleaned = raw.strip().rstrip(".,;)]}>\"'")
    # Trim unbalanced trailing parenthesis if any
    while cleaned.endswith(")") and cleaned.count(")") > cleaned.count("("):
        cleaned = cleaned[:-1]
    if "/" not in cleaned:
        return None
    prefix, suffix = cleaned.split("/", 1)
    if not suffix or len(suffix) < 2:
        return None
    if not re.fullmatch(r"10\.\d{4,9}", prefix):
        return None
    return normalize_doi(cleaned)


def _decode_pdf_literal_string(raw: bytes) -> str:
    # Unescape basic PDF literal escapes
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1]
            mapping = {
                ord("n"): 0x0A,
                ord("r"): 0x0D,
                ord("t"): 0x09,
                ord("b"): 0x08,
                ord("f"): 0x0C,
                ord("("): ord("("),
                ord(")"): ord(")"),
                ord("\\"): ord("\\"),
            }
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
        out.append(b)
        i += 1
    data = bytes(out)
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace").strip("\x00 ")
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace").strip("\x00 ")
    return data.decode("utf-8", errors="replace").strip("\x00 ")


def _parse_author_string(author_str: str) -> list[Author]:
    cleaned = " ".join(author_str.split()).strip()
    if not cleaned:
        return []
    parts = [p.strip() for p in re.split(r";|\band\b", cleaned) if p.strip()]
    if len(parts) == 1 and "," in cleaned:
        # Could be "Last, First" or "A. Smith, B. Jones"
        comma_parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        if len(comma_parts) == 2 and " " not in comma_parts[0]:
            return [Author(first_name=comma_parts[1], last_name=comma_parts[0])]
        parts = comma_parts

    authors: list[Author] = []
    for p in parts:
        if "," in p:
            last, first = [x.strip() for x in p.split(",", 1)]
            authors.append(Author(first_name=first, last_name=last))
        else:
            words = p.rsplit(" ", 1)
            if len(words) == 2:
                authors.append(Author(first_name=words[0], last_name=words[1]))
            else:
                authors.append(Author(name=p))
    return authors


def _extract_xmp_fields(pdf_bytes: bytes) -> tuple[str | None, str, list[Author]]:
    start = pdf_bytes.find(b"<x:xmpmeta")
    if start == -1:
        start = pdf_bytes.find(b"<rdf:RDF")
    if start == -1:
        return None, "", []
    end = pdf_bytes.find(b"</x:xmpmeta>", start)
    if end == -1:
        end = pdf_bytes.find(b"</rdf:RDF>", start)
    if end == -1:
        end = min(len(pdf_bytes), start + 65536)
    xmp = pdf_bytes[start : end + 16].decode("utf-8", errors="replace")

    doi: str | None = None
    for pat in (
        r"<prism:doi[^>]*>([^<]+)</prism:doi>",
        r"<pdfx:doi[^>]*>([^<]+)</pdfx:doi>",
        r'prism:doi="([^"]+)"',
        r'pdfx:doi="([^"]+)"',
        r"<dc:identifier[^>]*>([^<]*10\.\d{4,9}/[^<]+)</dc:identifier>",
    ):
        m = re.search(pat, xmp, re.IGNORECASE)
        if m:
            m_doi = re.search(r"10\.\d{4,9}/[^\s<\"']+", html.unescape(m.group(1)))
            if m_doi:
                doi = _clean_doi_candidate(m_doi.group(0))
                if doi:
                    break

    title = ""
    m_title_block = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", xmp, re.IGNORECASE | re.DOTALL)
    if m_title_block:
        m_li = re.search(r"<rdf:li[^>]*>(.*?)</rdf:li>", m_title_block.group(1), re.IGNORECASE | re.DOTALL)
        raw_t = m_li.group(1) if m_li else m_title_block.group(1)
        title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw_t)).split())

    creators: list[Author] = []
    m_creator_block = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", xmp, re.IGNORECASE | re.DOTALL)
    if m_creator_block:
        for li in re.findall(r"<rdf:li[^>]*>(.*?)</rdf:li>", m_creator_block.group(1), re.IGNORECASE | re.DOTALL):
            clean_name = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", li)).split())
            if clean_name:
                creators.extend(_parse_author_string(clean_name))

    return doi, title, creators


def _extract_info_dict_fields(pdf_bytes: bytes) -> tuple[str | None, str, list[Author]]:
    # Scan header + trailer region (where /Info dict lives in nearly all PDFs)
    regions = [pdf_bytes[:200_000]]
    if len(pdf_bytes) > 200_000:
        regions.append(pdf_bytes[-200_000:])
    blob = b"\n".join(regions)

    def _find_field(name: bytes) -> str:
        m = re.search(rb"/" + name + rb"\s*\(([^()]{1,1000})\)", blob)
        if not m:
            return ""
        return " ".join(_decode_pdf_literal_string(m.group(1)).split())

    title = _find_field(b"Title")
    author = _find_field(b"Author")
    subject = _find_field(b"Subject")

    doi: str | None = None
    for text in (subject, title):
        m = re.search(r"10\.\d{4,9}/[^\s,;\"']+", text)
        if m:
            doi = _clean_doi_candidate(m.group(0))
            if doi:
                break

    return doi, title, _parse_author_string(author) if author else []


def _choose_best_doi(candidates: list[str], preferred_arnumber: str | None) -> str | None:
    if not candidates:
        return None
    if preferred_arnumber:
        for c in candidates:
            if c.endswith(f".{preferred_arnumber}") or c.endswith(f"/{preferred_arnumber}"):
                return c
        for c in candidates:
            if c.startswith("10.1109/") and preferred_arnumber in c:
                return c
        for c in candidates:
            if c.startswith("10.1109/"):
                return c
    return candidates[0]


def extract_pdf_metadata(
    pdf_bytes: bytes,
    *,
    preferred_arnumber: str | None = None,
    max_streams: int = 20,
) -> ExtractedPdfMetadata:
    """Extract DOI, title, and authors directly from PDF bytes using Python stdlib only."""
    if not pdf_bytes:
        return ExtractedPdfMetadata()

    xmp_doi, xmp_title, xmp_creators = _extract_xmp_fields(pdf_bytes)
    info_doi, info_title, info_creators = _extract_info_dict_fields(pdf_bytes)

    candidates: list[str] = []
    for d in (xmp_doi, info_doi):
        if d and d not in candidates:
            candidates.append(d)

    # Scan early uncompressed regions (header / XMP / annotations / URI links)
    for m in DOI_BYTES_RE.finditer(pdf_bytes[:300_000]):
        d = _clean_doi_candidate(m.group(0).decode("latin-1", errors="ignore"))
        if d and d not in candidates:
            candidates.append(d)

    # Decompress first N PDF streams (covers page 1 text & IEEE footer "Digital Object Identifier 10.1109/...")
    for idx, m in enumerate(STREAM_RE.finditer(pdf_bytes)):
        if idx >= max_streams:
            break
        raw_stream = m.group(1)
        try:
            dec = zlib.decompress(raw_stream)
        except zlib.error:
            dec = raw_stream
        # Collapse PDF TJ kerning splits like [(10.1109/T) -15 (WC.2024.123)] TJ
        joined = TJ_KERN_RE.sub(b"", dec)
        for dm in DOI_BYTES_RE.finditer(joined):
            d = _clean_doi_candidate(dm.group(0).decode("latin-1", errors="ignore"))
            if d and d not in candidates:
                candidates.append(d)

    doi = _choose_best_doi(candidates, preferred_arnumber)
    title = xmp_title or info_title
    # Discard generic/placeholder PDF titles like "Microsoft Word - ..." or bare filenames
    if title and (
        title.lower().endswith((".pdf", ".doc", ".docx", ".dvi", ".ps"))
        or title.lower().startswith("microsoft word")
    ):
        title = ""
    creators = xmp_creators or info_creators
    return ExtractedPdfMetadata(doi=doi, title=title, creators=creators)
