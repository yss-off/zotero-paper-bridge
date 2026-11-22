"""Parsers for DOIs, IEEE URLs/arnumbers, arXiv IDs, BibTeX entries, and batch files."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .models import ParsedIdentifier

DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>\]\)]+)", re.IGNORECASE)
ARXIV_ID_RE = re.compile(
    r"(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)?((?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)(?:\.pdf)?",
    re.IGNORECASE,
)
IEEE_DOC_RE = re.compile(
    r"ieeexplore\.ieee\.org/(?:document|abstract/document)/(\d+)", re.IGNORECASE
)
IEEE_STAMP_RE = re.compile(
    r"ieeexplore\.ieee\.org/(?:stamp/stamp\.jsp|stampPDF/getPDF\.jsp)", re.IGNORECASE
)


def normalize_doi(doi: str) -> str:
    cleaned = doi.strip()
    cleaned = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^doi:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.rstrip(".,;)")
    return cleaned.lower()


def normalize_title(title: str) -> str:
    lowered = title.lower()
    lowered = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", lowered)
    return " ".join(lowered.split())


def extract_ieee_arnumber(text: str) -> str | None:
    m = IEEE_DOC_RE.search(text)
    if m:
        return m.group(1)
    if IEEE_STAMP_RE.search(text):
        parsed = urlparse(text)
        qs = parse_qs(parsed.query)
        vals = qs.get("arnumber")
        if vals and vals[0].isdigit():
            return vals[0]
    m_param = re.search(r"[?&]arnumber=(\d+)", text)
    if m_param and "ieee.org" in text.lower():
        return m_param.group(1)
    return None


def parse_identifier(raw: str) -> ParsedIdentifier:
    text = raw.strip()
    if not text:
        raise ValueError("Identifier cannot be empty")

    # 1. IEEE Xplore URL
    arnumber = extract_ieee_arnumber(text)
    if arnumber:
        url = f"https://ieeexplore.ieee.org/document/{arnumber}"
        return ParsedIdentifier(
            raw=text,
            kind="ieee_url",
            normalized=f"ieee:{arnumber}",
            ieee_arnumber=arnumber,
            url=url,
        )

    # 2. arXiv URL or explicit arXiv ID (check before generic URL/DOI)
    lower = text.lower()
    if lower.startswith("arxiv:") or "arxiv.org/" in lower or re.fullmatch(
        r"\d{4}\.\d{4,5}(?:v\d+)?", text
    ):
        m = ARXIV_ID_RE.search(text)
        if m:
            aid = m.group(1)
            base_id = re.sub(r"v\d+$", "", aid, flags=re.IGNORECASE)
            return ParsedIdentifier(
                raw=text,
                kind="arxiv",
                normalized=f"arxiv:{base_id.lower()}",
                arxiv_id=aid,
                doi=f"10.48550/arxiv.{base_id.lower()}" if re.match(r"^\d{4}\.\d{4,5}$", base_id) else None,
                url=f"https://arxiv.org/abs/{aid}",
            )

    # 3. DOI (bare, doi:, or https://doi.org/...)
    if lower.startswith(("10.", "doi:", "https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")):
        m = DOI_RE.search(text)
        if m:
            doi = normalize_doi(m.group(1))
            if doi.startswith("10.48550/arxiv."):
                aid = doi.split("10.48550/arxiv.", 1)[1]
                return ParsedIdentifier(
                    raw=text,
                    kind="arxiv",
                    normalized=f"arxiv:{aid}",
                    arxiv_id=aid,
                    doi=doi,
                    url=f"https://arxiv.org/abs/{aid}",
                )
            return ParsedIdentifier(
                raw=text,
                kind="doi",
                normalized=f"doi:{doi}",
                doi=doi,
                url=f"https://doi.org/{doi}",
            )

    # 4. Generic HTTP(S) URL (may still contain an embedded DOI)
    if lower.startswith(("http://", "https://")):
        m_doi = DOI_RE.search(text)
        doi = normalize_doi(m_doi.group(1)) if m_doi else None
        return ParsedIdentifier(
            raw=text,
            kind="url",
            normalized=f"doi:{doi}" if doi else text,
            doi=doi,
            url=text,
        )

    # 5. Embedded DOI inside citation text
    m_doi = DOI_RE.search(text)
    if m_doi:
        doi = normalize_doi(m_doi.group(1))
        return ParsedIdentifier(
            raw=text,
            kind="doi",
            normalized=f"doi:{doi}",
            doi=doi,
            url=f"https://doi.org/{doi}",
        )

    # 6. Plain title
    return ParsedIdentifier(
        raw=text,
        kind="title",
        normalized=f"title:{normalize_title(text)}",
        title=text,
    )


def _extract_bib_field(entry: str, field_name: str) -> str | None:
    pattern = re.compile(
        rf"\b{field_name}\s*=\s*(?:\{{([^{{}}]*(?:\{{[^{{}}]*\}}[^{{}}]*)*)\}}|\"([^\"]*)\"|([^\s,}}]+))",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(entry)
    if not m:
        return None
    val = m.group(1) or m.group(2) or m.group(3) or ""
    val = re.sub(r"[{}]", "", val)
    return " ".join(val.split()).strip() or None


def parse_bibtex_identifiers(bibtex_text: str) -> list[str]:
    """Extract the highest-signal identifier (DOI > IEEE URL > arXiv > URL > title) per BibTeX entry."""
    entries = re.split(r"(?=@\w+\s*\{)", bibtex_text)
    results: list[str] = []
    for entry in entries:
        entry = entry.strip()
        if not entry.startswith("@"):
            continue
        if entry.lower().startswith(("@comment", "@preamble", "@string")):
            continue
        doi = _extract_bib_field(entry, "doi")
        url = _extract_bib_field(entry, "url")
        eprint = _extract_bib_field(entry, "eprint")
        archive_prefix = (_extract_bib_field(entry, "archiveprefix") or "").lower()
        title = _extract_bib_field(entry, "title")

        if doi:
            results.append(normalize_doi(doi))
        elif url and extract_ieee_arnumber(url):
            results.append(url)
        elif eprint and (archive_prefix == "arxiv" or re.match(r"^\d{4}\.\d{4,5}", eprint)):
            results.append(f"arXiv:{eprint}")
        elif url:
            results.append(url)
        elif title:
            results.append(title)
    return results


def load_identifiers_from_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".bib" or "@" in text and re.search(r"@\w+\s*\{", text):
        return parse_bibtex_identifiers(text)
    identifiers: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        identifiers.append(cleaned)
    return identifiers
