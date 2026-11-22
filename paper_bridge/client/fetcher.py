"""Compliant tiered PDF fetcher: arXiv/OA first, IEEE campus direct second, browser handoff on any gate."""

from __future__ import annotations

import http.cookiejar
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from ..common.models import PaperMetadata
from ..common.security import is_safe_http_url

AUTH_URL_MARKERS = (
    "shibboleth",
    "openathens",
    "wayf",
    "idp",
    "login",
    "signin",
    "sign-in",
    "cas/login",
    "oauth",
    "sso",
    "seamlessaccess",
)

CAPTCHA_BODY_MARKERS = (
    "verify you are human",
    "captcha",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "cf-challenge",
    "cloudflare",
    "akamai",
    "incapsula",
    "radware",
    "bot detection",
    "unusual traffic",
)

AUTH_BODY_MARKERS = (
    "institutional sign in",
    "sign in to continue",
    "purchase pdf",
    "subscribe to read",
    "access through your institution",
    "openathens",
    "shibboleth",
    "denied access",
    "access denied",
)

IEEE_IFRAME_PDF_RE = re.compile(
    r"""<iframe[^>]+src=["']([^"']+(?:getPDF\.jsp|\.pdf)[^"']*)["']""",
    re.IGNORECASE,
)


@dataclass
class FetchOutcome:
    ok: bool
    pdf_bytes: bytes | None = None
    pdf_url: str = ""
    filename: str = ""
    source: str = ""
    needs_manual: bool = False
    reason: str = ""
    manual_url: str = ""
    is_rate_or_challenge: bool = False


def is_valid_pdf(data: bytes) -> bool:
    if not data or len(data) < 1024:
        return False
    head = data[:1024].lstrip()
    return head.startswith(b"%PDF-")


def sanitize_filename(title: str, fallback: str = "paper") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title or "")
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > 110:
        cleaned = cleaned[:110].rstrip(" .")
    return f"{cleaned}.pdf"


def classify_non_pdf_response(status: int, final_url: str, body: bytes) -> tuple[str, bool]:
    """Classify why a remote PDF fetch did not return a PDF.

    Returns (reason_code, is_rate_or_challenge).
    """
    if status == 429:
        return "rate_limited", True
    if status in {401, 403}:
        snippet = body[:8192].decode("utf-8", errors="replace").lower()
        if any(m in snippet for m in CAPTCHA_BODY_MARKERS):
            return "captcha_or_challenge", True
        return "paywall_or_forbidden", False

    url_lower = (final_url or "").lower()
    if any(m in url_lower for m in AUTH_URL_MARKERS):
        return "auth_required", False

    snippet = body[:16384].decode("utf-8", errors="replace").lower()
    if any(m in snippet for m in CAPTCHA_BODY_MARKERS):
        return "captcha_or_challenge", True
    if any(m in snippet for m in AUTH_BODY_MARKERS):
        return "auth_required", False
    if status >= 500 or status == 0:
        return f"upstream_error_{status}", False
    return "non_pdf_response", False


class TieredPaperFetcher:
    """Fetches PDFs politely without ever bypassing logins, paywalls, or anti-bot checks."""

    def __init__(self, *, polite_email: str = "", timeout: float = 30.0) -> None:
        self.polite_email = polite_email
        self.timeout = timeout

    def _user_agent(self) -> str:
        if self.polite_email:
            return f"zotero-paper-bridge/0.1 (mailto:{self.polite_email})"
        return "zotero-paper-bridge/0.1 (academic-research-workflow)"

    def _download(
        self,
        url: str,
        *,
        referer: str | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> tuple[int, bytes, str, str]:
        if not is_safe_http_url(url, require_academic_host=True):
            return 403, b"", url, ""
        headers = {
            "User-Agent": self._user_agent(),
            "Accept": "application/pdf, text/html;q=0.8, */*;q=0.5",
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        open_fn = opener.open if opener is not None else urllib.request.urlopen
        try:
            with open_fn(req, timeout=self.timeout) as resp:
                return (
                    resp.status,
                    resp.read(),
                    resp.geturl(),
                    str(resp.headers.get("Content-Type") or ""),
                )
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.geturl(), str(exc.headers.get("Content-Type") or "")
        except (urllib.error.URLError, OSError):
            return 0, b"", url, ""

    def fetch(self, meta: PaperMetadata, *, skip_silent_ieee: bool = False) -> FetchOutcome:
        filename = sanitize_filename(
            meta.title,
            fallback=meta.ieee_arnumber or meta.arxiv_id or "paper",
        )
        landing = meta.canonical_landing_url()

        # Tier 1a: arXiv direct PDF
        if meta.arxiv_id:
            arxiv_pdf = f"https://arxiv.org/pdf/{meta.arxiv_id}.pdf"
            status, body, final_url, _ = self._download(arxiv_pdf, referer=f"https://arxiv.org/abs/{meta.arxiv_id}")
            if status == 200 and is_valid_pdf(body):
                return FetchOutcome(
                    ok=True,
                    pdf_bytes=body,
                    pdf_url=final_url or arxiv_pdf,
                    filename=filename,
                    source="arxiv",
                )

        # Tier 1b: Legal OA PDF from OpenAlex / Unpaywall / Crossref
        if meta.pdf_url and "ieeexplore.ieee.org" not in meta.pdf_url.lower():
            status, body, final_url, _ = self._download(meta.pdf_url, referer=landing or None)
            if status == 200 and is_valid_pdf(body):
                return FetchOutcome(
                    ok=True,
                    pdf_bytes=body,
                    pdf_url=final_url or meta.pdf_url,
                    filename=filename,
                    source="open_access",
                )

        # Tier 2: IEEE Xplore direct fetch over campus/VPN network (no evasion!)
        if meta.ieee_arnumber:
            if skip_silent_ieee:
                return FetchOutcome(
                    ok=False,
                    needs_manual=True,
                    reason="ieee_circuit_breaker_active",
                    manual_url=landing,
                    source="ieee",
                )
            return self._fetch_ieee_campus_direct(meta.ieee_arnumber, filename, landing)

        # Tier 3: Other publishers without legal OA PDF -> browser handoff
        return FetchOutcome(
            ok=False,
            needs_manual=True,
            reason="publisher_browser_required",
            manual_url=landing,
            source=meta.source or "publisher",
        )

    def _fetch_ieee_campus_direct(self, arnumber: str, filename: str, landing: str) -> FetchOutcome:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        direct_pdf_url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}&ref="
        status, body, final_url, _ = self._download(direct_pdf_url, referer=landing, opener=opener)
        if status == 200 and is_valid_pdf(body):
            return FetchOutcome(
                ok=True,
                pdf_bytes=body,
                pdf_url=final_url or direct_pdf_url,
                filename=filename,
                source="ieee_campus",
            )

        reason, is_rl = classify_non_pdf_response(status, final_url, body)
        if is_rl or reason in {"auth_required", "paywall_or_forbidden"}:
            return FetchOutcome(
                ok=False,
                needs_manual=True,
                reason=reason,
                manual_url=landing,
                source="ieee",
                is_rate_or_challenge=is_rl,
            )

        # Some campus networks serve the PDF inside stamp.jsp's iframe without an auth gate.
        # Make at most ONE polite follow-up if stamp.jsp exposes a direct ieeexplore.ieee.org PDF URL.
        stamp_url = f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={arnumber}"
        s_status, s_body, s_final_url, _ = self._download(stamp_url, referer=landing, opener=opener)
        if s_status == 200 and is_valid_pdf(s_body):
            return FetchOutcome(
                ok=True,
                pdf_bytes=s_body,
                pdf_url=s_final_url or stamp_url,
                filename=filename,
                source="ieee_campus",
            )

        if s_status == 200 and s_body:
            s_reason, s_rl = classify_non_pdf_response(s_status, s_final_url, s_body)
            if s_rl or s_reason == "auth_required":
                return FetchOutcome(
                    ok=False,
                    needs_manual=True,
                    reason=s_reason,
                    manual_url=landing,
                    source="ieee",
                    is_rate_or_challenge=s_rl,
                )
            html = s_body.decode("utf-8", errors="replace")
            m = IEEE_IFRAME_PDF_RE.search(html)
            if m:
                iframe_url = urljoin(s_final_url or stamp_url, m.group(1))
                host = (urlparse(iframe_url).hostname or "").lower()
                if host == "ieeexplore.ieee.org" or host.endswith(".ieee.org"):
                    p_status, p_body, p_final, _ = self._download(
                        iframe_url, referer=stamp_url, opener=opener
                    )
                    if p_status == 200 and is_valid_pdf(p_body):
                        return FetchOutcome(
                            ok=True,
                            pdf_bytes=p_body,
                            pdf_url=p_final or iframe_url,
                            filename=filename,
                            source="ieee_campus",
                        )
                    p_reason, p_rl = classify_non_pdf_response(p_status, p_final, p_body)
                    return FetchOutcome(
                        ok=False,
                        needs_manual=True,
                        reason=p_reason,
                        manual_url=landing,
                        source="ieee",
                        is_rate_or_challenge=p_rl,
                    )

        final_reason, final_rl = classify_non_pdf_response(s_status or status, s_final_url or final_url, s_body or body)
        return FetchOutcome(
            ok=False,
            needs_manual=True,
            reason=final_reason,
            manual_url=landing,
            source="ieee",
            is_rate_or_challenge=final_rl,
        )
