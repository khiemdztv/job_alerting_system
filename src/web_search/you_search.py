"""You.com Web Search adapter with job-specific validation and normalization."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3
import requests

from src.common.logger import get_logger
from src.common.text_utils import clean_vn_text
from src.config import Settings, get_settings
from src.matcher.search import identity, normalize_location, rank_jobs

logger = get_logger(__name__)


class YouSearchError(RuntimeError):
    """A safe, non-secret-bearing You.com API failure."""


VIETNAM_JOB_DOMAINS = [
    "topcv.vn",
    "vietnamworks.com",
    "careerviet.vn",
    "jobsgo.vn",
    "vieclam24h.vn",
    "topdev.vn",
]

OTHER_JOB_DOMAINS = [
    "linkedin.com",
    "careerlink.vn",
    "glints.com",
    "itviec.com",
    "ybox.vn",
    "indeed.com",
    "jobs.smartrecruiters.com",
    "jobs.lever.co",
    "job-boards.greenhouse.io",
]

# The first request is restricted to the six large Vietnamese job boards.
# This broader list is used only for a second recent-results request when the
# first request is sparse.
BOOST_DOMAINS = [*VIETNAM_JOB_DOMAINS, *OTHER_JOB_DOMAINS]

JOB_SIGNALS = (
    " job ",
    " jobs ",
    " career",
    " hiring",
    " vacancy",
    " vacancies",
    " recruitment",
    " recruit",
    " apply",
    " position",
    " opening",
    " tuyen dung",
    " viec lam",
    " ung tuyen",
    " tim viec",
    " thuc tap",
    " intern",
)

NON_JOB_SIGNALS = (
    "course",
    "training course",
    "khoa hoc",
    "chuong trinh dao tao",
    "curriculum",
    "syllabus",
    "certificate course",
    "huong dan",
    "cach ung tuyen",
    "la gi",
    "lo trinh",
    "career guide",
    "how to apply",
)

NON_JOB_PATHS = (
    "/in/",
    "/posts/",
    "/pulse/",
    "/learning/",
    "/course",
    "/blog/",
    "/news/",
    "/search",
)

JOB_PATH_SIGNALS = (
    "/job/",
    "/jobs/",
    "/jobs/view/",
    "/careers/",
    "/career/",
    "/positions/",
    "/opportunities/",
    "/recruitment/",
    "/tuyen-dung/",
    "/viec-lam/",
    "/viewjob",
)

LOCATION_LABELS = (
    ("ho chi minh", "Ho Chi Minh"),
    ("hcm", "Ho Chi Minh"),
    ("saigon", "Ho Chi Minh"),
    ("ha noi", "Ha Noi"),
    ("hanoi", "Ha Noi"),
    ("da nang", "Da Nang"),
    ("binh duong", "Binh Duong"),
    ("dong nai", "Dong Nai"),
    ("hai phong", "Hai Phong"),
    ("can tho", "Can Tho"),
)


@lru_cache(maxsize=8)
def _read_secret(secret_id: str, region: str) -> str:
    response = boto3.client("secretsmanager", region_name=region).get_secret_value(
        SecretId=secret_id
    )
    value = response.get("SecretString", "")
    if not value:
        raise YouSearchError("You.com API secret is empty")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value.strip()
    if isinstance(payload, dict):
        for key in ("YDC_API_KEY", "api_key", "key"):
            if payload.get(key):
                return str(payload[key]).strip()
    raise YouSearchError("You.com API secret has an unsupported format")


def _canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid", "trk", "trackingid"}
    ]
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(query), "")
    )


def _domain_label(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    name = parts[-2] if len(parts) >= 2 else parts[0]
    known = {
        "linkedin": "LinkedIn",
        "topcv": "TopCV",
        "vietnamworks": "VietnamWorks",
        "careerviet": "CareerViet",
        "careerlink": "CareerLink",
        "vieclam24h": "ViecLam24h",
        "jobsgo": "JobsGO",
        "topdev": "TopDev",
        "itviec": "ITviec",
        "smartrecruiters": "SmartRecruiters",
        "greenhouse": "Greenhouse",
    }
    return known.get(name, name.replace("-", " ").title()) or "Nguồn tuyển dụng"


def _company_from_title(title: str, fallback: str) -> str:
    generic = {
        "linkedin",
        "topcv",
        "vietnamworks",
        "careerviet",
        "careerlink",
        "jobsgo",
        "indeed",
        "glints",
        "itviec",
        "jobs",
        "job",
        "tuyen dung",
    }
    for marker in (" hiring ", " is looking for "):
        if marker in title.lower():
            company = title[: title.lower().index(marker)].strip()
            if 1 < len(company) <= 80:
                return company
    role_words = re.compile(
        r"\b(analyst|intern|engineer|developer|manager|specialist|assistant|"
        r"consultant|accountant|designer|tester|support)\b",
        flags=re.I,
    )
    site_names = generic | {clean_vn_text(fallback), "built in", "myworkdayjobs com"}
    for separator in (" at ", " @ ", " - ", " – ", " — ", " | "):
        if separator not in title:
            continue
        parts = [part.strip() for part in title.split(separator) if part.strip()]
        if len(parts) >= 2 and not role_words.search(parts[0]) and role_words.search(parts[1]):
            return parts[0]
        for candidate in parts[1:]:
            candidate = re.sub(r"\s+careers?$", "", candidate, flags=re.I).strip()
            cleaned = clean_vn_text(candidate).replace(".", " ")
            if (
                cleaned not in site_names
                and not re.search(r"\.(?:com|vn|net|org)$", candidate, flags=re.I)
                and 1 < len(candidate) <= 80
            ):
                return candidate
    return fallback


def _location_from_text(text: str, requested: str | None, url: str = "") -> str:
    cleaned = f" {clean_vn_text(text)} "
    for needle, label in LOCATION_LABELS:
        if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", cleaned):
            return label
    if requested:
        # Do not claim that a global result is in the requested city merely
        # because the city appeared in the search query. A Vietnam marker or
        # local domain is enough when the provider snippet omits the city.
        host = urlsplit(url).netloc.lower()
        if (
            " vietnam " in cleaned
            or " viet nam " in cleaned
            or host.endswith(".vn")
        ):
            return requested
        return ""
    if " vietnam " in cleaned or " viet nam " in cleaned:
        return "Vietnam"
    return ""


def _looks_like_job(title: str, description: str, url: str) -> bool:
    title_and_url = f" {clean_vn_text(title)} {clean_vn_text(url)} "
    text = f" {title_and_url} {clean_vn_text(description)} "
    clean_title = clean_vn_text(title).strip()
    path = urlsplit(url).path.lower()
    if any(signal in title_and_url for signal in NON_JOB_SIGNALS):
        return False
    if any(part in path for part in NON_JOB_PATHS):
        return False
    if re.search(
        r"^\s*(?:tuyen dung\s+)?[\d,.+]+\s+.*\b(jobs?|viec lam)\b",
        clean_title,
    ):
        return False
    if (
        (clean_title.startswith("find ") and " job" in clean_title)
        or re.search(r"\bjobs? in\b", clean_title)
        or re.search(r"\(\d+\)$", clean_title)
    ):
        return False
    return any(part in path for part in JOB_PATH_SIGNALS) or any(
        signal in text for signal in JOB_SIGNALS
    )


def _query_for_web(query: str, location: str | None) -> str:
    role = re.sub(r'["()]', " ", query)
    role = re.sub(r"\s+", " ", role).strip()
    variants = [role]
    if re.search(r"\bintern\b", role, flags=re.I):
        variants.append(re.sub(r"\bintern\b", "internship", role, flags=re.I))
    elif re.search(r"\binternship\b", role, flags=re.I):
        variants.append(re.sub(r"\binternship\b", "intern", role, flags=re.I))
    role_expression = " OR ".join(f'"{item}"' for item in dict.fromkeys(variants))

    place = normalize_location(location) if location else "vietnam"
    if place == "ho chi minh":
        location_expression = '"Ho Chi Minh" OR HCM OR Saigon'
    else:
        location_expression = f'"{place}" OR Vietnam'
    return (
        f"({role_expression}) ({location_expression}) "
        '(job OR hiring OR apply OR recruitment OR "tuyen dung" OR "viec lam")'
    )[:700]


class YouSearchClient:
    """Fetch and normalize job-like results from You.com Search API."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _api_key(self) -> str:
        if self.settings.you_api_key:
            return self.settings.you_api_key.strip()
        if self.settings.you_api_key_secret_arn:
            return _read_secret(
                self.settings.you_api_key_secret_arn,
                self.settings.aws_region,
            )
        raise YouSearchError("You.com API key is not configured")

    def search(
        self,
        query: str,
        *,
        location: str | None = None,
        limit: int = 20,
        deadline_at: float | None = None,
    ) -> list[dict]:
        preferred_payload: dict = {
            "query": _query_for_web(query, location),
            "count": min(100, max(10, min(self.settings.you_search_count, limit * 2))),
            "country": "VN",
            "safesearch": "moderate",
            "include_domains": VIETNAM_JOB_DOMAINS,
        }
        candidates = self._request(
            preferred_payload,
            self.settings.you_search_freshness,
            deadline_at=deadline_at,
        )
        jobs = self._normalize(candidates, query, location, limit)
        # Keep the same freshness window when broadening. This prevents an
        # empty first request from filling the result list with old postings.
        if (
            len(jobs) < min(3, limit)
            and (deadline_at is None or time.monotonic() < deadline_at - 2.5)
        ):
            fallback_payload = {
                **preferred_payload,
                "include_domains": BOOST_DOMAINS,
            }
            candidates.extend(
                self._request(
                    fallback_payload,
                    self.settings.you_search_freshness,
                    deadline_at=deadline_at,
                )
            )
            jobs = self._normalize(candidates, query, location, limit)
        logger.info(
            "You.com search normalized %s/%s job-like results",
            len(jobs),
            len(candidates),
        )
        return jobs

    def _request(
        self,
        payload: dict,
        freshness: str,
        *,
        deadline_at: float | None = None,
    ) -> list[dict]:
        request_payload = dict(payload)
        if freshness:
            request_payload["freshness"] = freshness
        timeout = self.settings.you_search_timeout_seconds
        if deadline_at is not None:
            remaining = deadline_at - time.monotonic()
            if remaining <= 1.5:
                raise YouSearchError("Không còn đủ thời gian để tìm trên web")
            timeout = min(timeout, max(1.0, remaining - 1.0))
        try:
            response = requests.post(
                self.settings.you_search_endpoint,
                headers={"X-API-Key": self._api_key()},
                json=request_payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            logger.warning("You.com search request failed: %s", type(exc).__name__)
            raise YouSearchError("Không kết nối được You.com") from exc

        if response.status_code != 200:
            logger.warning("You.com search returned HTTP %s", response.status_code)
            messages = {
                401: "You.com API key không hợp lệ",
                402: "Tài khoản You.com đã hết credit",
                403: "API key chưa có quyền Web Search",
                429: "You.com đang giới hạn tần suất tìm kiếm",
            }
            raise YouSearchError(
                messages.get(response.status_code, "You.com tạm thời có lỗi")
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise YouSearchError("You.com trả về dữ liệu không hợp lệ") from exc
        result_groups = body.get("results") or {}
        return list(result_groups.get("web") or []) + list(
            result_groups.get("news") or []
        )

    def _normalize(
        self,
        candidates: list[dict],
        query: str,
        location: str | None,
        limit: int,
    ) -> list[dict]:
        now = datetime.now(timezone.utc)
        jobs, seen_urls = [], set()
        for result in candidates:
            url = _canonical_url(result.get("url", ""))
            title = str(result.get("title") or "").strip()
            description = str(result.get("description") or "").strip()
            snippets = result.get("snippets") or []
            if not description and snippets:
                description = str(snippets[0])
            if not url or url in seen_urls or not title:
                continue
            if not _looks_like_job(title, description, url):
                continue
            domain = _domain_label(url)
            job = {
                "job_id": "web:" + identity({"source_url": url}),
                "title": title,
                "company": _company_from_title(title, domain),
                "location": _location_from_text(
                    f"{title} {description}", location, url
                ),
                "salary_raw": "",
                "description": description[:2000],
                "search_blob": clean_vn_text(f"{title} {description}"),
                "source": f"Web · {domain}"[:30],
                "source_url": url,
                "posted_at": result.get("page_age") or "",
                "scraped_at": now.isoformat(),
                "is_web_result": True,
            }
            seen_urls.add(url)
            jobs.append(job)

        ranked = rank_jobs(jobs, query, location=location, limit=None)
        deduplicated, seen = [], set()
        for job in ranked:
            key = identity(job)
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(job)
            if len(deduplicated) >= limit:
                break
        return deduplicated
