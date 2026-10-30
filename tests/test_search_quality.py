"""Search-quality regression cases from docs/SEARCH_QUALITY_REVIEW.md.

Passing tests pin behaviour that already works and must not regress.  Cases marked
``xfail(strict=False)`` document a finding of the review: they fail on the current
code and should start passing (and then lose the marker) when the fix lands.
Finding ids (F1, F2, ...) refer to the review document.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.common.models import JobSource, RawJob
from src.common.text_utils import clean_vn_text
from src.etl.transformer import Transformer
from src.matcher.search import (
    contains,
    identity,
    matches_filters,
    normalize_location,
    parse_search,
    rank_jobs,
    relevance,
)

M = 1_000_000
BASE = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def xfail(reason: str):
    return pytest.mark.xfail(strict=False, reason=reason)


def etl_item(title, description="", *, company="Example", location="Hồ Chí Minh",
             source=JobSource.CAREERVIET, url=None, salary="", posted="", **extra):
    """Build a job exactly the way ETL stores it in DynamoDB (search_blob + tags)."""
    raw = RawJob(title=title, company=company, location=location, description=description,
                 salary_raw=salary, posted_at_raw=posted, source=source,
                 source_url=url or f"https://example.com/{abs(hash((title, company, location)))}")
    item = Transformer().transform(raw).to_dynamo_item()
    item.update(extra)
    return item


def score(title, description, query, **kwargs):
    return relevance(etl_item(title, description, **kwargs), query)


def age_hours(text):
    parsed = Transformer._parse_date(text, BASE)
    return None if parsed is None else round((BASE - parsed).total_seconds() / 3600, 1)


# ── Behaviour that works today (guards) ────────────────────────────────────────


@pytest.mark.parametrize("query,title", [
    ("kế toán", "Kế toán tổng hợp"),
    ("ke toan", "KẾ TOÁN THUẾ"),
    ("accountant", "Kế toán nội bộ"),
    ("kỹ sư dữ liệu", "Data Engineer"),
    ("HR", "HR Generalist"),
    ("C&B", "Chuyên viên C&B"),
    ("c#", "Lập trình viên C# .NET"),
    ("senior .net", "Senior .NET Developer"),
    ("thực tập marketing", "Marketing Intern"),
])
def test_guard_equivalent_title_matches(query, title):
    assert score(title, "", query) >= 80


@pytest.mark.parametrize("query,title", [
    ("java", "JavaScript Developer"),
    ("data engineer", "Data Analyst"),
    ("điều dưỡng", "Bác sĩ đa khoa"),
    ("it", "Digital Marketing Executive"),
    ("c++", "C Developer"),
])
def test_guard_no_substring_or_sibling_occupation_in_title(query, title):
    job = etl_item(title)
    job["tags"] = []  # isolate the title; tag pollution is covered by F5
    job["search_blob"] = clean_vn_text(title)
    assert relevance(job, query) == 0


def test_guard_exact_title_outranks_description_mention():
    exact = etl_item("Kế toán tổng hợp", source=JobSource.ITVIEC)
    mention = etl_item("Nhân viên kinh doanh", "Phối hợp với phòng kế toán",
                       source=JobSource.CAREERVIET)
    assert rank_jobs([mention, exact], "kế toán", limit=None)[0]["title"] == exact["title"]


@pytest.mark.parametrize("query_location,job_location,expected", [
    ("HCM", "Quận 7, TP.HCM", True),
    ("Hồ Chí Minh", "Sài Gòn", True),
    ("hn", "Hà Nội", True),
    ("HCM", "Hà Nội", False),
    ("Hà Nội", "Hà Nội, Hồ Chí Minh", True),
    ("binh duong", "Thủ Dầu Một, Bình Dương", True),
    ("Vũng Tàu", "Bà Rịa - Vũng Tàu", True),
])
def test_guard_location_aliases(query_location, job_location, expected):
    assert matches_filters(etl_item("Kế toán", location=job_location),
                           location=query_location) is expected


@pytest.mark.parametrize("text,expected", [
    ("kế toán | HCM", ("kế toán", "HCM")),
    ("tìm việc kế toán tại Hà Nội", ("tìm việc kế toán", "Hà Nội")),
    ("data engineer", ("data engineer", None)),
])
def test_guard_parse_search_explicit_location(text, expected):
    assert parse_search(text) == expected


@pytest.mark.parametrize("raw,expected", [
    ("15-20 triệu", (15 * M, 20 * M)),
    ("15tr", (15 * M, 15 * M)),
    ("15.000.000 - 20.000.000 VNĐ", (15 * M, 20 * M)),
    ("1,000 - 1,500 USD", (25 * M, 37_500_000)),
    ("Thỏa thuận", (None, None)),
])
def test_guard_salary_formats(raw, expected):
    assert Transformer._parse_salary(raw) == expected


@pytest.mark.parametrize("raw,hours", [
    ("2 days ago", 48.0),
    ("5 giờ trước", 5.0),
    ("1 tuần trước", 168.0),
    ("2026-10-29T12:00:00+00:00", 24.0),
])
def test_guard_posted_at_formats(raw, hours):
    assert age_hours(raw) == hours


def test_guard_salary_filter_keeps_unknown_and_drops_lower():
    low = etl_item("Kế toán", salary="8 - 10 triệu")
    unknown = etl_item("Kế toán viên", salary="Thỏa thuận")
    assert not matches_filters(low, salary_min=15 * M)
    assert matches_filters(unknown, salary_min=15 * M)


def test_guard_identity_merges_case_variants():
    a = {"title": "Kế toán tổng hợp", "company": "Công ty ABC", "location": "TP.HCM"}
    b = {"title": "KẾ TOÁN TỔNG HỢP", "company": "CÔNG TY ABC", "location": "Hồ Chí Minh"}
    assert identity(a) == identity(b)


# ── F2: accent folding makes short/ambiguous terms collide ─────────────────────


@pytest.mark.parametrize("query,title,description", [
    ("IT", "Nhân viên bán hàng", "Ít nhất 2 năm kinh nghiệm"),
    ("DE", "Nhân viên kinh doanh", "Có cơ hội để phát triển bản thân"),
    ("nhân viên kho", "Nhân viên bán hàng", "Không ngại khó khăn"),
    ("kỹ sư điện", "Kỹ sư dự toán", "Làm việc với đại diện chủ đầu tư"),
    ("BA", "Nhân viên phục vụ", "Làm ca ba, có phụ cấp"),
])
def test_f2_accent_collisions_do_not_match(query, title, description):
    assert score(title, description, query) == 0


# ── F3: parse_search splits on commas / unaccented "tai", misses implicit city ─


@pytest.mark.parametrize("text,expected", [
    ("kế toán, kiểm toán", ("kế toán, kiểm toán", None)),
    ("ke toan tai chinh", ("ke toan tai chinh", None)),
    ("python, django", ("python, django", None)),
    ("logistics Bình Dương", ("logistics", "Bình Dương")),
    ("nhân viên kinh doanh Hà Nội", ("nhân viên kinh doanh", "Hà Nội")),
])
def test_f3_parse_search(text, expected):
    assert parse_search(text) == expected


def test_f3_salary_phrase_in_query_does_not_zero_results():
    job = etl_item("Kế toán tổng hợp", salary="15 - 20 triệu")
    assert rank_jobs([job], "lương trên 15 triệu kế toán", limit=None)


# ── F4: location aliases, districts, nationwide/remote ─────────────────────────


@pytest.mark.parametrize("query_location,job_location", [
    ("BD", "Bình Dương"),
    ("ĐN", "Đà Nẵng"),
    ("HCMC", "Hồ Chí Minh"),
    ("HCM", "Toàn quốc"),
])
def test_f4_location_aliases_and_nationwide(query_location, job_location):
    assert matches_filters(etl_item("Kế toán", location=job_location), location=query_location)


def test_f4_district_filter_is_not_whole_city():
    assert not matches_filters(etl_item("Kế toán", location="Quận 1, Hồ Chí Minh"),
                               location="Thủ Đức")


# ── F6: salary parser ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("7.5 - 10 triệu", (7_500_000, 10 * M)),
    ("Lương 15-20 triệu + 2 tháng lương", (15 * M, 20 * M)),
    ("Up to $2,000 (attractive)", (None, 50 * M)),
    ("$30,000/year", (62_500_000, 62_500_000)),
    ("Lên đến 20 triệu", (None, 20 * M)),
    ("Trên 1.5 triệu/ngày", (None, None)),
])
def test_f6_salary_parsing(raw, expected):
    assert Transformer._parse_salary(raw) == expected


# ── F5: posted_at parser ────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,hours", [
    ("3 ngày trước", 72.0),
    ("Đăng 2 ngày trước", 48.0),
    ("2 tháng trước", 1440.0),
    ("18 phút trước", 0.3),
    ("Hôm nay", 0.0),
    ("Hôm qua", 24.0),
    ("1 year ago", 8760.0),
    ("5 minutes ago", 0.1),
])
def test_f5_relative_dates(raw, hours):
    assert age_hours(raw) == hours


def test_f5_vietnamese_date_uses_local_timezone():
    assert age_hours("30/10/2026") == 19.0


# ── F8: tag extraction pollutes the search blob ─────────────────────────────────


@pytest.mark.parametrize("text,unwanted", [
    ("ReactJS, JavaScript, TypeScript", "java"),
    ("Trustworthy team, scalable systems", "rust"),
    ("Trustworthy team, scalable systems", "scala"),
])
def test_f8_no_substring_tags(text, unwanted):
    assert unwanted not in etl_item("Developer", text)["tags"]


def test_f8_symbol_skills_are_tagged():
    assert {"c++", "c#"} <= set(etl_item("Developer", "C++ and C# required")["tags"])


def test_f8_java_query_does_not_return_javascript_job_via_tags():
    assert score("Frontend Developer", "JavaScript, ReactJS", "java") == 0


# ── F11: token boundaries ────────────────────────────────────────────────────────


def test_f11_dotnet_inside_aspnet():
    assert contains("asp.net core developer", ".net")


# ── F1: company name and description-only hits ──────────────────────────────────


def test_f1_company_name_is_not_an_occupation_match():
    assert score("Kế toán nội bộ", "", "xây dựng", company="Công ty CP Xây dựng ABC") == 0


def test_f1_description_only_mentions_are_not_returned_for_alert_grade_search():
    job = etl_item("Nhân viên kinh doanh", "Báo cáo doanh số cho phòng kế toán")
    assert rank_jobs([job], "kế toán", limit=None) == []


# ── F7: cross-board identity ────────────────────────────────────────────────────


@xfail("F7: identity() needs exact title+company+location; legal-form suffixes and a "
       "district prefix split the same posting into two cards")
def test_f7_identity_across_boards():
    a = {"title": "Kế toán tổng hợp", "company": "Công ty TNHH ABC",
         "location": "Quận 1, Hồ Chí Minh"}
    b = {"title": "Kế Toán Tổng Hợp", "company": "CÔNG TY TNHH ABC VIỆT NAM",
         "location": "Hồ Chí Minh"}
    assert identity(a) == identity(b)


# ── F9: freshness / interleaving ───────────────────────────────────────────────


@xfail("F9: round-robin across sources puts a 40-day-old job second, ahead of fresh "
       "jobs from a busier board")
def test_f9_interleaving_does_not_promote_stale_jobs():
    now = datetime.now(timezone.utc)
    fresh = [etl_item(f"Kế toán {i}", source=JobSource.CAREERVIET,
                      posted_at=(now - timedelta(hours=i + 1)).isoformat()) for i in range(3)]
    stale = etl_item("Kế toán kho", source=JobSource.ITVIEC,
                     posted_at=(now - timedelta(days=40)).isoformat())
    ranked = rank_jobs([*fresh, stale], "kế toán", limit=None)
    assert ranked.index(next(j for j in ranked if j["title"] == "Kế toán kho")) == 3


def test_f9_age_limit_uses_posting_date():
    now = datetime.now(timezone.utc)
    job = etl_item("Kế toán", posted_at=(now - timedelta(days=90)).isoformat(),
                   scraped_at=now.isoformat())
    assert not matches_filters(job, max_age_days=60, now=now)


# ── Smoke: normalisation helpers used everywhere ────────────────────────────────


def test_guard_normalize_location_is_idempotent():
    once = normalize_location("TP.HCM")
    assert normalize_location(once) == once == "ho chi minh"


# ── Behaviour added with the fixes ───────────────────────────────────────────────


def test_skill_in_description_is_alert_grade_but_occupation_is_not():
    skill = etl_item("Backend Developer", "Build services with Python and SQL")
    mention = etl_item("Nhân viên kinh doanh", "Báo cáo doanh số cho phòng kế toán")
    assert rank_jobs([skill], "python", limit=None) == [skill]
    # Interactive search keeps the description-only mention, after title hits.
    exact = etl_item("Kế toán tổng hợp")
    ranked = rank_jobs([mention, exact], "kế toán", limit=None, min_score=1)
    assert [j["title"] for j in ranked] == ["Kế toán tổng hợp", "Nhân viên kinh doanh"]


def test_accented_query_still_matches_unaccented_posting():
    assert score("Ke toan tong hop", "", "kế toán") >= 80
    assert score("Kỹ sư điện công nghiệp", "", "ky su dien") >= 80
    assert score("Kỹ sư điện", "", "kỹ sư điện") >= 80


def test_tone_placement_variants_match():
    assert score("Nhân viên hoá chất", "", "hóa chất") >= 80


def test_comma_and_slash_are_alternatives():
    accountant = etl_item("Kế toán tổng hợp")
    auditor = etl_item("Kiểm toán viên")
    assert len(rank_jobs([accountant, auditor], "kế toán, kiểm toán", limit=None)) == 2
    assert len(rank_jobs([accountant, auditor], "kế toán/kiểm toán", limit=None)) == 2


def test_salary_phrases_become_filters():
    from src.matcher.search import extract_salary_filter

    assert extract_salary_filter("lương trên 15 triệu kế toán") == ("kế toán", 15 * M, None)
    assert extract_salary_filter("kế toán từ 10-15 triệu") == ("kế toán", 10 * M, 15 * M)
    assert extract_salary_filter("kế toán dưới 20tr") == ("kế toán", None, 20 * M)
    low = etl_item("Kế toán tổng hợp", salary="8 - 10 triệu")
    high = etl_item("Kế toán thuế", salary="25 - 30 triệu")
    assert rank_jobs([low, high], "kế toán trên 15tr", limit=None) == [high]
    assert rank_jobs([low, high], "kế toán dưới 20 triệu", limit=None) == [low]


@pytest.mark.parametrize("text,expected", [
    ("ke toan o ha noi", ("ke toan", "ha noi")),
    ("kế toán HCM", ("kế toán", "HCM")),
    ("sales BD", ("sales BD", None)),
    ("kế toán, Đà Nẵng", ("kế toán", "Đà Nẵng")),
])
def test_parse_search_places(text, expected):
    assert parse_search(text) == expected


@pytest.mark.parametrize("query_location,job_location,expected", [
    ("Đà Nẵng", "Ðà Nẵng, DN", True),
    ("SG", "Hồ Chí Minh", True),
    ("TPHCM", "Quận 3", True),
    ("Q.1", "Quận 1, Hồ Chí Minh", True),
    ("Thủ Đức", "Hồ Chí Minh", True),
    ("Hà Nội", "Remote", True),
    ("Hà Nội", "Đà Nẵng", False),
])
def test_location_gazetteer(query_location, job_location, expected):
    assert matches_filters(etl_item("Kế toán", location=job_location),
                           location=query_location) is expected


def test_age_falls_back_to_first_seen_before_scraped_at():
    now = datetime.now(timezone.utc)
    old = etl_item("Kế toán", first_seen_at=(now - timedelta(days=90)).isoformat(),
                   scraped_at=now.isoformat())
    assert "posted_at" not in old
    assert not matches_filters(old, max_age_days=60, now=now)
    fresh = etl_item("Kế toán", first_seen_at=(now - timedelta(days=2)).isoformat())
    assert matches_filters(fresh, posted_since=now - timedelta(days=7), now=now)


def test_unparseable_posted_date_is_none():
    assert Transformer._parse_date("Còn 5 ngày để ứng tuyển", BASE) is None
    assert Transformer._parse_date("sắp hết hạn", BASE) is None


def test_valid_through_becomes_expires_at():
    raw = RawJob(title="Kế toán", source=JobSource.VIETNAMWORKS,
                 source_url="https://example.com/deadline", valid_through_raw="15/11/2026")
    job = Transformer().transform(raw)
    # End of 15/11 in Vietnam (UTC+7) is 16:59:59.999999 UTC.
    assert job.expires_at == datetime(2026, 11, 15, 16, 59, 59, 999999, tzinfo=timezone.utc)
    assert Transformer._parse_deadline("2026-11-15T10:00:00+07:00") == datetime(
        2026, 11, 15, 3, tzinfo=timezone.utc)
    assert Transformer._parse_deadline("30-11-2026").day == 30
    assert Transformer().transform(raw.model_copy(update={"valid_through_raw": "Hết hạn"})) \
        .expires_at is None
    expired = etl_item("Kế toán")
    expired["expires_at"] = "2026-01-01T00:00:00+00:00"
    assert not matches_filters(expired)


@pytest.mark.parametrize("raw,expected", [
    ("8.5 - 10 triệu", (8_500_000, 10 * M)),
    ("1.500 USD", (37_500_000, 37_500_000)),
    ("15 - 25 triệu/tháng", (15 * M, 25 * M)),
    ("Trên 15 triệu", (15 * M, None)),
])
def test_salary_more_formats(raw, expected):
    assert Transformer._parse_salary(raw) == expected


def test_tags_aliases_and_ambiguous_short_skills():
    tags = etl_item("Developer", "ASP.NET Core, Node.js, ReactJS. Ai cũng có thể go further")["tags"]
    assert {".net", "node.js", "nodejs", "react"} <= set(tags)
    assert "ai" not in tags and "go" not in tags


def test_fully_unaccented_query_still_folds_short_syllables():
    assert score("Kế Toán Tổng Hợp", "", "ke toan tong hop") >= 80
    # ... but an abbreviation typed in capitals stays literal.
    assert score("Nhân viên bán hàng", "Ít nhất 1 năm", "IT support") == 0


def test_district_postings_rank_before_province_only_postings():
    city = etl_item("Kế toán nội bộ", location="Hồ Chí Minh")
    district = etl_item("Kế toán thuế", location="TP Thủ Đức, Hồ Chí Minh")
    ranked = rank_jobs([city, district], "kế toán", location="Thủ Đức", limit=None)
    assert [j["title"] for j in ranked] == ["Kế toán thuế", "Kế toán nội bộ"]


def test_de_abbreviation_finds_data_engineers():
    assert score("Data Engineer", "", "DE") >= 80
