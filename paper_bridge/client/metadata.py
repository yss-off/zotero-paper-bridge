"""Bibliographic metadata resolvers (Crossref, arXiv Atom API, OpenAlex, Unpaywall, IEEE)."""

from __future__ import annotations

import difflib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from ..common.identifiers import extract_ieee_arnumber, normalize_doi, normalize_title
from ..common.models import Author, PaperMetadata, ParsedIdentifier
from ..common.security import build_ssl_context


class AmbiguousTitleError(RuntimeError):
    """Raised when a plain-title search does not have a single high-confidence match."""

    def __init__(self, query: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(f"Ambiguous title match for: {query}")
        self.query = query
        self.candidates = candidates


def title_similarity(a: str, b: str) -> float:
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    ta = set(na.split())
    tb = set(nb.split())
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    return 0.65 * seq + 0.35 * jaccard


class MetadataResolver:
    def __init__(self, *, polite_email: str = "", timeout: float = 20.0) -> None:
        self.polite_email = polite_email.strip()
        self.timeout = timeout
        self._ssl_context = build_ssl_context()

    def _user_agent(self) -> str:
        if self.polite_email:
            return f"zotero-paper-bridge/0.1 (mailto:{self.polite_email})"
        return "zotero-paper-bridge/0.1 (academic-research-workflow)"

    def _http_get(self, url: str, *, accept: str = "application/json") -> tuple[int, bytes, str]:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self._user_agent(),
                "Accept": accept,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_context) as resp:
                return resp.status, resp.read(), resp.geturl()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), url
        except (urllib.error.URLError, OSError):
            return 0, b"", url

    def resolve(self, parsed: ParsedIdentifier) -> PaperMetadata:
        if parsed.kind == "arxiv" and parsed.arxiv_id:
            return self._resolve_arxiv(parsed.arxiv_id)

        if parsed.kind == "ieee_url" and parsed.ieee_arnumber:
            return self._resolve_ieee_arnumber(parsed.ieee_arnumber)

        if parsed.doi:
            meta = self._resolve_doi(parsed.doi)
            if meta is not None:
                return meta

        if parsed.kind == "url" and parsed.url:
            return PaperMetadata(
                title=parsed.url,
                item_type="journalArticle",
                url=parsed.url,
                source="url",
            )

        if parsed.kind == "title" and parsed.title:
            return self._resolve_title(parsed.title)

        return PaperMetadata(title=parsed.raw, source="raw")

    def resolve_by_doi(self, doi: str) -> PaperMetadata | None:
        return self._resolve_doi(doi)

    def _parse_arxiv_abs_html(self, clean_id: str, base_id: str) -> PaperMetadata | None:
        abs_url = f"https://arxiv.org/abs/{clean_id}"
        status, raw, _ = self._http_get(abs_url, accept="text/html")
        if status != 200 or not raw:
            return None
        body = raw.decode("utf-8", errors="replace")

        def _meta_all(name: str) -> list[str]:
            pat = re.compile(
                rf'<meta\s+[^>]*name=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)["\']',
                re.IGNORECASE,
            )
            vals = [html.unescape(v).strip() for v in pat.findall(body) if v.strip()]
            if vals:
                return vals
            pat_rev = re.compile(
                rf'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']{re.escape(name)}["\']',
                re.IGNORECASE,
            )
            return [html.unescape(v).strip() for v in pat_rev.findall(body) if v.strip()]

        titles = _meta_all("citation_title")
        if not titles:
            return None
        title = " ".join(titles[0].split())

        creators: list[Author] = []
        for raw_author in _meta_all("citation_author"):
            if "," in raw_author:
                last, first = [p.strip() for p in raw_author.split(",", 1)]
                creators.append(Author(first_name=first, last_name=last))
            else:
                parts = raw_author.rsplit(" ", 1)
                if len(parts) == 2:
                    creators.append(Author(first_name=parts[0], last_name=parts[1]))
                else:
                    creators.append(Author(name=raw_author))

        dates = _meta_all("citation_date") or _meta_all("citation_online_date")
        date_str = dates[0].replace("/", "-") if dates else ""
        dois = _meta_all("citation_doi")
        doi = normalize_doi(dois[0]) if dois else (
            f"10.48550/arxiv.{base_id.lower()}" if re.match(r"^\d{4}\.\d{4,5}$", base_id) else None
        )

        abstract = ""
        abs_metas = _meta_all("citation_abstract")
        if abs_metas:
            abstract = " ".join(abs_metas[0].split())
        else:
            m_abs = re.search(
                r'<blockquote[^>]+class="[^"]*abstract[^"]*"[^>]*>(.*?)</blockquote>',
                body,
                re.IGNORECASE | re.DOTALL,
            )
            if m_abs:
                cleaned_abs = re.sub(r"<[^>]+>", " ", m_abs.group(1))
                cleaned_abs = re.sub(r"^\s*Abstract:\s*", "", html.unescape(cleaned_abs), flags=re.IGNORECASE)
                abstract = " ".join(cleaned_abs.split())

        return PaperMetadata(
            title=title,
            item_type="preprint",
            creators=creators,
            doi=doi,
            arxiv_id=clean_id,
            url=abs_url,
            pdf_url=f"https://arxiv.org/pdf/{clean_id}.pdf",
            publication_title="arXiv",
            date=date_str,
            abstract_note=abstract,
            source="arxiv",
        )

    def _resolve_arxiv_via_openalex(self, clean_id: str, base_id: str) -> PaperMetadata | None:
        if not re.match(r"^\d{4}\.\d{4,5}$", base_id):
            return None
        doi = f"10.48550/arxiv.{base_id.lower()}"
        encoded = urllib.parse.quote(f"https://doi.org/{doi}", safe="")
        status, raw, _ = self._http_get(f"https://api.openalex.org/works/{encoded}")
        if status != 200 or not raw:
            return None
        try:
            work = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        if not isinstance(work, dict) or not work.get("title"):
            return None
        creators: list[Author] = []
        for auth in work.get("authorships") or []:
            name = str(((auth or {}).get("author") or {}).get("display_name") or "").strip()
            if not name:
                continue
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                creators.append(Author(first_name=parts[0], last_name=parts[1]))
            else:
                creators.append(Author(name=name))
        return PaperMetadata(
            title=" ".join(str(work["title"]).split()),
            item_type="preprint",
            creators=creators,
            doi=doi,
            arxiv_id=clean_id,
            url=f"https://arxiv.org/abs/{clean_id}",
            pdf_url=f"https://arxiv.org/pdf/{clean_id}.pdf",
            publication_title="arXiv",
            date=str(work.get("publication_date") or work.get("publication_year") or ""),
            source="arxiv",
        )

    def _resolve_arxiv(self, arxiv_id: str) -> PaperMetadata:
        clean_id = re.sub(r"^arxiv:", "", arxiv_id, flags=re.IGNORECASE).strip()
        base_id = re.sub(r"v\d+$", "", clean_id, flags=re.IGNORECASE)
        default_doi = (
            f"10.48550/arxiv.{base_id.lower()}" if re.match(r"^\d{4}\.\d{4,5}$", base_id) else None
        )

        # 1. Official arXiv Atom API
        url = f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(clean_id)}"
        status, raw, _ = self._http_get(url, accept="application/atom+xml")
        if status == 200 and raw:
            try:
                root = ET.fromstring(raw)
                ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
                entry = root.find("atom:entry", ns)
                if entry is not None:
                    title = " ".join((entry.findtext("atom:title", "", ns) or "").split())
                    summary = " ".join((entry.findtext("atom:summary", "", ns) or "").split())
                    published = (entry.findtext("atom:published", "", ns) or "")[:10]
                    doi = (entry.findtext("arxiv:doi", "", ns) or "").strip() or default_doi
                    creators: list[Author] = []
                    for author_el in entry.findall("atom:author", ns):
                        full = " ".join((author_el.findtext("atom:name", "", ns) or "").split())
                        if not full:
                            continue
                        parts = full.rsplit(" ", 1)
                        if len(parts) == 2:
                            creators.append(Author(first_name=parts[0], last_name=parts[1]))
                        else:
                            creators.append(Author(name=full))
                    if title:
                        return PaperMetadata(
                            title=title,
                            item_type="preprint",
                            creators=creators,
                            doi=normalize_doi(doi) if doi else None,
                            arxiv_id=clean_id,
                            url=f"https://arxiv.org/abs/{clean_id}",
                            pdf_url=f"https://arxiv.org/pdf/{clean_id}.pdf",
                            publication_title="arXiv",
                            date=published,
                            abstract_note=summary,
                            source="arxiv",
                        )
            except ET.ParseError:
                pass

        # 2. Fallback: arXiv /abs/<id> HTML <meta name="citation_*"> (resilient when export API returns 429!)
        abs_meta = self._parse_arxiv_abs_html(clean_id, base_id)
        if abs_meta is not None:
            return abs_meta

        # 3. Fallback: OpenAlex DataCite arXiv DOI lookup
        oa_meta = self._resolve_arxiv_via_openalex(clean_id, base_id)
        if oa_meta is not None:
            return oa_meta

        return PaperMetadata(
            title=f"arXiv:{clean_id}",
            item_type="preprint",
            doi=default_doi,
            arxiv_id=clean_id,
            url=f"https://arxiv.org/abs/{clean_id}",
            pdf_url=f"https://arxiv.org/pdf/{clean_id}.pdf",
            publication_title="arXiv",
            source="arxiv",
        )

    def _resolve_crossref_doi(self, doi: str) -> PaperMetadata | None:
        encoded = urllib.parse.quote(doi, safe="")
        status, raw, _ = self._http_get(f"https://api.crossref.org/works/{encoded}")
        if status != 200 or not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        msg = payload.get("message")
        if not isinstance(msg, dict):
            return None
        return self._crossref_item_to_metadata(msg)

    def _crossref_item_to_metadata(self, msg: dict[str, Any]) -> PaperMetadata:
        titles = msg.get("title") or []
        title = " ".join(str(titles[0]).split()) if titles else str(msg.get("DOI") or "Untitled")
        cr_type = str(msg.get("type") or "").lower()
        item_type = "conferencePaper" if "proceedings" in cr_type or "conference" in cr_type else "journalArticle"

        creators: list[Author] = []
        for a in msg.get("author") or []:
            if not isinstance(a, dict):
                continue
            given = str(a.get("given") or "").strip()
            family = str(a.get("family") or "").strip()
            name = str(a.get("name") or "").strip()
            if given or family:
                creators.append(Author(first_name=given, last_name=family))
            elif name:
                creators.append(Author(name=name))

        container = msg.get("container-title") or []
        venue = " ".join(str(container[0]).split()) if container else ""

        date_str = ""
        for date_field in ("published-print", "published-online", "issued", "created"):
            dp = (msg.get(date_field) or {}).get("date-parts")
            if isinstance(dp, list) and dp and isinstance(dp[0], list) and dp[0]:
                date_str = "-".join(f"{int(x):02d}" if idx > 0 else str(x) for idx, x in enumerate(dp[0]))
                break

        doi = normalize_doi(str(msg.get("DOI"))) if msg.get("DOI") else None
        url = str(msg.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
        ieee_arnumber = extract_ieee_arnumber(url)

        # Check Crossref links for IEEE arnumber or direct PDF
        pdf_url: str | None = None
        for link in msg.get("link") or []:
            if not isinstance(link, dict):
                continue
            lurl = str(link.get("URL") or "")
            if not ieee_arnumber:
                ieee_arnumber = extract_ieee_arnumber(lurl)
            ctype = str(link.get("content-type") or "").lower()
            if "pdf" in ctype and not pdf_url and "ieeexplore.ieee.org" not in lurl.lower():
                pdf_url = lurl

        abstract = str(msg.get("abstract") or "")
        abstract = " ".join(re.sub(r"<[^>]+>", " ", abstract).split())

        return PaperMetadata(
            title=title,
            item_type=item_type,
            creators=creators,
            doi=doi,
            ieee_arnumber=ieee_arnumber,
            url=f"https://ieeexplore.ieee.org/document/{ieee_arnumber}" if ieee_arnumber else url,
            pdf_url=pdf_url,
            publication_title=venue if item_type != "conferencePaper" else "",
            proceedings_title=venue if item_type == "conferencePaper" else "",
            date=date_str,
            volume=str(msg.get("volume") or ""),
            issue=str(msg.get("issue") or ""),
            pages=str(msg.get("page") or ""),
            abstract_note=abstract,
            publisher=str(msg.get("publisher") or ""),
            source="ieee" if (doi and doi.startswith("10.1109/")) or ieee_arnumber else "crossref",
        )

    def _enrich_from_openalex(self, meta: PaperMetadata) -> PaperMetadata:
        if not meta.doi:
            return meta
        encoded = urllib.parse.quote(f"https://doi.org/{meta.doi}", safe="")
        status, raw, _ = self._http_get(f"https://api.openalex.org/works/{encoded}")
        if status != 200 or not raw:
            return meta
        try:
            work = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return meta
        if not isinstance(work, dict):
            return meta

        best_oa = work.get("best_oa_location") or {}
        if isinstance(best_oa, dict):
            oa_pdf = best_oa.get("pdf_url")
            if oa_pdf and not meta.pdf_url:
                meta.pdf_url = str(oa_pdf)
            landing = str(best_oa.get("landing_page_url") or "")
            if not meta.ieee_arnumber:
                meta.ieee_arnumber = extract_ieee_arnumber(landing)

        primary = work.get("primary_location") or {}
        if isinstance(primary, dict):
            landing = str(primary.get("landing_page_url") or "")
            if not meta.ieee_arnumber:
                meta.ieee_arnumber = extract_ieee_arnumber(landing)
            if not meta.pdf_url and primary.get("pdf_url"):
                cand = str(primary["pdf_url"])
                if "ieeexplore.ieee.org" not in cand.lower():
                    meta.pdf_url = cand
        return meta

    def _enrich_from_unpaywall(self, meta: PaperMetadata) -> PaperMetadata:
        if not meta.doi or meta.pdf_url or not self.polite_email:
            return meta
        encoded = urllib.parse.quote(meta.doi, safe="")
        q = urllib.parse.urlencode({"email": self.polite_email})
        status, raw, _ = self._http_get(f"https://api.unpaywall.org/v2/{encoded}?{q}")
        if status != 200 or not raw:
            return meta
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return meta
        best = payload.get("best_oa_location") or {}
        if isinstance(best, dict) and best.get("url_for_pdf"):
            meta.pdf_url = str(best["url_for_pdf"])
        return meta

    def _resolve_doi(self, doi: str) -> PaperMetadata | None:
        norm = normalize_doi(doi)
        meta = self._resolve_crossref_doi(norm)
        if meta is None:
            meta = PaperMetadata(
                title=norm,
                doi=norm,
                url=f"https://doi.org/{norm}",
                source="ieee" if norm.startswith("10.1109/") else "doi",
            )
        meta = self._enrich_from_openalex(meta)
        meta = self._enrich_from_unpaywall(meta)

        # For IEEE DOIs without an arnumber yet, attempt a single polite redirect check on doi.org
        if norm.startswith("10.1109/") and not meta.ieee_arnumber:
            arnum = self._discover_ieee_arnumber_from_doi(norm)
            if arnum:
                meta.ieee_arnumber = arnum
                meta.url = f"https://ieeexplore.ieee.org/document/{arnum}"
                meta.source = "ieee"
        return meta

    def _discover_ieee_arnumber_from_doi(self, doi: str) -> str | None:
        # Use DOI.org official Handle API JSON first (never hits IEEE directly!)
        encoded = urllib.parse.quote(doi, safe="")
        status, raw, _ = self._http_get(f"https://doi.org/api/handles/{encoded}")
        if status == 200 and raw:
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
                for val in data.get("values") or []:
                    if isinstance(val, dict) and val.get("type") == "URL":
                        v = (val.get("data") or {}).get("value")
                        if isinstance(v, str):
                            arnum = extract_ieee_arnumber(v)
                            if arnum:
                                return arnum
            except json.JSONDecodeError:
                pass
        return None

    def _resolve_ieee_arnumber(self, arnumber: str) -> PaperMetadata:
        landing = f"https://ieeexplore.ieee.org/document/{arnumber}"
        # 1. Try OpenAlex first (avoids unnecessary IEEE HTML scraping!)
        q = urllib.parse.urlencode({"filter": f"primary_location.landing_page_url:{landing}", "per-page": "1"})
        status, raw, _ = self._http_get(f"https://api.openalex.org/works?{q}")
        if status == 200 and raw:
            try:
                results = (json.loads(raw.decode("utf-8", errors="replace")) or {}).get("results") or []
                if results and isinstance(results[0], dict):
                    oa_doi = results[0].get("doi")
                    if oa_doi:
                        meta = self._resolve_doi(str(oa_doi))
                        if meta:
                            meta.ieee_arnumber = arnumber
                            meta.url = landing
                            meta.source = "ieee"
                            return meta
            except json.JSONDecodeError:
                pass

        # 2. Polite single fetch of IEEE document page metadata JSON (if campus IP allows)
        p_status, p_raw, _ = self._http_get(landing, accept="text/html")
        if p_status == 200 and p_raw:
            html = p_raw.decode("utf-8", errors="replace")
            m = re.search(r"xplGlobal\.document\.metadata\s*=\s*(\{.*?\});", html, re.DOTALL)
            if m:
                try:
                    j = json.loads(m.group(1))
                    doi = normalize_doi(str(j["doi"])) if j.get("doi") else None
                    if doi:
                        meta = self._resolve_doi(doi)
                        if meta:
                            meta.ieee_arnumber = arnumber
                            meta.url = landing
                            meta.source = "ieee"
                            return meta
                    creators = []
                    for a in j.get("authors") or []:
                        if isinstance(a, dict) and a.get("name"):
                            parts = str(a["name"]).rsplit(" ", 1)
                            if len(parts) == 2:
                                creators.append(Author(first_name=parts[0], last_name=parts[1]))
                            else:
                                creators.append(Author(name=str(a["name"])))
                    return PaperMetadata(
                        title=str(j.get("title") or f"IEEE Document {arnumber}"),
                        item_type="conferencePaper" if j.get("isConference") else "journalArticle",
                        creators=creators,
                        doi=doi,
                        ieee_arnumber=arnumber,
                        url=landing,
                        publication_title=str(j.get("publicationTitle") or ""),
                        date=str(j.get("publicationYear") or j.get("publicationDate") or ""),
                        abstract_note=str(j.get("abstract") or ""),
                        source="ieee",
                    )
                except json.JSONDecodeError:
                    pass

        return PaperMetadata(
            title=f"IEEE Document {arnumber}",
            item_type="journalArticle",
            ieee_arnumber=arnumber,
            url=landing,
            source="ieee",
        )

    def _resolve_title(self, title_query: str) -> PaperMetadata:
        q = urllib.parse.urlencode({"query.title": title_query, "rows": "5"})
        status, raw, _ = self._http_get(f"https://api.crossref.org/works?{q}")
        candidates: list[tuple[float, dict[str, Any]]] = []
        if status == 200 and raw:
            try:
                items = (json.loads(raw.decode("utf-8", errors="replace")).get("message") or {}).get("items") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    t_list = item.get("title") or []
                    if not t_list:
                        continue
                    score = title_similarity(title_query, str(t_list[0]))
                    candidates.append((score, item))
            except json.JSONDecodeError:
                pass

        candidates.sort(key=lambda pair: pair[0], reverse=True)
        if not candidates:
            raise AmbiguousTitleError(title_query, [])

        top_score, top_item = candidates[0]
        second_score = candidates[1][0] if len(candidates) > 1 else 0.0
        if top_score >= 0.88 and (top_score - second_score >= 0.05 or top_score >= 0.96):
            meta = self._crossref_item_to_metadata(top_item)
            if meta.doi:
                resolved = self._resolve_doi(meta.doi)
                if resolved:
                    return resolved
            return meta

        summary_candidates = []
        for score, item in candidates[:5]:
            t_list = item.get("title") or [""]
            summary_candidates.append(
                {
                    "score": round(score, 3),
                    "title": str(t_list[0]),
                    "doi": item.get("DOI"),
                    "year": ((item.get("issued") or {}).get("date-parts") or [[None]])[0][0],
                }
            )
        raise AmbiguousTitleError(title_query, summary_candidates)
