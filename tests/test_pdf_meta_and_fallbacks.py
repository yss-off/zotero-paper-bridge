"""Tests for PDF DOI/XMP extraction, arXiv 429 HTML fallback, and post-PDF Crossref enrichment."""

from __future__ import annotations

import zlib
from pathlib import Path

from test_zotero_and_proxy import MockZoteroState, start_mock_zotero

from paper_bridge.client.config import ClientHelperConfig
from paper_bridge.client.engine import PaperBridgeEngine
from paper_bridge.client.fetcher import FetchOutcome
from paper_bridge.client.metadata import MetadataResolver
from paper_bridge.client.pdf_meta import extract_pdf_metadata
from paper_bridge.client.zotero_local import ZoteroLocalClient
from paper_bridge.common.models import Author, IngestRequest, PaperMetadata, ParsedIdentifier


def _make_flate_pdf(stream_text: bytes, *, extra_header: bytes = b"") -> bytes:
    compressed = zlib.compress(stream_text)
    body = (
        b"%PDF-1.4\n"
        + extra_header
        + b"\n1 0 obj << /Filter /FlateDecode >>\nstream\n"
        + compressed
        + b"\nendstream\nendobj\n%%EOF\n"
    )
    if len(body) < 1200:
        body += b" " * (1200 - len(body))
    return body


def test_extract_pdf_metadata_prefers_matching_ieee_arnumber_and_handles_tj_kerning() -> None:
    stream = (
        b"BT /F1 9 Tf (Ref [1] DOI: 10.1109/OTHER.2020.1111111) Tj "
        b"[(Digital Object Identifier 10.1109/T) -12 (WC.2022.9928262)] TJ ET"
    )
    pdf = _make_flate_pdf(stream)
    extracted = extract_pdf_metadata(pdf, preferred_arnumber="9928262")
    assert extracted.doi == "10.1109/twc.2022.9928262"


def test_extract_pdf_metadata_from_xmp_and_info_dict() -> None:
    xmp = (
        b"<x:xmpmeta><rdf:RDF>"
        b"<prism:doi>10.1109/JSAC.2025.7777777</prism:doi>"
        b"<dc:title><rdf:Alt><rdf:li>Semantic Spectrum Sensing</rdf:li></rdf:Alt></dc:title>"
        b"<dc:creator><rdf:Seq><rdf:li>Jane Doe</rdf:li><rdf:li>Smith, John</rdf:li></rdf:Seq></dc:creator>"
        b"</rdf:RDF></x:xmpmeta>"
    )
    pdf = _make_flate_pdf(b"BT (Hello) Tj ET", extra_header=xmp)
    extracted = extract_pdf_metadata(pdf)
    assert extracted.doi == "10.1109/jsac.2025.7777777"
    assert extracted.title == "Semantic Spectrum Sensing"
    assert [(a.first_name, a.last_name) for a in extracted.creators] == [
        ("Jane", "Doe"),
        ("John", "Smith"),
    ]

    info_pdf = _make_flate_pdf(
        b"BT (No DOI here) Tj ET",
        extra_header=b"2 0 obj << /Title (Fallback Info Title) /Author (Alice Lin; Bob Zhou) >> endobj",
    )
    extracted2 = extract_pdf_metadata(info_pdf)
    assert extracted2.doi is None
    assert extracted2.title == "Fallback Info Title"
    assert len(extracted2.creators) == 2


def test_arxiv_resolver_falls_back_to_abs_html_on_429() -> None:
    sample_abs_html = """<!doctype html><html><head>
    <meta name="citation_title" content="The Era of 1-bit LLMs: All Large Language Models are in 1.58 Bits" />
    <meta name="citation_author" content="Ma, Shuming" />
    <meta name="citation_author" content="Wei, Furu" />
    <meta name="citation_date" content="2024/02/27" />
    <meta name="citation_arxiv_id" content="2402.17764" />
    </head><body>
    <blockquote class="abstract mathjax"><span class="descriptor">Abstract:</span>Recent research on 1-bit LLMs.</blockquote>
    </body></html>""".encode("utf-8")

    class StubMetadataResolver(MetadataResolver):
        def _http_get(self, url: str, *, accept: str = "application/json") -> tuple[int, bytes, str]:
            if "export.arxiv.org" in url:
                return 429, b"Rate limited", url
            if "arxiv.org/abs/2402.17764" in url:
                return 200, sample_abs_html, url
            return 404, b"", url

    resolver = StubMetadataResolver()
    meta = resolver.resolve(
        ParsedIdentifier(raw="2402.17764", kind="arxiv", normalized="arxiv:2402.17764", arxiv_id="2402.17764")
    )
    assert meta.title == "The Era of 1-bit LLMs: All Large Language Models are in 1.58 Bits"
    assert meta.doi == "10.48550/arxiv.2402.17764"
    assert meta.date == "2024-02-27"
    assert [(a.first_name, a.last_name) for a in meta.creators] == [("Shuming", "Ma"), ("Furu", "Wei")]
    assert "1-bit LLMs" in meta.abstract_note


def test_engine_enriches_sparse_ieee_metadata_from_downloaded_pdf(tmp_path: Path) -> None:
    state = MockZoteroState()
    srv, base_url = start_mock_zotero(state)
    try:
        cfg = ClientHelperConfig.load(tmp_path / "cfg")
        cfg.zotero_base_url = base_url

        ieee_pdf = _make_flate_pdf(b"BT (Digital Object Identifier 10.1109/TWC.2022.9928262) Tj ET")

        class SparseIeeeResolver:
            def resolve(self, parsed: ParsedIdentifier) -> PaperMetadata:
                # Simulates IEEE /document/9928262 returning HTTP 202 before PDF download
                return PaperMetadata(
                    title="IEEE Document 9928262",
                    item_type="journalArticle",
                    ieee_arnumber="9928262",
                    url="https://ieeexplore.ieee.org/document/9928262",
                    source="ieee",
                )

            def resolve_by_doi(self, doi: str) -> PaperMetadata | None:
                assert doi == "10.1109/twc.2022.9928262"
                return PaperMetadata(
                    title="Deep Learning-Based Wideband Spectrum Sensing",
                    item_type="journalArticle",
                    creators=[Author(first_name="Wei", last_name="Zhang")],
                    doi="10.1109/twc.2022.9928262",
                    publication_title="IEEE Transactions on Wireless Communications",
                    date="2022-10",
                    source="ieee",
                )

        class DirectIeeeFetcher:
            def fetch(self, meta: PaperMetadata, *, skip_silent_ieee: bool = False) -> FetchOutcome:
                return FetchOutcome(
                    ok=True,
                    pdf_bytes=ieee_pdf,
                    pdf_url="https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?arnumber=9928262",
                    filename="IEEE Document 9928262.pdf",
                    source="ieee_campus",
                )

        engine = PaperBridgeEngine(
            cfg,
            zotero=ZoteroLocalClient(base_url),
            resolver=SparseIeeeResolver(),  # type: ignore[arg-type]
            fetcher=DirectIeeeFetcher(),  # type: ignore[arg-type]
        )
        res, _ = engine.ingest_one(
            IngestRequest(
                identifier="https://ieeexplore.ieee.org/document/9928262",
                collection="Agent-Inbox",
            )
        )
        assert res.status == "saved"
        assert res.title == "Deep Learning-Based Wideband Spectrum Sensing"
        assert res.doi == "10.1109/twc.2022.9928262"
        assert res.pdf_attached is True
        assert res.item_key is not None
        saved_data = state.items[res.item_key]["data"]
        assert saved_data["title"] == "Deep Learning-Based Wideband Spectrum Sensing"
        assert saved_data["DOI"] == "10.1109/twc.2022.9928262"
        assert state.attachments_received[-1]["meta"]["title"] == "Deep Learning-Based Wideband Spectrum Sensing.pdf"
    finally:
        srv.shutdown()
        srv.server_close()
