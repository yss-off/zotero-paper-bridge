"""Tests for identifier, URL, arXiv, IEEE, and BibTeX parsing."""

from pathlib import Path

from paper_bridge.common.identifiers import (
    extract_ieee_arnumber,
    load_identifiers_from_file,
    normalize_doi,
    parse_bibtex_identifiers,
    parse_identifier,
)


def test_parse_doi_variants() -> None:
    for raw in [
        "10.1109/TWC.2024.1234567",
        "doi:10.1109/TWC.2024.1234567",
        "https://doi.org/10.1109/TWC.2024.1234567.",
        "http://dx.doi.org/10.1109/TWC.2024.1234567",
    ]:
        parsed = parse_identifier(raw)
        assert parsed.kind == "doi"
        assert parsed.doi == "10.1109/twc.2024.1234567"
        assert parsed.normalized == "doi:10.1109/twc.2024.1234567"


def test_parse_ieee_urls() -> None:
    urls = [
        "https://ieeexplore.ieee.org/document/10123456",
        "https://ieeexplore.ieee.org/abstract/document/10123456/",
        "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=10123456",
        "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=10123456&ref=",
    ]
    for u in urls:
        assert extract_ieee_arnumber(u) == "10123456"
        parsed = parse_identifier(u)
        assert parsed.kind == "ieee_url"
        assert parsed.ieee_arnumber == "10123456"
        assert parsed.url == "https://ieeexplore.ieee.org/document/10123456"


def test_parse_arxiv_variants() -> None:
    for raw in [
        "2401.12345",
        "arXiv:2401.12345v2",
        "https://arxiv.org/abs/2401.12345v2",
        "https://arxiv.org/pdf/2401.12345v2.pdf",
        "10.48550/arXiv.2401.12345",
    ]:
        parsed = parse_identifier(raw)
        assert parsed.kind == "arxiv"
        assert parsed.normalized == "arxiv:2401.12345"
        assert parsed.arxiv_id is not None and parsed.arxiv_id.startswith("2401.12345")


def test_parse_title() -> None:
    parsed = parse_identifier("Wideband Semantic Spectrum Segmentation with Transformer Queries")
    assert parsed.kind == "title"
    assert parsed.title == "Wideband Semantic Spectrum Segmentation with Transformer Queries"


def test_parse_bibtex_priority_and_batch_file(tmp_path: Path) -> None:
    bib = """
    @comment{ignored entry}
    @article{paper1,
      title = {{Wideband Spectrum Sensing}},
      doi = {10.1109/TWC.2024.1111111},
      url = {https://ieeexplore.ieee.org/document/9999999}
    }
    @inproceedings{paper2,
      title = "RF Signal Detection",
      url = "https://ieeexplore.ieee.org/document/8888888"
    }
    @misc{paper3,
      title = {Preprint Paper},
      eprint = {2405.00123},
      archivePrefix = {arXiv}
    }
    @article{paper4,
      title = {Title Only Fallback Paper}
    }
    """
    ids = parse_bibtex_identifiers(bib)
    assert ids == [
        "10.1109/twc.2024.1111111",
        "https://ieeexplore.ieee.org/document/8888888",
        "arXiv:2405.00123",
        "Title Only Fallback Paper",
    ]

    bib_file = tmp_path / "refs.bib"
    bib_file.write_text(bib, encoding="utf-8")
    assert load_identifiers_from_file(bib_file) == ids

    txt_file = tmp_path / "list.txt"
    txt_file.write_text("# comment\n\n10.1109/TWC.2024.2222222\n2401.12345\n", encoding="utf-8")
    assert load_identifiers_from_file(txt_file) == ["10.1109/TWC.2024.2222222", "2401.12345"]
    assert normalize_doi("DOI:10.1109/ABC.1.") == "10.1109/abc.1"
