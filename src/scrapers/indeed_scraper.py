"""Indeed Vietnam through the mobile-app GraphQL endpoint.

The endpoint and the app API key are the ones `python-jobspy` uses
(MIT licence): a plain JSON POST, no cookies, no login, no HTML.  Each
result already carries title, employer, location, salary, posting date and
the full description, so no per-job request is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

from src.common.models import JobSource, RawJob
from src.scrapers.base_scraper import BaseScraper

API_URL = "https://apis.indeed.com/graphql"
BASE_URL = "https://vn.indeed.com"
PAGE_SIZE = 100
LIVE_PAGE_SIZE = 40
MAX_PAGES_DEFAULT = 1
DEFAULT_LOCATION = "Vietnam"
HOURS_OLD = 72

# Public app key shipped in the Indeed mobile app (same value jobspy uses).
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
    "accept-language": "vi-VN,vi;q=0.9,en;q=0.8",
    "indeed-api-key": "161092c2017b5bbab13edb12461a62d5a833871e7cad6d9d475304573de67ac8",
    "indeed-co": "VN",
    "indeed-locale": "vi-VN",
    "indeed-app-info": "appv=193.1; appid=com.indeed.jobsearch; osv=16.6.1; os=ios; dtype=phone",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 Indeed App 193.1"
    ),
}

# Attribute codes of the "job type" taxonomy (from jobspy).
JOB_TYPE_CODES = {
    "CF3CP": "full-time",
    "75GKK": "part-time",
    "NJXCK": "contract",
    "T9BXE": "contract",
    "T65DZ": "contract",
    "VDTG7": "internship",
    "4HKF7": "temporary",
    "CJWTS": "temporary",
    "TQKYQ": "per diem",
    "UXQZ8": "volunteer",
}
REMOTE_CODE = "DSQF7"
HYBRID_CODE = "PAXZC"

QUERY = """
query GetJobData {
  jobSearch(
    what: @WHAT@
    location: {where: @WHERE@, radius: 50, radiusUnit: MILES}
    limit: @LIMIT@
    @CURSOR@
    sort: RELEVANCE
    filters: [{ date: { field: "dateOnIndeed", start: "@HOURS@h" } }]
  ) {
    pageInfo { nextCursor }
    results {
      job {
        key title sourceEmployerName dateOnIndeed
        description { html }
        location { countryCode admin1Code city }
        compensation {
          baseSalary {
            unitOfWork
            range {
              ... on Range { min max }
              ... on AtLeast { min }
              ... on AtMost { max }
              ... on Exactly { value }
            }
          }
          currencyCode
        }
        attributes { key }
        employer { name relativeCompanyPageUrl }
      }
    }
  }
}
"""

_PERIOD_VI = {
    "HOUR": "giờ",
    "DAY": "ngày",
    "WEEK": "tuần",
    "MONTH": "tháng",
    "YEAR": "năm",
}


def build_query(keyword: str, location: str, limit: int, cursor: str | None, hours: int) -> str:
    return (
        QUERY.replace("@WHAT@", json.dumps(keyword, ensure_ascii=False))
        .replace("@WHERE@", json.dumps(location, ensure_ascii=False))
        .replace("@LIMIT@", str(int(limit)))
        .replace("@CURSOR@", f"cursor: {json.dumps(cursor)}" if cursor else "")
        .replace("@HOURS@", str(int(hours)))
    )


class IndeedScraper(BaseScraper):
    def __init__(self, hours_old: int = HOURS_OLD):
        super().__init__(source=JobSource.INDEED)
        self.hours_old = hours_old
        self.session.headers.update(HEADERS)

    def scrape(
        self,
        keyword: str,
        max_pages: Optional[int] = None,
        location: Optional[str] = None,
    ) -> list[RawJob]:
        pages = 1 if self.live_mode else (max_pages or MAX_PAGES_DEFAULT)
        limit = LIVE_PAGE_SIZE if self.live_mode else PAGE_SIZE
        where = location or DEFAULT_LOCATION
        jobs: list[RawJob] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(pages):
            payload = {"query": build_query(keyword, where, limit, cursor, self.hours_old)}
            response = self._post(API_URL, json_data=payload)
            search = ((response.json() or {}).get("data") or {}).get("jobSearch")
            if not search:
                self.last_error = "NoJobSearch"
                break
            for result in search.get("results") or []:
                raw = result.get("job")
                if not raw or raw.get("key") in seen:
                    continue
                seen.add(raw.get("key"))
                job = self.parse_job(raw)
                if job:
                    jobs.append(job)
            cursor = (search.get("pageInfo") or {}).get("nextCursor")
            if not cursor:
                break
        return jobs

    def parse_job(self, raw_data: dict) -> Optional[RawJob]:
        key = raw_data.get("key")
        title = str(raw_data.get("title") or "").strip()
        if not key or not title:
            return None
        employer = raw_data.get("employer") or {}
        company = str(raw_data.get("sourceEmployerName") or employer.get("name") or "")
        attributes = {a.get("key") for a in raw_data.get("attributes") or []}
        job_types = {JOB_TYPE_CODES[k] for k in attributes if k in JOB_TYPE_CODES}
        if REMOTE_CODE in attributes and HYBRID_CODE not in attributes:
            job_types.add("remote")
        html = ((raw_data.get("description") or {}).get("html")) or ""
        description = BeautifulSoup(html, "html.parser").get_text("\n", strip=True) if html else ""
        return RawJob(
            title=title,
            company=company,
            location=_location(raw_data.get("location") or {}),
            salary_raw=_salary(raw_data.get("compensation") or {}),
            description=description,
            job_type_raw=", ".join(sorted(job_types)),
            source=JobSource.INDEED,
            source_url=f"{BASE_URL}/viewjob?jk={key}",
            posted_at_raw=_posted(raw_data.get("dateOnIndeed")),
        )


# Indeed admin1Code values: ISO 3166-2:VN (letters for the big cities, numbers otherwise).
ADMIN1_PROVINCES = {
    "HN": "Hà Nội", "SG": "Hồ Chí Minh", "DN": "Đà Nẵng", "HP": "Hải Phòng", "CT": "Cần Thơ",
    "01": "Lai Châu", "02": "Lào Cai", "03": "Hà Giang", "04": "Cao Bằng", "05": "Sơn La",
    "06": "Yên Bái", "07": "Tuyên Quang", "09": "Lạng Sơn", "13": "Quảng Ninh", "14": "Hòa Bình",
    "18": "Ninh Bình", "20": "Thái Bình", "21": "Thanh Hóa", "22": "Nghệ An", "23": "Hà Tĩnh",
    "24": "Quảng Bình", "25": "Quảng Trị", "26": "Thừa Thiên Huế", "27": "Quảng Nam",
    "28": "Kon Tum", "29": "Quảng Ngãi", "30": "Gia Lai", "31": "Bình Định", "32": "Phú Yên",
    "33": "Đắk Lắk", "34": "Khánh Hòa", "35": "Lâm Đồng", "36": "Ninh Thuận", "37": "Tây Ninh",
    "39": "Đồng Nai", "40": "Bình Thuận", "41": "Long An", "43": "Bà Rịa - Vũng Tàu",
    "44": "An Giang", "45": "Đồng Tháp", "46": "Tiền Giang", "47": "Kiên Giang",
    "49": "Vĩnh Long", "50": "Bến Tre", "51": "Trà Vinh", "52": "Sóc Trăng", "53": "Bắc Kạn",
    "54": "Bắc Giang", "55": "Bạc Liêu", "56": "Bắc Ninh", "57": "Bình Dương", "58": "Bình Phước",
    "59": "Cà Mau", "61": "Hải Dương", "63": "Hà Nam", "66": "Hưng Yên", "67": "Nam Định",
    "68": "Phú Thọ", "69": "Thái Nguyên", "70": "Vĩnh Phúc", "71": "Điện Biên",
    "72": "Đắk Nông", "73": "Hậu Giang",
}
_HCM_ALIASES = {"sg", "sài gòn", "sai gon", "saigon", "hcm", "hcmc", "tphcm", "tp hcm",
                "tp.hcm", "ho chi minh", "ho chi minh city", "hồ chí minh",
                "thành phố hồ chí minh", "tp. hồ chí minh", "tp hồ chí minh"}


def _clean_place(text) -> str:
    text = str(text or "").replace("Ð", "Đ").replace("ð", "đ").strip()
    if text.lower() in _HCM_ALIASES:
        return "Hồ Chí Minh"
    return text


def _location(loc: dict) -> str:
    city = _clean_place(loc.get("city"))
    if city:
        return city
    code = str(loc.get("admin1Code") or "").strip()
    if code:
        province = ADMIN1_PROVINCES.get(code.upper())
        if province:
            return province
        if not code.isdigit() and len(code) > 2:
            return _clean_place(code)
    country = loc.get("countryCode")
    return "Việt Nam" if country == "VN" else str(country or "")


def _posted(millis) -> str:
    try:
        return datetime.fromtimestamp(float(millis) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _salary(compensation: dict) -> str:
    pay = compensation.get("baseSalary") or {}
    amounts = pay.get("range") or {}
    low = amounts.get("min", amounts.get("value"))
    high = amounts.get("max", amounts.get("value"))
    if low is None and high is None:
        return ""
    currency = compensation.get("currencyCode") or "VND"
    period = _PERIOD_VI.get(pay.get("unitOfWork") or "", "")
    if currency == "VND" and period == "tháng":
        low_text = f"{low / 1e6:g}" if low is not None else ""
        high_text = f"{high / 1e6:g}" if high is not None else ""
        if low_text and high_text and low_text != high_text:
            return f"{low_text} - {high_text} triệu/tháng"
        return f"{low_text or high_text} triệu/tháng"
    values = [f"{v:,.0f}" for v in (low, high) if v is not None]
    suffix = f"/{period}" if period else ""
    return f"{' - '.join(dict.fromkeys(values))} {currency}{suffix}"
