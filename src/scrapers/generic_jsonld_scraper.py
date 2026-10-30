"""Selector-free scraper for boards that publish schema.org ``JobPosting`` JSON-LD.

Google for Jobs only indexes postings that carry ``JobPosting`` structured data,
so most Vietnamese boards embed it on every detail page.  This scraper never
looks at visual markup:

1. Discover job URLs from XML sitemaps (newest ``<lastmod>`` first), a listing
   page, or a site search URL.
2. Fetch each detail page and read its ``application/ld+json`` blocks.
3. Fall back to OpenGraph tags when a page has no ``JobPosting``.

A layout redesign therefore rarely breaks it; when every fetched page lacks
``JobPosting`` the scraper reports ``last_error = "layout_changed"``.
"""

from __future__ import annotations

import gzip
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from src.common.logger import get_logger
from src.common.models import JobSource, RawJob
from src.common.text_utils import clean_vn_text, slugify_vn
from src.scrapers.base_scraper import BLOCKED_STATUSES, BaseScraper

logger = get_logger(__name__)

DESCRIPTION_LIMIT = 4000
REQUIREMENTS_LIMIT = 4000
LAYOUT_CHANGED = "layout_changed"

# Placeholder strings some boards put into address fields.
_JUNK_PLACES = {"not available", "n/a", "na", "none", "null", "-", "vn", "viet nam", "vietnam"}
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "zarsrc", "_gl"}


@dataclass(frozen=True)
class JsonLdSite:
    """Where to find one board's job pages.

    ``job_url_pattern`` is a regex (``re.search``) a detail URL must match;
    empty matches everything.  ``sitemap_filter`` optionally restricts which
    child sitemaps of a sitemap index are followed.  ``listing_urls`` and
    ``search_url`` may contain ``{page}``; ``search_url`` also ``{keyword}``.
    """

    source: JobSource = JobSource.GENERIC
    label: str = ""
    sitemap_urls: tuple[str, ...] = ()
    listing_urls: tuple[str, ...] = ()
    job_url_pattern: str = ""
    max_age_days: int = 3
    search_url: Optional[str] = None
    sitemap_filter: str = ""


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: Optional[datetime]
    order: int  # position in discovery order; later entries are usually newer


class GenericJsonLdScraper(BaseScraper):
    """Scrape any board whose detail pages carry ``JobPosting`` JSON-LD."""

    def __init__(
        self,
        site: JsonLdSite,
        max_detail_pages: int = 100,
        live_max_detail_pages: int = 10,
        max_sitemaps: int = 6,
        live_max_sitemaps: int = 2,
        now: Optional[Callable[[], datetime]] = None,
    ):
        super().__init__(source=site.source)
        self.site = site
        self.max_detail_pages = max_detail_pages
        self.live_max_detail_pages = live_max_detail_pages
        self.max_sitemaps = max_sitemaps
        self.live_max_sitemaps = live_max_sitemaps
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._job_re = re.compile(site.job_url_pattern) if site.job_url_pattern else None
        self._sitemap_re = re.compile(site.sitemap_filter) if site.sitemap_filter else None
        self._soft_error = ""

    # ── Public API ───────────────────────────────────────────────

    def scrape(self, keyword: str, max_pages: Optional[int] = None) -> list[RawJob]:
        """Jobs for ``keyword``: site search when configured, else local filter."""
        self._start_run()
        if self.site.search_url:
            urls = self._discover_from_search(keyword, max_pages)
            return self._finish_run(self._fetch_jobs(urls))
        urls = self._discover(max_pages)
        slug = slugify_vn(keyword) if keyword else ""
        if slug:
            # URL slugs usually repeat the title, so likely matches go first.
            urls = sorted(urls, key=lambda u: slug not in u.lower())
        jobs = self._fetch_jobs(urls)
        return self._finish_run([job for job in jobs if _matches_keyword(job, keyword)])

    def scrape_latest(self, max_pages: Optional[int] = None) -> list[RawJob]:
        """Newest jobs from sitemaps/listing pages, without a keyword."""
        self._start_run()
        return self._finish_run(self._fetch_jobs(self._discover(max_pages)))

    def _start_run(self) -> None:
        self.last_error = ""
        self._soft_error = ""

    def _finish_run(self, jobs: list[RawJob]) -> list[RawJob]:
        # A stray 404 is normal; report it only when it cost us every job.
        if not jobs and not self.last_error and self._soft_error:
            self.last_error = self._soft_error
        return jobs

    def parse_job(self, raw_data: dict | object) -> Optional[RawJob]:
        """Parse ``{"html": str, "url": str}`` into a RawJob (None if not a job)."""
        if not isinstance(raw_data, dict):
            return None
        job, _ = self.parse_page(str(raw_data.get("html") or ""), str(raw_data.get("url") or ""))
        return job

    def parse_page(self, page_html: str, url: str) -> tuple[Optional[RawJob], bool]:
        """Return ``(job, had_jobposting)`` for one detail page."""
        canonical = _canonical_url(page_html, url)
        posting = find_job_posting(extract_json_ld(page_html))
        if posting is not None:
            job = self._job_from_posting(posting, canonical)
            if job is not None:
                return job, True
        return self._job_from_opengraph(page_html, canonical), posting is not None

    # ── Budget helpers ───────────────────────────────────────────

    @property
    def _name(self) -> str:
        return self.site.label or self.source.value

    @property
    def detail_cap(self) -> int:
        if self.live_mode:
            return max(0, min(self.max_detail_pages, self.live_max_detail_pages))
        return max(0, self.max_detail_pages)

    @property
    def sitemap_cap(self) -> int:
        if self.live_mode:
            return max(1, min(self.max_sitemaps, self.live_max_sitemaps))
        return max(1, self.max_sitemaps)

    def _page_cap(self, max_pages: Optional[int]) -> int:
        if self.live_mode:
            return 1
        return max(1, max_pages or self.settings.scrape_max_pages)

    def _budget_left(self) -> bool:
        if self.deadline_at is None:
            return True
        return time.monotonic() + self.budget_margin < self.deadline_at

    def _fetch(self, url: str) -> Optional[requests.Response]:
        """GET a URL; None on failure.  Raises _Stop when we must back off."""
        if not self._budget_left():
            raise _Stop("budget")
        previous_error = self.last_error
        try:
            return self._get(url)
        except TimeoutError as exc:  # _rate_limit: budget exhausted
            raise _Stop("budget") from exc
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in BLOCKED_STATUSES:
                logger.warning(f"{self._name}: blocked ({status}) at {url}")
                raise _Stop(f"HTTP{status}") from exc
            logger.info(f"{self._name}: {url} failed: {exc}")
            self._soft_error = self.last_error or type(exc).__name__
            self.last_error = previous_error
            return None

    # ── Discovery ────────────────────────────────────────────────

    def _discover(self, max_pages: Optional[int]) -> list[str]:
        urls: list[str] = []
        try:
            if self.site.sitemap_urls:
                urls = self._discover_from_sitemaps()
            if not urls and self.site.listing_urls:
                urls = self._discover_from_listings(max_pages)
        except _Stop:
            pass
        return urls[: self.detail_cap]

    def _discover_from_sitemaps(self) -> list[str]:
        cutoff = self._now() - timedelta(days=self.site.max_age_days)
        queue = [SitemapEntry(url, None, i) for i, url in enumerate(self.site.sitemap_urls)]
        fetched = 0
        entries: list[SitemapEntry] = []
        seen_jobs: set[str] = set()
        order = 0
        while queue and fetched < self.sitemap_cap:
            sitemap = queue.pop(0)
            fetched += 1
            response = self._fetch(sitemap.url)
            if response is None:
                continue
            text = _decode_sitemap(response.content)
            is_index, items = parse_sitemap(text)
            if is_index:
                children = [
                    SitemapEntry(loc, lastmod, i)
                    for i, (loc, lastmod) in enumerate(items)
                    if self._sitemap_re is None or self._sitemap_re.search(loc)
                ]
                # Newest child first; same lastmod → later (higher-numbered) file first.
                queue = _newest_first(children) + queue
                continue
            for loc, lastmod in items:
                order += 1
                if loc in seen_jobs or not self._is_job_url(loc):
                    continue
                if lastmod is not None and lastmod < cutoff:
                    continue
                seen_jobs.add(loc)
                entries.append(SitemapEntry(loc, lastmod, order))
            dated = sum(1 for e in entries if e.lastmod is not None)
            if dated >= self.detail_cap:
                break
        return [e.url for e in _newest_first(entries)]

    def _discover_from_listings(self, max_pages: Optional[int]) -> list[str]:
        pages: list[str] = []
        page_numbers = range(1, self._page_cap(max_pages) + 1)
        for template in self.site.listing_urls:
            if "{page}" in template:
                pages.extend(template.replace("{page}", str(p)) for p in page_numbers)
            else:
                pages.append(template)
        return self._collect_links(pages)

    def _discover_from_search(self, keyword: str, max_pages: Optional[int]) -> list[str]:
        template = (self.site.search_url or "").replace("{keyword}", quote_plus(keyword))
        pages = []
        for page in range(1, self._page_cap(max_pages) + 1):
            pages.append(template.replace("{page}", str(page)))
            if "{page}" not in template:
                break
        try:
            return self._collect_links(pages)[: self.detail_cap]
        except _Stop:
            return []

    def _collect_links(self, pages: Iterable[str]) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for page_url in pages:
            if len(found) >= self.detail_cap:
                break
            response = self._fetch(page_url)
            if response is None:
                continue
            new = 0
            for link in extract_links(_response_text(response), page_url):
                if link not in seen and self._is_job_url(link):
                    seen.add(link)
                    found.append(link)
                    new += 1
            if new == 0:
                break  # past the last result page
        return found

    def _is_job_url(self, url: str) -> bool:
        return self._job_re is None or bool(self._job_re.search(url))

    # ── Detail pages ─────────────────────────────────────────────

    def _fetch_jobs(self, urls: list[str]) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()
        fetched_ok = 0
        with_posting = 0
        for url in urls[: self.detail_cap]:
            try:
                response = self._fetch(url)
            except _Stop:
                break
            if response is None:
                continue
            fetched_ok += 1
            final_url = str(getattr(response, "url", "") or url)
            job, had_posting = self.parse_page(_response_text(response), final_url)
            with_posting += had_posting
            if job is None or job.source_url in seen:
                continue
            seen.add(job.source_url)
            jobs.append(job)
        if fetched_ok and not with_posting:
            self.last_error = LAYOUT_CHANGED
            logger.warning(
                f"{self._name}: {fetched_ok} pages, no JobPosting",
                extra={"source": self.source.value},
            )
        return jobs

    def _job_from_posting(self, posting: dict, url: str) -> Optional[RawJob]:
        title = _text(posting.get("title") or posting.get("name"))
        if not title:
            return None
        fields: dict[str, Any] = {
            "title": title,
            "company": _organization_name(posting.get("hiringOrganization")),
            "location": _locations(posting),
            "salary_raw": format_salary(
                posting.get("baseSalary") or posting.get("estimatedSalary")
            ),
            "description": html_to_text(posting.get("description"))[:DESCRIPTION_LIMIT],
            "requirements": _requirements(posting)[:REQUIREMENTS_LIMIT],
            "job_type_raw": _employment_type(posting.get("employmentType")),
            "source": self.source,
            "source_url": url,
            "posted_at_raw": _text(posting.get("datePosted")),
        }
        valid_through = _text(posting.get("validThrough"))
        # RawJob has no deadline field yet; pass it through once the model gains one.
        if valid_through and "valid_through_raw" in RawJob.model_fields:
            fields["valid_through_raw"] = valid_through
        return RawJob(**fields)

    def _job_from_opengraph(self, page_html: str, url: str) -> Optional[RawJob]:
        metas = _meta_tags(page_html)
        title = metas.get("og:title") or _title_tag(page_html)
        if not title:
            return None
        description = metas.get("og:description") or metas.get("description") or ""
        return RawJob(
            title=title,
            description=description[:DESCRIPTION_LIMIT],
            source=self.source,
            source_url=url,
        )


class _Stop(Exception):
    """Stop the run: budget spent or the site is refusing us."""


def parse_detail_html(page_html: str, url: str = "") -> dict[str, Any]:
    """Fields of one job detail page from ``JobPosting`` JSON-LD, else OpenGraph.

    Used by the ETL detail fetch (``src/etl/detail_fetch.py``) to fill short
    descriptions and missing deadlines.  Returns ``{}`` when the page has
    neither; keys with no value are omitted.  ``has_job_posting`` tells whether
    the data came from structured JSON-LD.
    """
    page_html = page_html or ""
    posting = find_job_posting(extract_json_ld(page_html))
    fields: dict[str, Any]
    if posting is not None:
        fields = {
            "title": _text(posting.get("title") or posting.get("name")),
            "company": _organization_name(posting.get("hiringOrganization")),
            "location": _locations(posting),
            "salary_raw": format_salary(
                posting.get("baseSalary") or posting.get("estimatedSalary")
            ),
            "description": html_to_text(posting.get("description"))[:DESCRIPTION_LIMIT],
            "requirements": _requirements(posting)[:REQUIREMENTS_LIMIT],
            "benefits": html_to_text(posting.get("jobBenefits"))[:DESCRIPTION_LIMIT],
            "job_type_raw": _employment_type(posting.get("employmentType")),
            "posted_at_raw": _text(posting.get("datePosted")),
            "valid_through_raw": _text(posting.get("validThrough")),
            "has_job_posting": True,
        }
    else:
        metas = _meta_tags(page_html)
        fields = {
            "title": metas.get("og:title") or _title_tag(page_html),
            "description": (metas.get("og:description") or metas.get("description")
                            or "")[:DESCRIPTION_LIMIT],
            "has_job_posting": False,
        }
        if not fields["title"] and not fields["description"]:
            return {}
    fields["source_url"] = _canonical_url(page_html, url) if url else ""
    return {k: v for k, v in fields.items() if v or k == "has_job_posting"}


# ── Sitemaps ─────────────────────────────────────────────────────

_SITEMAP_BLOCK = re.compile(r"<(?:\w+:)?(url|sitemap)\b[^>]*>(.*?)</(?:\w+:)?\1\s*>", re.S | re.I)
_LOC = re.compile(r"<(?:\w+:)?loc\b[^>]*>(.*?)</(?:\w+:)?loc\s*>", re.S | re.I)
_LASTMOD = re.compile(r"<(?:\w+:)?lastmod\b[^>]*>(.*?)</(?:\w+:)?lastmod\s*>", re.S | re.I)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def _decode_sitemap(content: bytes) -> str:
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError):
            return ""
    return content.decode("utf-8", errors="replace")


def parse_sitemap(text: str) -> tuple[bool, list[tuple[str, Optional[datetime]]]]:
    """Return ``(is_index, [(loc, lastmod)])``.  Regex-based: tolerant and XXE-free."""
    is_index = bool(re.search(r"<(?:\w+:)?sitemapindex\b", text, re.I))
    wanted = "sitemap" if is_index else "url"
    items: list[tuple[str, Optional[datetime]]] = []
    for match in _SITEMAP_BLOCK.finditer(text):
        if match.group(1).lower() != wanted:
            continue
        body = match.group(2)
        loc_match = _LOC.search(body)
        if not loc_match:
            continue
        loc = html.unescape(_CDATA.sub(r"\1", loc_match.group(1))).strip()
        if not loc:
            continue
        lastmod_match = _LASTMOD.search(body)
        items.append((loc, parse_lastmod(lastmod_match.group(1)) if lastmod_match else None))
    return is_index, items


def parse_lastmod(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _newest_first(entries: list[SitemapEntry]) -> list[SitemapEntry]:
    """Dated entries newest first, then undated; ties → later document order first."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        entries,
        key=lambda e: (e.lastmod is not None, e.lastmod or floor, e.order),
        reverse=True,
    )


# ── HTML helpers ─────────────────────────────────────────────────

_LD_SCRIPT = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script\s*>",
    re.S | re.I,
)
_TAG_ATTRS = re.compile(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_HREF = re.compile(r"<a\b[^>]*?\bhref\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)


def _response_text(response: requests.Response) -> str:
    content_type = str(response.headers.get("content-type", "")).lower()
    if "charset=" in content_type:
        return response.text
    # requests assumes ISO-8859-1 without a charset; Vietnamese pages are UTF-8.
    return response.content.decode("utf-8", errors="replace")


def _attrs(tag: str) -> dict[str, str]:
    result = {}
    for name, value in _TAG_ATTRS.findall(tag):
        if value[:1] in {'"', "'"}:
            value = value[1:-1]
        result[name.lower()] = html.unescape(value)
    return result


def _meta_tags(page_html: str) -> dict[str, str]:
    metas: dict[str, str] = {}
    for tag in re.findall(r"<meta\b[^>]*>", page_html, re.I):
        attrs = _attrs(tag)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content", "").strip()
        if key and content and key not in metas:
            metas[key] = content
    return metas


def _title_tag(page_html: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", page_html, re.S | re.I)
    return _clean_space(html.unescape(match.group(1))) if match else ""


def _canonical_url(page_html: str, url: str) -> str:
    for tag in re.findall(r"<link\b[^>]*>", page_html, re.I):
        attrs = _attrs(tag)
        if "canonical" in attrs.get("rel", "").lower().split() and attrs.get("href"):
            return strip_tracking(urljoin(url, attrs["href"].strip()))
    return strip_tracking(url)


def strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def extract_links(page_html: str, base_url: str) -> list[str]:
    links = []
    for raw in _HREF.findall(page_html):
        href = html.unescape(raw.strip("\"'")).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        links.append(strip_tracking(urljoin(base_url, href)))
    return links


def html_to_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if "&lt;" in text and "<" not in text:
        text = html.unescape(text)  # double-escaped markup
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n")
    else:
        text = html.unescape(text)
    lines = (_clean_space(line) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


# ── JSON-LD ──────────────────────────────────────────────────────


def extract_json_ld(page_html: str) -> list[Any]:
    """Parse every ld+json block, skipping ones that cannot be repaired."""
    documents = []
    for block in _LD_SCRIPT.findall(page_html):
        data = _loads_tolerant(block)
        if data is not None:
            documents.append(data)
    return documents


def _loads_tolerant(block: str) -> Any:
    text = block.strip()
    text = re.sub(r"^\s*(?:<!--|<!\[CDATA\[)", "", text)
    text = re.sub(r"(?:-->|\]\]>)\s*$", "", text).strip()
    if not text:
        return None
    attempts = [text, re.sub(r",\s*([}\]])", r"\1", text)]
    for candidate in attempts:
        try:
            return json.loads(candidate, strict=False)
        except ValueError:
            pass
    try:  # trailing junk after a valid object
        return json.JSONDecoder(strict=False).raw_decode(attempts[1])[0]
    except ValueError:
        return None


def _iter_nodes(data: Any) -> Iterable[dict]:
    stack = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            stack[:0] = node
        elif isinstance(node, dict):
            yield node
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def _is_job_posting(node: dict) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(k).rsplit("/", 1)[-1] == "JobPosting" for k in kinds if k)


def find_job_posting(documents: list[Any]) -> Optional[dict]:
    for document in documents:
        for node in _iter_nodes(document):
            if _is_job_posting(node):
                return node
    return None


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return html.unescape(str(value)).strip()


def _organization_name(value: Any) -> str:
    if isinstance(value, list):
        return next((name for item in value if (name := _organization_name(item))), "")
    if isinstance(value, dict):
        return _clean_space(_text(value.get("name") or value.get("legalName")))
    return _clean_space(_text(value))


def _locations(posting: dict) -> str:
    places: list[str] = []

    def add(value: Any) -> None:
        name = _clean_space(_text(value))
        if name and name.lower() not in _JUNK_PLACES and name not in places:
            places.append(name)

    raw = posting.get("jobLocation")
    for place in raw if isinstance(raw, list) else [raw]:
        if isinstance(place, str):
            add(place)
            continue
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        for addr in address if isinstance(address, list) else [address]:
            if isinstance(addr, dict):
                add(addr.get("addressLocality"))
                add(addr.get("addressRegion"))
            else:
                add(addr)
        if not address:
            add(place.get("name"))
    if not places and str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        places.append("Remote")
    return ", ".join(places)


def _number(value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).replace(",", "").strip())
        except ValueError:
            return None
    return str(int(number)) if number.is_integer() else f"{number:g}"


def format_salary(value: Any) -> str:
    """``baseSalary`` → e.g. ``"15000000-20000000 VND/MONTH"`` or the board's text."""
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return ""
    if not isinstance(value, dict):
        return _text(value)
    currency = _text(value.get("currency") or value.get("salaryCurrency"))
    amount = value.get("value")
    unit = _text(value.get("unitText"))
    low = high = single = None
    text_value = ""
    if isinstance(amount, dict):
        currency = currency or _text(amount.get("currency"))
        unit = _text(amount.get("unitText")) or unit
        low, high = _number(amount.get("minValue")), _number(amount.get("maxValue"))
        single = _number(amount.get("value"))
        text_value = _text(amount.get("value"))
    else:
        single = _number(amount)
        text_value = _text(amount)
        low, high = _number(value.get("minValue")), _number(value.get("maxValue"))
    if low and high:
        figure = low if low == high else f"{low}-{high}"
    else:
        figure = low or high or single
    if not figure:
        return text_value  # "Cạnh tranh", "Thỏa thuận", ...
    result = f"{figure} {currency}".strip()
    return f"{result}/{unit.upper()}" if unit else result


def _employment_type(value: Any) -> str:
    items = value if isinstance(value, list) else [value]
    kinds = []
    for item in items:
        text = _text(item).strip("\"' ")
        if text and text not in kinds:
            kinds.append(text)
    return ", ".join(kinds)


_SCHEMA_TYPE_TEXT = re.compile(r"^[A-Z][a-z]+(?:[A-Z][a-z]+)+(?:,|$)")


def _requirement_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(t for t in (_requirement_text(v) for v in value) if t)
    if isinstance(value, dict):
        return html_to_text(
            value.get("description") or value.get("name") or value.get("alternateName")
        )
    text = html_to_text(value)
    # Some boards stringify the schema type: "OccupationalExperienceRequirements, 36".
    return "" if _SCHEMA_TYPE_TEXT.match(text) else text


def _requirements(posting: dict) -> str:
    parts = []
    for key in ("responsibilities", "qualifications", "skills",
                "experienceRequirements", "educationRequirements"):
        text = _requirement_text(posting.get(key))
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def _matches_keyword(job: RawJob, keyword: str) -> bool:
    needle = clean_vn_text(keyword)
    if not needle:
        return True
    haystack = clean_vn_text(f"{job.title} {job.description}")
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


# ── Boards verified live on 2026-10-30 (see docs/SOURCES_JSONLD.md) ──

KNOWN_SITES: tuple[JsonLdSite, ...] = (
    JsonLdSite(
        source=JobSource.CAREERVIET,
        label="CareerViet",
        sitemap_urls=("https://careerviet.vn/sitemap/job_current_date.xml",),
        job_url_pattern=r"careerviet\.vn/vi/tim-viec-lam/[^/]+\.[0-9A-F]+\.html$",
        max_age_days=2,
    ),
    JsonLdSite(
        source=JobSource.VIECLAM24H,
        label="ViecLam24h",
        # Index of tintuyendung-N.xml; the highest N holds the newest IDs.
        sitemap_urls=("https://cdn1.vieclam24h.vn/file/sitemap/daily/job-0.xml",),
        job_url_pattern=r"vieclam24h\.vn/[^/]+/[^/]+-c\d+p\d+id\d+\.html$",
    ),
    JsonLdSite(
        source=JobSource.VIECOI,
        label="Viecoi",
        sitemap_urls=("https://viecoi.vn/job.xml",),
        job_url_pattern=r"viecoi\.vn/viec-lam/[^/]+-\d+\.html$",
    ),
    JsonLdSite(
        source=JobSource.ITVIEC,
        label="ITviec",
        sitemap_urls=("https://itviec.com/twinnings_jobs_desc_vn.xml",),
        job_url_pattern=r"itviec\.com/viec-lam-it/[^/?#]+$",
    ),
    JsonLdSite(
        source=JobSource.CAREERLINK,
        label="CareerLink",
        listing_urls=("https://www.careerlink.vn/vieclam/list?page={page}",),
        search_url="https://www.careerlink.vn/vieclam/list?keyword={keyword}&page={page}",
        job_url_pattern=r"careerlink\.vn/tim-viec-lam/[^/]+/\d+$",
    ),
    JsonLdSite(
        source=JobSource.JOB123,
        label="123job",
        listing_urls=("https://123job.vn/viec-lam",),
        # Detail slugs end in a mixed-case hash such as "-29GgoNXv9E".
        job_url_pattern=r"123job\.vn/viec-lam/[^/?#]+-(?=[A-Za-z0-9]*[A-Z0-9])[A-Za-z0-9]{8,12}$",
    ),
)
