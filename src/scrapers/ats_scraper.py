"""Company career pages through the public JSON APIs of five ATS vendors.

Greenhouse, Lever, Ashby, SmartRecruiters and Workable publish every open
position of a company as unauthenticated JSON (they exist so that job
aggregators can read them).  ``ats_companies.json`` lists the companies that
were verified to post jobs located in Vietnam; one request per company returns
all of its positions, which are filtered locally by location and keyword.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from src.common.models import JobSource, RawJob
from src.common.text_utils import clean_vn_text
from src.scrapers.base_scraper import BaseScraper

COMPANIES_FILE = Path(__file__).with_name("ats_companies.json")
LIVE_MAX_COMPANIES = 8
MAX_DESCRIPTION = 4000
CACHE_TTL_SECONDS = 1800
SMARTRECRUITERS_PAGE = 100
SMARTRECRUITERS_MAX_PAGES = 3

API_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
}

# Matched against folded (diacritic-free, lowercase) location text.
_VN_PLACES = re.compile(
    r"viet ?nam|ho chi minh|\bhcmc?\b|sai ?gon|ha ?noi|da nang|hai phong|can tho|"
    r"bac ninh|binh duong|dong nai|long an|\bvn\b"
)
_REMOTE_APAC = re.compile(r"\b(apac|asia|asean|southeast|south east|sea)\b")

# "ats:slug" -> (fetched_at, jobs); shared by every scraper instance so that the
# per-keyword scrapes of one batch run reuse a single request per company.
_CACHE: dict[str, tuple[float, list[RawJob]]] = {}


def is_vietnam_location(text: str) -> bool:
    """True for Vietnam places and for remote roles open to Asia-Pacific."""
    folded = clean_vn_text(text or "")
    if not folded:
        return False
    if _VN_PLACES.search(folded):
        return True
    return "remote" in folded and bool(_REMOTE_APAC.search(folded))


def keyword_matches(keyword: str, *texts: str) -> bool:
    """Every word of the keyword appears as a whole word (diacritics ignored)."""
    words = clean_vn_text(keyword or "").split()
    if not words:
        return True
    haystack = clean_vn_text(" ".join(t for t in texts if t))
    return all(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", haystack) for w in words)


def html_to_text(raw: str, limit: int = MAX_DESCRIPTION) -> str:
    """Strip tags (also from entity-escaped HTML, as Greenhouse sends it)."""
    if not raw:
        return ""
    text = html_lib.unescape(raw) if "&lt;" in raw else raw
    text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    return text[:limit]


def _iso_from_millis(value) -> str:
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def load_companies(path: Path = COMPANIES_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class AtsScraper(BaseScraper):
    def __init__(self, companies: Optional[list[dict]] = None):
        super().__init__(source=JobSource.ATS)
        self.companies = companies if companies is not None else load_companies()

    # ---- public API -------------------------------------------------------

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: Optional[str] = None,
    ) -> list[RawJob]:
        jobs = self._collect()
        out = [j for j in jobs if keyword_matches(keyword, j.title, j.description, j.company)]
        if location:
            out = [j for j in out if keyword_matches(location, j.location) or not j.location]
        return out

    def scrape_latest(self, max_pages: Optional[int] = None) -> list[RawJob]:
        return self._collect()

    def parse_job(self, raw_data: dict) -> Optional[RawJob]:
        """Parse one posting; ``raw_data`` carries ``_ats`` and ``_company`` markers."""
        parser = _PARSERS.get(raw_data.get("_ats", ""))
        if parser is None:
            return None
        return parser(raw_data, raw_data.get("_company", ""))

    # ---- fetching ---------------------------------------------------------

    def _selected(self) -> list[dict]:
        if self.live_mode:
            ranked = [c for c in self.companies if c.get("live_priority") is not None]
            ranked.sort(key=lambda c: c["live_priority"])
            return ranked[:LIVE_MAX_COMPANIES]
        return self.companies

    def _collect(self) -> list[RawJob]:
        collected: list[RawJob] = []
        for entry in self._selected():
            try:
                collected.extend(self._company_jobs(entry))
            except TimeoutError:
                self.last_error = self.last_error or "TimeoutError"
                break
            except (requests.RequestException, ValueError):
                continue  # one company down must not hide the others
        return collected

    def _company_jobs(self, entry: dict) -> list[RawJob]:
        cache_key = f"{entry['ats']}:{entry['slug']}"
        cached = _CACHE.get(cache_key)
        if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        jobs: list[RawJob] = []
        for raw in self._fetch(entry):
            raw["_ats"], raw["_company"] = entry["ats"], entry.get("company", "")
            job = self.parse_job(raw)
            if job and is_vietnam_location(job.location):
                jobs.append(job)
        _CACHE[cache_key] = (time.time(), jobs)
        return jobs

    def _fetch(self, entry: dict) -> list[dict]:
        ats, slug = entry["ats"], entry["slug"]
        url = API_URLS[ats].format(slug=slug)
        if ats == "smartrecruiters":
            return self._fetch_smartrecruiters(url)
        data = self._get(url).json()
        if ats == "lever":
            return list(data or [])
        return list((data or {}).get("jobs") or [])

    def _fetch_smartrecruiters(self, url: str) -> list[dict]:
        postings: list[dict] = []
        pages = 1 if self.live_mode else SMARTRECRUITERS_MAX_PAGES
        for page in range(pages):
            params = {
                "country": "vn",
                "limit": SMARTRECRUITERS_PAGE,
                "offset": page * SMARTRECRUITERS_PAGE,
            }
            data = self._get(url, params=params).json() or {}
            batch = data.get("content") or []
            postings.extend(batch)
            if len(batch) < SMARTRECRUITERS_PAGE:
                break
        return postings


# ---- per-vendor field mapping ------------------------------------------------


def _join(parts) -> str:
    return "; ".join(dict.fromkeys(str(p).strip() for p in parts if p and str(p).strip()))


def _job(title, company, location, description, posted, url, job_type="") -> Optional[RawJob]:
    title = str(title or "").strip()
    if not title or not url:
        return None
    return RawJob(
        title=title,
        company=company,
        location=location,
        description=description,
        job_type_raw=job_type,
        source=JobSource.ATS,
        source_url=url,
        posted_at_raw=posted or "",
    )


def parse_greenhouse(raw: dict, company: str) -> Optional[RawJob]:
    names = [(raw.get("location") or {}).get("name")]
    names += [o.get("location") or o.get("name") for o in raw.get("offices") or []]
    return _job(
        raw.get("title"), raw.get("company_name") or company, _join(names),
        html_to_text(raw.get("content", "")),
        raw.get("updated_at") or raw.get("first_published"), raw.get("absolute_url"),
    )


def parse_lever(raw: dict, company: str) -> Optional[RawJob]:
    cats = raw.get("categories") or {}
    location = _join([cats.get("location")] + list(cats.get("allLocations") or []))
    if raw.get("country") == "VN" and not is_vietnam_location(location):
        location = _join([location, "Việt Nam"])
    description = (raw.get("descriptionPlain") or "") + "\n" + (raw.get("additionalPlain") or "")
    types = [cats.get("commitment")]
    if raw.get("workplaceType") == "remote":
        types.append("remote")
    return _job(
        raw.get("text"), company, location, description.strip()[:MAX_DESCRIPTION],
        _iso_from_millis(raw.get("createdAt")), raw.get("hostedUrl") or raw.get("applyUrl"),
        ", ".join(t for t in types if t),
    )


def parse_ashby(raw: dict, company: str) -> Optional[RawJob]:
    if raw.get("isListed") is False:
        return None
    places = [raw.get("location")]
    places += [s.get("location") for s in raw.get("secondaryLocations") or []]
    location = _join(places)
    if raw.get("isRemote") and location and "remote" not in location.lower():
        location = f"Remote - {location}"
    description = raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml", ""))
    types = [raw.get("employmentType")]
    if raw.get("isRemote"):
        types.append("remote")
    return _job(
        raw.get("title"), company, location, description[:MAX_DESCRIPTION],
        raw.get("publishedAt"), raw.get("jobUrl") or raw.get("applyUrl"),
        ", ".join(t for t in types if t),
    )


def parse_smartrecruiters(raw: dict, company: str) -> Optional[RawJob]:
    loc = raw.get("location") or {}
    location = loc.get("fullLocation") or ", ".join(
        p for p in (loc.get("city"), loc.get("country")) if p
    )
    if str(loc.get("country") or "").lower() == "vn" and not is_vietnam_location(location):
        location = f"{location}, Việt Nam".strip(", ")
    labels = [(raw.get(key) or {}).get("label") for key in ("function", "department", "industry")]
    description = " | ".join(dict.fromkeys(label for label in labels if label))
    ident = (raw.get("company") or {}).get("identifier") or ""
    url = ""
    if ident and raw.get("id"):
        url = f"https://jobs.smartrecruiters.com/{ident}/{raw['id']}"
    return _job(
        raw.get("name"), (raw.get("company") or {}).get("name") or company, location,
        description, raw.get("releasedDate"), url,
        (raw.get("typeOfEmployment") or {}).get("label") or "",
    )


def parse_workable(raw: dict, company: str) -> Optional[RawJob]:
    parts = [raw.get("city"), raw.get("state"), raw.get("country")]
    for item in raw.get("locations") or []:
        parts += [item.get("city"), item.get("country")]
    facts = [raw.get("function"), raw.get("industry"), raw.get("experience"), raw.get("education")]
    types = [raw.get("employment_type")]
    if raw.get("telecommuting"):
        types.append("remote")
    return _job(
        raw.get("title"), company, ", ".join(dict.fromkeys(str(p) for p in parts if p)),
        " | ".join(str(f) for f in facts if f),
        raw.get("published_on") or raw.get("created_at"),
        raw.get("url") or raw.get("shortlink"), ", ".join(t for t in types if t),
    )


_PARSERS = {
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
    "smartrecruiters": parse_smartrecruiters,
    "workable": parse_workable,
}
