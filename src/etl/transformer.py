"""
ETL Transformer for ViecLamBot.

Transforms RawJob objects into normalized Job objects:
1. Normalize title (lowercase, strip extra whitespace)
2. Parse salary (extract min/max VND from various formats)
3. Normalize location (standardize city names)
4. Extract job type
5. Extract tags/skills from description
6. Generate deterministic job ID
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from src.common.logger import get_logger
from src.common.models import Job, JobType, RawJob
from src.common.text_utils import clean_vn_text, normalize_vn_text

logger = get_logger(__name__)


# ── Location normalization mapping ───────────────────────────────

# Aliases are matched on accent-folded text with word boundaries, so "hòa"/"hoà",
# "Đà Nẵng"/"Ðà Nẵng"/"da nang" and "ĐN"/"DN" all reach the same entry.
LOCATION_MAP: dict[str, str] = {
    # Ho Chi Minh variants
    "hồ chí minh": "Ho Chi Minh",
    "ho chi minh": "Ho Chi Minh",
    "hcm": "Ho Chi Minh",
    "hcmc": "Ho Chi Minh",
    "tp.hcm": "Ho Chi Minh",
    "tp. hcm": "Ho Chi Minh",
    "tp hcm": "Ho Chi Minh",
    "tphcm": "Ho Chi Minh",
    "sài gòn": "Ho Chi Minh",
    "saigon": "Ho Chi Minh",
    "sg": "Ho Chi Minh",
    "quận 1": "Ho Chi Minh",
    "quận 7": "Ho Chi Minh",
    "thủ đức": "Ho Chi Minh",
    # Ha Noi variants
    "hà nội": "Ha Noi",
    "ha noi": "Ha Noi",
    "hanoi": "Ha Noi",
    "hn": "Ha Noi",
    # Da Nang
    "đà nẵng": "Da Nang",
    "da nang": "Da Nang",
    "danang": "Da Nang",
    "đn": "Da Nang",
    # Other cities
    "bình dương": "Binh Duong",
    "bd": "Binh Duong",
    "đồng nai": "Dong Nai",
    "hải phòng": "Hai Phong",
    "cần thơ": "Can Tho",
    "bắc ninh": "Bac Ninh",
    "hưng yên": "Hung Yen",
    "bà rịa - vũng tàu": "Ba Ria - Vung Tau",
    "bà rịa vũng tàu": "Ba Ria - Vung Tau",
    "vũng tàu": "Ba Ria - Vung Tau",
    "bà rịa": "Ba Ria - Vung Tau",
    "brvt": "Ba Ria - Vung Tau",
    "br-vt": "Ba Ria - Vung Tau",
    "thừa thiên huế": "Thua Thien Hue",
    "huế": "Thua Thien Hue",
    "khánh hòa": "Khanh Hoa",
    "nha trang": "Khanh Hoa",
    "lâm đồng": "Lam Dong",
    "đà lạt": "Lam Dong",
    "quảng ninh": "Quang Ninh",
    "hạ long": "Quang Ninh",
    "đắk lắk": "Dak Lak",
    "đắc lắk": "Dak Lak",
    "buôn ma thuột": "Dak Lak",
}
# Remaining provinces (pre-2025 names, still what job boards print).
for _province in (
    "An Giang", "Bắc Giang", "Bắc Kạn", "Bạc Liêu", "Bến Tre", "Bình Định", "Bình Phước",
    "Bình Thuận", "Cà Mau", "Cao Bằng", "Đắk Nông", "Điện Biên", "Đồng Tháp", "Gia Lai",
    "Hà Giang", "Hà Nam", "Hà Tĩnh", "Hải Dương", "Hậu Giang", "Hòa Bình", "Kiên Giang",
    "Kon Tum", "Lai Châu", "Lạng Sơn", "Lào Cai", "Long An", "Nam Định", "Nghệ An",
    "Ninh Bình", "Ninh Thuận", "Phú Thọ", "Phú Yên", "Quảng Bình", "Quảng Nam",
    "Quảng Ngãi", "Quảng Trị", "Sóc Trăng", "Sơn La", "Tây Ninh", "Thái Bình",
    "Thái Nguyên", "Thanh Hóa", "Tiền Giang", "Trà Vinh", "Tuyên Quang", "Vĩnh Long",
    "Vĩnh Phúc", "Yên Bái",
):
    LOCATION_MAP.setdefault(_province.lower(), clean_vn_text(_province).title())
# Numbered districts only exist in Ho Chi Minh City.
for _number in range(1, 13):
    for _alias in (f"quận {_number}", f"q.{_number}", f"q{_number}", f"district {_number}"):
        LOCATION_MAP.setdefault(_alias, "Ho Chi Minh")

# District/county aliases → (district key, province).  A district filter keeps
# postings of that district (or of the province when the posting names none).
DISTRICT_MAP: dict[str, tuple[str, str]] = {
    **{name: (clean_vn_text(name), "Ho Chi Minh") for name in (
        "thủ đức", "bình thạnh", "gò vấp", "tân bình", "tân phú", "phú nhuận", "bình tân",
        "hóc môn", "củ chi", "nhà bè", "bình chánh", "cần giờ")},
    **{name: (clean_vn_text(name), "Ha Noi") for name in (
        "ba đình", "hoàn kiếm", "đống đa", "cầu giấy", "thanh xuân", "hai bà trưng",
        "hoàng mai", "long biên", "nam từ liêm", "bắc từ liêm", "tây hồ", "hà đông")},
    **{name: (clean_vn_text(name), "Binh Duong") for name in (
        "thủ dầu một", "dĩ an", "thuận an", "bến cát", "tân uyên")},
    "biên hòa": ("bien hoa", "Dong Nai"),
}

# A posting for these locations is open to candidates from any city.
NATIONWIDE_TERMS: tuple[str, ...] = (
    "toàn quốc", "cả nước", "nationwide", "remote", "từ xa", "work from home", "wfh",
)

_VN_TZ = timezone(timedelta(hours=7))  # Asia/Ho_Chi_Minh has no DST


@lru_cache(maxsize=1)
def _location_pattern() -> tuple[re.Pattern, dict[str, str]]:
    aliases = {clean_vn_text(k): v for k, v in LOCATION_MAP.items()}
    ordered = sorted(aliases, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, ordered)) + r")(?!\w)")
    return pattern, aliases


# ── Job type keywords ───────────────────────────────────────────

JOB_TYPE_KEYWORDS: dict[str, JobType] = {
    "full-time": JobType.FULL_TIME,
    "full time": JobType.FULL_TIME,
    "toàn thời gian": JobType.FULL_TIME,
    "part-time": JobType.PART_TIME,
    "part time": JobType.PART_TIME,
    "bán thời gian": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "hợp đồng": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "thực tập": JobType.INTERNSHIP,
    "remote": JobType.REMOTE,
    "từ xa": JobType.REMOTE,
    "work from home": JobType.REMOTE,
    "freelance": JobType.FREELANCE,
}

# ── Common tech skills for tag extraction ────────────────────────

TECH_SKILLS = [
    "python", "java", "javascript", "typescript", "sql", "nosql",
    "aws", "azure", "gcp", "docker", "kubernetes", "k8s",
    "spark", "hadoop", "kafka", "airflow", "dbt",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
    "react", "angular", "vue", "node.js", "nodejs",
    "terraform", "ansible", "jenkins", "ci/cd", "git",
    "machine learning", "deep learning", "ai", "nlp",
    "data engineer", "data analyst", "data scientist",
    "etl", "elt", "data pipeline", "data warehouse",
    "tableau", "power bi", "looker", "superset",
    "scala", "golang", "go", "rust", "c++", "c#", ".net",
    "linux", "agile", "scrum", "microservices", "api",
    "big data", "data lake", "snowflake", "redshift", "bigquery",
]

# Spellings that mean the same skill.  Every listed tag is emitted on a match so
# both "/search nodejs" and "/search node.js" find the posting.
_SKILL_ALIASES: dict[tuple[str, ...], str] = {
    ("node.js", "nodejs"): r"node\.?js|node js",
    ("react",): r"react|reactjs|react\.js",
    ("vue",): r"vue|vuejs|vue\.js",
    ("c#",): r"c#|c sharp",
    ("c++",): r"c\+\+|cpp",
    ("golang",): r"golang",
}
# Short skills that are also ordinary words ("ai" = who, "go" = to go): only the
# capitalised spelling counts.
_CASE_SENSITIVE_SKILLS: dict[str, str] = {"ai": r"AI", "go": r"Go|GO"}


def _skill_regex(alternatives: str, *, flags: int = 0) -> re.Pattern:
    # "+" and "#" belong to the token (C++, C#); a skill must not be glued to
    # letters on either side, so "java" never fires on JavaScript, "rust" on
    # "trust" or "scala" on "scalable".  ".net" may follow "asp".
    return re.compile(r"(?<![\w+#])(?:" + alternatives + r")(?![\w+#])", flags)


_SKILL_PATTERNS: list[tuple[tuple[str, ...], re.Pattern]] = []
_aliased = {tag for tags in _SKILL_ALIASES for tag in tags}
for _skill in TECH_SKILLS:
    if _skill in _aliased or _skill == ".net":
        continue
    if _skill in _CASE_SENSITIVE_SKILLS:
        _SKILL_PATTERNS.append(((_skill,), _skill_regex(_CASE_SENSITIVE_SKILLS[_skill])))
    else:
        _SKILL_PATTERNS.append(((_skill,), _skill_regex(re.escape(_skill), flags=re.I)))
for _tags, _alternatives in _SKILL_ALIASES.items():
    _SKILL_PATTERNS.append((_tags, _skill_regex(_alternatives, flags=re.I)))
# ".net" is also the tail of "ASP.NET" / "VB.NET", so only a "." may not precede it.
_SKILL_PATTERNS.append(((".net",), re.compile(
    r"(?<![\w+#.])dotnet(?![\w+#])|(?<![+#.])\.net(?![\w+#])", re.I
)))

_NEGOTIABLE = ("thỏa thuận", "thoả thuận", "thương lượng", "negotiable", "cạnh tranh",
               "competitive")
_NUMBER = r"\d+(?:[.,]\d+)*"
_SALARY_UNIT = r"(?:trieu|tr|million|mil|m|k|nghin|ngan|usd|vnd|d)(?![a-z])"
_SALARY_RANGE = re.compile(
    rf"({_NUMBER})\s*({_SALARY_UNIT})?"
    rf"(?:\s*(?:-|–|—|~|den|toi|to)\s*\$?\s*({_NUMBER})\s*({_SALARY_UNIT})?)?"
)
_PER_DAY = re.compile(
    r"/\s*(?:ngay|gio|h|day|hour|hr|ca|buoi)(?![a-z])|per\s+(?:day|hour)|theo\s+(?:gio|ngay)"
)
_PER_YEAR = re.compile(r"/\s*(?:nam|year|yr)(?![a-z])|per\s+(?:year|annum)|annual|yearly")
_UPPER_BOUND = re.compile(
    r"(?<![a-z])(?:len den|len toi|toi da|up to|upto|max|maximum|duoi|under|<)\W*$"
)
_LOWER_BOUND = re.compile(r"(?<![a-z])(?:tren|tu|toi thieu|from|min|minimum|over|above|>)\W*$")

_RELATIVE_DATE = re.compile(
    r"(?<!\w)(\d+|một|mot|an|a|one)\s*\+?\s*"
    r"(phút|phut|giây|giay|giờ|gio|tiếng|tieng|ngày|ngay|tuần|tuan|tháng|thang|năm|nam"
    r"|seconds?|secs?|minutes?|mins?|hours?|hrs?|h|days?|d|weeks?|w|months?|mo|years?|yrs?|y)"
    r"(?!\w)"
)
_RELATIVE_UNITS: dict[str, timedelta] = {
    **dict.fromkeys(("giây", "giay", "second", "seconds", "sec", "secs"), timedelta(seconds=1)),
    **dict.fromkeys(("phút", "phut", "minute", "minutes", "min", "mins"), timedelta(minutes=1)),
    **dict.fromkeys(("giờ", "gio", "tiếng", "tieng", "hour", "hours", "hr", "hrs", "h"),
                    timedelta(hours=1)),
    **dict.fromkeys(("ngày", "ngay", "day", "days", "d"), timedelta(days=1)),
    **dict.fromkeys(("tuần", "tuan", "week", "weeks", "w"), timedelta(weeks=1)),
    **dict.fromkeys(("tháng", "thang", "month", "months", "mo"), timedelta(days=30)),
    **dict.fromkeys(("năm", "nam", "year", "years", "yr", "yrs", "y"), timedelta(days=365)),
}
_NOW_WORDS = ("hôm nay", "today", "vừa xong", "vừa đăng", "mới đăng", "mới đây", "just now",
              "just posted")
_DMY = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?:[ ,t]+(\d{1,2}):(\d{2}))?")
_YMD = re.compile(r"(?<!\d)(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})(?!\d)")
_VN_LONG_DATE = re.compile(r"(\d{1,2})\s*tháng\s*(\d{1,2})\s*(?:năm|,)?\s*(\d{4})")


class Transformer:
    """Transform RawJob into normalized Job objects."""

    def transform(self, raw_job: RawJob) -> Optional[Job]:
        """Transform a single RawJob into a Job.

        Args:
            raw_job: Raw job data from scraper.

        Returns:
            Normalized Job, or None if transformation fails.
        """
        try:
            title_normalized = self._normalize_text(raw_job.title)

            job = Job(
                title=raw_job.title,
                title_normalized=title_normalized,
                company=raw_job.company,
                location=raw_job.location,
                location_normalized=self._normalize_location(raw_job.location),
                salary_raw=raw_job.salary_raw,
                description=raw_job.description,
                requirements=raw_job.requirements,
                job_type=self._parse_job_type(raw_job),
                tags=self._extract_tags(raw_job),
                source=raw_job.source,
                source_url=raw_job.source_url,
                posted_at=self._parse_date(raw_job.posted_at_raw, raw_job.scraped_at),
                expires_at=self._parse_deadline(getattr(raw_job, "valid_through_raw", "")),
                scraped_at=raw_job.scraped_at,
                is_new=True,
            )

            # Parse salary
            salary_min, salary_max = self._parse_salary(raw_job.salary_raw)
            job.salary_min = salary_min
            job.salary_max = salary_max

            # Precompute search blob for full-text matching.  It keeps the
            # diacritics (lowercase NFC) so search can tell "ít" from "IT"; the
            # matcher folds accents itself.  The company is left out: working
            # for "Công ty Xây dựng ABC" does not make an accountant a builder.
            job.search_blob = normalize_vn_text(" ".join(filter(None, [
                raw_job.title,
                raw_job.description,
                raw_job.requirements,
                " ".join(job.tags),
            ])))

            # Generate deterministic ID
            job.generate_id()

            return job

        except Exception as e:
            logger.warning(f"Transform failed for '{raw_job.title}': {e}")
            return None

    def transform_batch(self, raw_jobs: list[RawJob]) -> list[Job]:
        """Transform a batch of RawJobs.

        Args:
            raw_jobs: List of raw jobs.

        Returns:
            List of successfully transformed jobs.
        """
        jobs = []
        for raw_job in raw_jobs:
            job = self.transform(raw_job)
            if job:
                jobs.append(job)

        logger.info(
            f"Transformed {len(jobs)}/{len(raw_jobs)} jobs",
            extra={"job_count": len(jobs)},
        )
        return jobs

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize text: lowercase, strip, collapse whitespace."""
        text = text.lower().strip()
        text = re.sub(r"\s+", " ", text)
        # Normalize unicode (e.g., combine diacritics)
        text = unicodedata.normalize("NFC", text)
        return text

    @staticmethod
    def _normalize_location(location: str) -> str:
        """Normalize Vietnamese location to standard form."""
        if not location:
            return ""

        # Whole-word aliases on folded text: "bd" must not fire inside a word and
        # "Ðà Nẵng" (Latin Ð) is the same city as "Đà Nẵng".  The city named
        # first wins for multi-city postings.
        pattern, aliases = _location_pattern()
        match = pattern.search(clean_vn_text(location))
        if match:
            return aliases[match.group(1)]

        # Return original if no mapping found
        return location.strip()

    @staticmethod
    def _parse_salary(salary_raw: str) -> tuple[Optional[int], Optional[int]]:
        """Parse salary string into min/max VND values.

        Handles formats:
        - "10 - 14 triệu"  → (10_000_000, 14_000_000)
        - "15 triệu"       → (15_000_000, 15_000_000)
        - "1000 - 2000 USD" → (25_000_000, 50_000_000)  # ~25k VND/USD
        - "Thỏa thuận"     → (None, None)
        - "$1000-2000"     → (25_000_000, 50_000_000)
        - "Lên đến 20 triệu" → (None, 20_000_000)
        - "Trên 15 triệu"  → (15_000_000, None)
        - "8.5 - 10 triệu" → (8_500_000, 10_000_000)
        - "15-20 triệu + 2 tháng lương" → (15_000_000, 20_000_000)  # first range only
        - "$30,000/year"   → monthly equivalent; per-day/per-hour pay → (None, None)
        """
        if not salary_raw:
            return None, None

        salary = normalize_vn_text(salary_raw)
        if any(kw in salary for kw in _NEGOTIABLE):
            return None, None
        folded = clean_vn_text(salary)
        if _PER_DAY.search(folded):
            return None, None  # a daily/hourly rate is not comparable to a monthly filter

        match = _SALARY_RANGE.search(folded)
        if not match:
            return None, None
        first, first_unit, second, second_unit = match.groups()
        usd = bool(re.search(r"usd|\$|(?<![a-z])us dollars?", folded))
        divisor = 12 if _PER_YEAR.search(folded) else 1

        def amount(number: str, unit: str | None) -> int:
            value = Transformer._parse_number(number)
            unit = unit or ""
            if unit in {"trieu", "tr", "million", "mil", "m"}:
                value *= 1_000_000
            elif unit in {"k", "nghin", "ngan"}:
                value *= 1_000
            if usd:
                value *= 25_000  # Approximate VND/USD
            elif value < 1_000:
                value *= 1_000_000  # "15 - 20" on a VN board means millions
            return int(round(value / divisor))

        low = amount(first, first_unit or second_unit)
        if second is None:
            prefix = folded[: match.start()]
            if _UPPER_BOUND.search(prefix):
                return None, low
            if _LOWER_BOUND.search(prefix):
                return low, None
            return low, low
        high = amount(second, second_unit or first_unit)
        return min(low, high), max(low, high)

    @staticmethod
    def _parse_number(text: str) -> float:
        """'15.000.000' and '2,000' are thousands; '8.5' and '1,5' are decimals."""
        separators = re.findall(r"[.,]", text)
        if not separators:
            return float(text)
        groups = re.split(r"[.,]", text)
        if len(set(separators)) == 1 and all(len(g) == 3 for g in groups[1:]):
            return float("".join(groups))
        # Mixed "1.234,5" / "1,234.5": the last separator is the decimal point.
        whole, decimal = "".join(groups[:-1]), groups[-1]
        return float(f"{whole}.{decimal}")

    @staticmethod
    def _parse_job_type(raw_job: RawJob) -> JobType:
        """Determine job type from raw data."""
        # The board's own label wins over words in the title or description:
        # an "internship" card whose text mentions "full time" is an internship.
        for text in (
            raw_job.job_type_raw.lower(),
            f"{raw_job.title} {raw_job.description}".lower(),
        ):
            for keyword, job_type in JOB_TYPE_KEYWORDS.items():
                if keyword in text:
                    return job_type

        return JobType.UNKNOWN

    @staticmethod
    def _extract_tags(raw_job: RawJob) -> list[str]:
        """Extract technology tags/skills from job data."""
        search_text = f"{raw_job.title} {raw_job.description} {raw_job.requirements}"
        found_tags = set()
        for tags, pattern in _SKILL_PATTERNS:
            if pattern.search(search_text):
                found_tags.update(tags)
        return sorted(found_tags)

    @staticmethod
    def _parse_date(date_str: str, base_date: Optional[datetime] = None) -> Optional[datetime]:
        """Parse a posted date (absolute or relative) into an aware UTC datetime.

        Relative: "18 phút trước", "3 ngày trước", "Đăng 2 tháng trước", "Hôm nay",
        "Hôm qua", "2 days ago", "Posted an hour ago", "30+ days ago".
        Absolute: ISO 8601, epoch seconds/milliseconds, dd/mm/yyyy, dd-mm-yyyy,
        yyyy-mm-dd, "30 tháng 10 năm 2026".  Dates without a time zone are read
        as Asia/Ho_Chi_Minh local time.  Unparseable text gives None, so search
        falls back to ``first_seen_at`` instead of trusting a wrong date.
        """
        if not date_str or not str(date_str).strip():
            return None
        if base_date is None:
            base_date = datetime.now(timezone.utc)
        elif base_date.tzinfo is None:
            base_date = base_date.replace(tzinfo=timezone.utc)

        absolute = Transformer._parse_absolute_date(str(date_str))
        if absolute is not None:
            return absolute

        text = normalize_vn_text(str(date_str))
        if any(word in text for word in ("hôm kia",)):
            return base_date - timedelta(days=2)
        if any(word in text for word in ("hôm qua", "yesterday")):
            return base_date - timedelta(days=1)
        if any(word in text for word in _NOW_WORDS):
            return base_date
        # "Còn 3 ngày" / "5 days left" is a deadline countdown, not an age.
        if re.search(r"(?<!\w)(?:còn|left|remaining)(?!\w)", text):
            return None
        match = _RELATIVE_DATE.search(text)
        if not match:
            return None
        count, unit = match.groups()
        value = int(count) if count.isdigit() else 1
        return base_date - value * _RELATIVE_UNITS[unit]

    @staticmethod
    def _parse_absolute_date(text: str, *, end_of_day: bool = False) -> Optional[datetime]:
        """Absolute dates only; date-only values are local (UTC+7) midnight or end of day."""
        text = text.strip()
        if not text:
            return None
        if text.isdigit():
            if len(text) < 10:
                return None
            ts = int(text)
            if ts > 1_000_000_000_000:  # epoch milliseconds (Chợ Tốt list_time)
                ts = ts / 1000
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None

        def local(year, month, day, hour=None, minute=None) -> Optional[datetime]:
            try:
                if hour is None:
                    value = datetime(int(year), int(month), int(day), tzinfo=_VN_TZ)
                    if end_of_day:
                        value += timedelta(days=1, microseconds=-1)
                else:
                    value = datetime(int(year), int(month), int(day), int(hour), int(minute),
                                     tzinfo=_VN_TZ)
            except ValueError:
                return None
            return value.astimezone(timezone.utc)

        iso = text.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            parsed = None
        if parsed is not None:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                return local(parsed.year, parsed.month, parsed.day)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_VN_TZ)
            return parsed.astimezone(timezone.utc)

        lowered = normalize_vn_text(text)
        match = _DMY.search(lowered)
        if match:
            day, month, year, hour, minute = match.groups()
            return local(year, month, day, hour, minute)
        match = _YMD.search(lowered)
        if match:
            year, month, day = match.groups()
            return local(year, month, day)
        match = _VN_LONG_DATE.search(lowered)
        if match:
            day, month, year = match.groups()
            return local(year, month, day)
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return local(parsed.year, parsed.month, parsed.day)
        return None

    @staticmethod
    def _parse_deadline(deadline_raw: str) -> Optional[datetime]:
        """Application deadline → expiry instant (end of that local day), else None."""
        if not deadline_raw:
            return None
        return Transformer._parse_absolute_date(str(deadline_raw), end_of_day=True)
