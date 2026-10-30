"""Per-user result filters: default /filter choices, blacklist, hidden jobs, feedback.

Everything here is pure (no I/O) so the webhook, /myjobs and the alert run share
exactly the same rules.  Jobs only carry title/company/location/salary fields in
the search snapshot, so level and work type fall back to title heuristics when
``job_type`` is absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from src.common.text_utils import clean_vn_text

# ── Choice catalogues (short codes keep callback_data far below 64 bytes) ──

CITY_CHOICES: dict[str, str] = {
    "hcm": "Hồ Chí Minh",
    "hn": "Hà Nội",
    "dn": "Đà Nẵng",
    "hp": "Hải Phòng",
    "ct": "Cần Thơ",
    "bd": "Bình Dương",
    "dna": "Đồng Nai",
    "bn": "Bắc Ninh",
    "all": "Toàn quốc",
}
SALARY_CHOICES: dict[str, int] = {
    "0": 0, "8": 8_000_000, "10": 10_000_000, "15": 15_000_000,
    "20": 20_000_000, "30": 30_000_000,
}
LEVEL_CHOICES: dict[str, str] = {
    "intern": "Thực tập",
    "fresher": "Fresher",
    "junior": "Junior",
    "mid": "Middle",
    "senior": "Senior / Lead",
    "any": "Mọi cấp bậc",
}
JOB_TYPE_CHOICES: dict[str, str] = {
    "ft": "Toàn thời gian",
    "pt": "Bán thời gian",
    "remote": "Remote",
    "any": "Mọi hình thức",
}

# Which detected title levels each filter accepts.  ``None`` = title has no
# level marker; strict for intern/senior, lenient for the middle of the ladder.
_LEVEL_ACCEPTS: dict[str, frozenset] = {
    "intern": frozenset({"intern"}),
    "fresher": frozenset({"fresher", "junior", None}),
    "junior": frozenset({"junior", "fresher", None}),
    "mid": frozenset({"mid", None}),
    "senior": frozenset({"senior", "lead"}),
}

_LEVEL_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("intern", ("intern", "internship", "thuc tap", "thuc tap sinh", "tts", "trainee")),
    ("lead", ("lead", "leader", "truong nhom", "truong phong", "head of", "manager",
              "principal", "giam doc", "director")),
    ("senior", ("senior", "sr", "chuyen gia")),
    ("fresher", ("fresher", "entry level", "moi tot nghiep", "new grad", "graduate")),
    ("junior", ("junior", "jr")),
    ("mid", ("middle", "mid level", "mid-level")),
)
_PART_TIME = ("part time", "part-time", "parttime", "ban thoi gian")
_REMOTE = ("remote", "tu xa", "work from home", "wfh", "lam viec online")


@lru_cache(maxsize=256)
def _word_pattern(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")


def _has_any(text: str, terms) -> bool:
    return any(_word_pattern(term).search(text) for term in terms)


def detect_level(job: dict) -> str | None:
    """Level named in the title (intern/fresher/junior/mid/senior/lead), else None."""
    if str(job.get("job_type") or "").lower() == "internship":
        return "intern"
    title = clean_vn_text(job.get("title") or "")
    for level, terms in _LEVEL_PATTERNS:
        if _has_any(title, terms):
            return level
    return None


def detect_job_types(job: dict) -> set[str]:
    """Work types we can see: 'pt' and/or 'remote'; empty means ordinary full-time."""
    kinds = set()
    job_type = str(job.get("job_type") or "").lower()
    if job_type == "part_time":
        kinds.add("pt")
    if job_type == "remote":
        kinds.add("remote")
    text = clean_vn_text(" ".join(str(job.get(key) or "") for key in ("title", "location")))
    if _has_any(text, _PART_TIME):
        kinds.add("pt")
    if _has_any(text, _REMOTE):
        kinds.add("remote")
    return kinds


def level_ok(job: dict, level: str | None) -> bool:
    if not level or level == "any":
        return True
    return detect_level(job) in _LEVEL_ACCEPTS.get(level, frozenset({None}))


def job_type_ok(job: dict, job_type: str | None) -> bool:
    if not job_type or job_type == "any":
        return True
    kinds = detect_job_types(job)
    if job_type == "ft":
        return "pt" not in kinds
    return job_type in kinds


def salary_ok(job: dict, salary_min: int | None) -> bool:
    """Same rule as search.matches_filters: unknown salaries are kept."""
    if not salary_min:
        return True
    salary = job.get("salary_max") or job.get("salary_min")
    try:
        return salary is None or int(salary) >= int(salary_min)
    except (TypeError, ValueError):
        return True


def normalize_term(value: str) -> str:
    return re.sub(r"\s+", " ", clean_vn_text(value or "")).strip()


def text_has_term(text: str, term: str) -> bool:
    """Accent/case-insensitive whole-word containment of ``term`` in ``text``."""
    term = normalize_term(term)
    return bool(term) and bool(_word_pattern(term).search(normalize_term(text)))


def is_excluded(job: dict, excluded_terms) -> bool:
    if not excluded_terms:
        return False
    text = normalize_term(f"{job.get('title') or ''} {job.get('company') or ''}")
    return any(term and _word_pattern(term).search(text) for term in excluded_terms)


def company_matches(job: dict, company_term: str) -> bool:
    return text_has_term(str(job.get("company") or ""), company_term)


def job_key(job: dict) -> str:
    """Short stable id for buttons and SAVED#/HIDDEN# keys (32 hex chars)."""
    from src.matcher.search import identity

    return str(job.get("identity") or identity(job))


@dataclass
class UserContext:
    """What one user told the bot about results (loaded with one query)."""

    prefs: dict = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)
    excluded: dict[str, str] = field(default_factory=dict)  # normalized -> raw
    disliked_companies: set[str] = field(default_factory=set)  # normalized
    follows: dict[str, str] = field(default_factory=dict)  # normalized -> raw

    @property
    def level(self) -> str | None:
        return self.prefs.get("filter_level") or None

    @property
    def job_type(self) -> str | None:
        return self.prefs.get("filter_job_type") or None

    @property
    def salary_min(self) -> int | None:
        value = int(self.prefs.get("filter_salary_min") or 0)
        return value or None

    @property
    def location(self) -> str | None:
        code = self.prefs.get("filter_location") or ""
        return None if code in {"", "all"} else CITY_CHOICES.get(code, code)


def apply_context(
    jobs: list[dict], ctx: UserContext | None, *, use_filters: bool = True,
    use_salary: bool = True,
) -> list[dict]:
    """Drop hidden/blacklisted jobs, apply default filters, push disliked companies down."""
    if ctx is None:
        return list(jobs)
    kept, disliked = [], []
    excluded = tuple(ctx.excluded)
    for job in jobs:
        if job_key(job) in ctx.hidden or is_excluded(job, excluded):
            continue
        if use_filters and not (
            (not use_salary or salary_ok(job, ctx.salary_min))
            and level_ok(job, ctx.level)
            and job_type_ok(job, ctx.job_type)
        ):
            continue
        company = normalize_term(str(job.get("company") or ""))
        (disliked if company and company in ctx.disliked_companies else kept).append(job)
    # Soft down-rank: disliked employers stay visible, just after everyone else.
    return kept + disliked


def filters_summary(prefs: dict) -> str:
    location = CITY_CHOICES.get(prefs.get("filter_location") or "all", "Toàn quốc")
    salary = int(prefs.get("filter_salary_min") or 0)
    salary_text = f"từ {salary // 1_000_000} triệu" if salary else "không giới hạn"
    level = LEVEL_CHOICES.get(prefs.get("filter_level") or "any", "Mọi cấp bậc")
    job_type = JOB_TYPE_CHOICES.get(prefs.get("filter_job_type") or "any", "Mọi hình thức")
    return (
        f"📍 Địa điểm: {location}\n"
        f"💰 Lương: {salary_text}\n"
        f"🎓 Cấp bậc: {level}\n"
        f"🕘 Hình thức: {job_type}"
    )
